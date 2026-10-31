"""Servidor de licencias del PROVEEDOR (servicio aparte del ERP; en Render: `erp-licencias`).

Responsabilidades:
  * /v1/activate y /v1/refresh — emite tokens Ed25519 atados a una instalación (plan, topes, equipos).
  * /admin/v1/...             — API del usuario Maestro (la consume el ERP por la red privada). Cada petición va firmada
                                con HMAC-SHA256 (LICENSE_ADMIN_SECRET) + marca de tiempo + nonce de un solo uso.
  * Órdenes de pago           — USDT con cuenta personal de Binance (monto único, validado leyendo el historial con una clave
                                de SOLO LECTURA) y bolívares por pago móvil (verificación del Maestro).
  * Entrega única por correo  — la clave se genera al entregar, se envía UNA vez por Brevo y solo se guarda su SHA-256.

Persistencia: PostgreSQL (o SQLite en pruebas) con SQLAlchemy Core; todo SQL va parametrizado (sin inyecciones).
"""
import hashlib
import hmac
import json
import re
import secrets
import time
from collections import defaultdict, deque
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from flask import Flask, jsonify, request
from sqlalchemy import (Column, Float, ForeignKey, Integer, MetaData, Numeric, String, Table, Text, create_engine, func, or_,
                        insert, select, update)
from sqlalchemy.exc import IntegrityError

from app.licensing.token import firmar

VIGENCIA_ORDEN_USDT_S = 24 * 3600    # una orden en USDT espera su pago 24 h
VIGENCIA_TOKEN_S = 7 * 24 * 3600   # ventana offline máxima: 7 días
LIMITE_PETICIONES = (20, 60)        # 20 peticiones por 60 s y por IP (endpoints públicos)
TOLERANCIA_FIRMA_S = 300
PLANES = ("ERP_CONTABLE", "CASH", "ERP_ADMIN", "ALL_IN_ONE", "PERSONALIZADO", "DEMO")
PLANES_TIENDA = PLANES[:4]   # PERSONALIZADO se vende como «combo» (lic_combos); DEMO es gratuita y dura 72 h
DEMO_HORAS = 72
DEMO_TOPES = dict(max_usuarios=2, max_empresas=1, max_equipos=2)
# Precios de REFERENCIA (USD) del mercado venezolano de software administrativo en la nube (2026). Se siembran una sola vez
# y solo donde no hay precio: nunca reemplazan un precio que el Maestro ya fijó. Anual = 10 meses (2 meses de regalo).
PRECIOS_REFERENCIA = {  # plan: (mensual USD, usuarios, empresas, equipos)
    "CASH": ("25.00", 3, 1, 3), "ERP_ADMIN": ("23.00", 5, 2, 5), "ERP_CONTABLE": ("45.00", 5, 5, 5), "ALL_IN_ONE": ("69.00", 10, 5, 10)}
PRECIOS_MODULO_REFERENCIA = {"contabilidad": "25.00", "administrativo": "15.00", "auditoria": "12.00", "inventario": "12.00", "facturacion": "18.00"}
DESCUENTO_ANUAL_PCT = Decimal("16.67")   # pagar 10 de 12 meses
MODULOS_VALIDOS = ("contabilidad", "administrativo", "auditoria", "inventario", "facturacion")


def _modulos(valor) -> str:
    lista = valor.split(",") if isinstance(valor, str) else list(valor or [])
    mods = sorted({str(m).strip() for m in lista if str(m).strip()})
    if not mods: raise ValueError("Elija al menos un módulo para el plan personalizado")
    if any(m not in MODULOS_VALIDOS for m in mods): raise ValueError("Módulo inválido")
    return ",".join(mods)
ESTADOS = ("activa", "suspendida", "revocada")
RE_REF = re.compile(r"^[A-Z0-9]{8,32}$")
RE_CORREO = re.compile(r"^[^@\s<>]{1,64}@[^@\s<>]{1,190}\.[A-Za-z]{2,24}$")

md = MetaData()
licencias = Table(
    "lic_licencias", md,
    Column("id", Integer, primary_key=True), Column("key_hash", String(64), unique=True, nullable=False),
    Column("cliente", Text, nullable=False), Column("correo", Text), Column("organizacion_ref", Text),
    Column("estado", String(12), nullable=False, default="activa"), Column("plan", String(16)),
    Column("max_instancias", Integer, nullable=False, default=1), Column("max_usuarios", Integer, nullable=False, default=5),
    Column("max_empresas", Integer, nullable=False, default=10), Column("max_equipos", Integer),
    Column("vence_en", Float, nullable=False), Column("creada_en", Float, nullable=False),
    Column("origen", String(12), nullable=False, default="maestro"), Column("clave_enviada_en", Float),
    Column("modulos", Text),
    # venta registrada por el Maestro (licencias emitidas a mano): con esto el sistema arma la factura solo
    Column("venta_monto", Numeric(18, 4)), Column("venta_moneda", String(4)), Column("venta_tasa", Numeric(18, 6)),
    Column("venta_forma_pago", Text), Column("cliente_rif", Text), Column("dias", Integer))
activaciones = Table(
    "lic_activaciones", md,
    Column("licencia_id", Integer, ForeignKey("lic_licencias.id"), primary_key=True), Column("huella", String(64), primary_key=True),
    Column("primera_vez", Float, nullable=False), Column("ultima_vez", Float, nullable=False))
eventos = Table(
    "lic_eventos", md,
    Column("id", Integer, primary_key=True), Column("ts", Float), Column("licencia_id", Integer), Column("evento", Text),
    Column("ip", Text), Column("detalle", Text))
nonces = Table("lic_nonces", md, Column("nonce", String(64), primary_key=True), Column("ts", Float, nullable=False))
precios = Table(
    "lic_precios", md,
    Column("plan", String(16), primary_key=True), Column("meses", Integer, primary_key=True),
    Column("monto_usd", Numeric(12, 2), nullable=False), Column("max_usuarios", Integer, nullable=False),
    Column("max_empresas", Integer, nullable=False), Column("max_equipos", Integer, nullable=False))
ordenes = Table(
    "lic_ordenes", md,
    Column("id", Integer, primary_key=True), Column("ref", String(32), unique=True, nullable=False),
    Column("organizacion_ref", Text, nullable=False), Column("cliente", Text, nullable=False), Column("correo", Text, nullable=False),
    Column("plan", String(16), nullable=False), Column("meses", Integer, nullable=False),
    Column("max_usuarios", Integer, nullable=False), Column("max_empresas", Integer, nullable=False), Column("max_equipos", Integer, nullable=False),
    Column("moneda", String(4), nullable=False), Column("monto", Numeric(18, 2), nullable=False), Column("monto_usd", Numeric(12, 2), nullable=False),
    Column("tasa_bcv", Numeric(18, 6)), Column("proveedor", String(12), nullable=False),
    Column("estado", String(14), nullable=False, default="pendiente"), Column("checkout_url", Text), Column("prepay_id", Text),
    Column("referencia_pago", Text), Column("detalle_pago", Text), Column("licencia_id", Integer),
    Column("creada_en", Float, nullable=False), Column("pagada_en", Float), Column("entregada_en", Float), Column("motivo", Text),
    Column("monto_esperado", Numeric(18, 4)), Column("expira_en", Float), Column("instrucciones", Text),
    Column("cliente_rif", Text), Column("cliente_direccion", Text), Column("combo_id", Integer), Column("modulos", Text))
# Combos personalizados: el Maestro arma un paquete de módulos con su período (mensual o anual), precio y topes.
combos = Table(
    "lic_combos", md,
    Column("id", Integer, primary_key=True), Column("nombre", Text, nullable=False), Column("modulos", Text, nullable=False),
    Column("meses", Integer, nullable=False), Column("monto_usd", Numeric(12, 2), nullable=False),
    Column("max_usuarios", Integer, nullable=False), Column("max_empresas", Integer, nullable=False), Column("max_equipos", Integer, nullable=False),
    Column("activo", Integer, nullable=False, default=1), Column("creado_en", Float, nullable=False))
# Precio mensual de cada módulo: con él se calcula el precio sugerido de un combo según lo que incluya
precios_modulo = Table(
    "lic_precios_modulo", md,
    Column("modulo", String(20), primary_key=True), Column("mensual_usd", Numeric(12, 2), nullable=False))
meta = Table("lic_meta", md, Column("clave", String(40), primary_key=True), Column("valor", Text))
# Cada transacción de Binance paga como máximo UNA orden (la clave primaria lo impone en la base de datos)
pagos_usados = Table(
    "lic_pagos_usados", md,
    Column("pago_id", String(120), primary_key=True), Column("orden_ref", String(32), nullable=False),
    Column("monto", Numeric(18, 8), nullable=False), Column("registrado_en", Float, nullable=False))
COLUMNAS_NUEVAS = {"lic_licencias": [("modulos", "TEXT"), ("venta_monto", "NUMERIC(18,4)"), ("venta_moneda", "VARCHAR(4)"), ("venta_tasa", "NUMERIC(18,6)"),
                                     ("venta_forma_pago", "TEXT"), ("cliente_rif", "TEXT"), ("dias", "INTEGER")], "lic_ordenes": [("monto_esperado", "NUMERIC(18,4)"), ("expira_en", "FLOAT"), ("instrucciones", "TEXT"),
                                   ("cliente_rif", "TEXT"), ("cliente_direccion", "TEXT"), ("combo_id", "INTEGER"), ("modulos", "TEXT")]}


def _h(clave: str) -> str:
    return hashlib.sha256(clave.strip().upper().encode()).hexdigest()


def nueva_clave() -> str:
    return "ERP-" + "-".join(secrets.token_hex(3).upper() for _ in range(4))


def _dec(v, nombre="monto") -> Decimal:
    try:
        d = Decimal(str(v)).quantize(Decimal("0.01"), ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError) as e:
        raise ValueError(f"{nombre} inválido") from e
    if not d.is_finite() or d <= 0 or d > Decimal("1000000000"):
        raise ValueError(f"{nombre} fuera de rango")
    return d


def _tasa(v) -> Decimal:
    """Tasa BCV con su precisión publicada (hasta 6 decimales); no se redondea a céntimos."""
    try:
        d = Decimal(str(v)).quantize(Decimal("0.000001"), ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError) as e:
        raise ValueError("tasa BCV inválida") from e
    if not d.is_finite() or d <= 0 or d > Decimal("100000000"): raise ValueError("tasa BCV fuera de rango")
    return d


def _entero(v, nombre, minimo=0, maximo=100000):
    try:
        n = int(v)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{nombre} inválido") from e
    if not (minimo <= n <= maximo): raise ValueError(f"{nombre} fuera de rango")
    return n


class Almacen:
    """Acceso a datos del servidor de licencias. Todo SQL es SQLAlchemy Core parametrizado."""

    def __init__(self, url, reloj=time.time):
        if url.startswith("postgres://"): url = "postgresql+psycopg://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"): url = "postgresql+psycopg://" + url[len("postgresql://"):]
        self.eng = create_engine(url, pool_pre_ping=True)
        self.reloj = reloj
        md.create_all(self.eng)
        from . import facturas as fx
        from . import presupuestos as px
        fx.instalar(self.eng); px.instalar(self.eng)
        from sqlalchemy import inspect, text
        with self.eng.begin() as c:  # bases creadas antes: se agregan las columnas nuevas (nombres fijos, no hay datos del usuario)
            for tabla, cols in COLUMNAS_NUEVAS.items():
                existentes = {x["name"] for x in inspect(c).get_columns(tabla)}
                for nombre, tipo in cols:
                    if nombre not in existentes: c.execute(text(f"ALTER TABLE {tabla} ADD COLUMN {nombre} {tipo}"))
        self.sembrar_referencia()

    # ---------------- licencias ----------------
    def crear_licencia(self, cliente, dias=365, max_usuarios=5, max_empresas=10, max_instancias=1, plan="ERP_CONTABLE",
                       max_equipos=None, correo=None, organizacion_ref=None, origen="maestro", con=None, modulos=None) -> tuple[int, str]:
        if plan not in PLANES: raise ValueError(f"Plan inválido: {plan}")
        modulos = _modulos(modulos) if plan == "PERSONALIZADO" else None
        if plan == "DEMO": dias = DEMO_HORAS // 24   # la demo dura 72 h, la emita quien la emita
        if correo and not RE_CORREO.match(correo): raise ValueError("Correo inválido")
        clave, ahora = nueva_clave(), self.reloj()
        datos = dict(key_hash=_h(clave), cliente=str(cliente)[:200], correo=correo, organizacion_ref=organizacion_ref, estado="activa", plan=plan,
                     max_instancias=_entero(max_instancias, "instancias", 1, 1000), max_usuarios=_entero(max_usuarios, "usuarios", 1, 100000),
                     max_empresas=_entero(max_empresas, "empresas", 1, 100000), max_equipos=None if max_equipos in (None, "") else _entero(max_equipos, "equipos", 1, 100000),
                     vence_en=ahora + _entero(dias, "días", 1, 3650) * 86400, creada_en=ahora, origen=origen, modulos=modulos, dias=int(dias))
        if con is not None:
            lid = con.execute(insert(licencias).values(**datos).returning(licencias.c.id)).scalar()
        else:
            with self.eng.begin() as c:
                lid = c.execute(insert(licencias).values(**datos).returning(licencias.c.id)).scalar()
        return lid, clave  # la clave se muestra/envía UNA vez; solo se guarda su hash

    def rotar_clave(self, lid, con) -> str:
        clave = nueva_clave()
        con.execute(update(licencias).where(licencias.c.id == lid).values(key_hash=_h(clave)))
        con.execute(activaciones.delete().where(activaciones.c.licencia_id == lid))  # la clave anterior deja de servir
        return clave

    def modificar(self, lid, cambios: dict):
        permitidos = {}
        if "plan" in cambios:
            if cambios["plan"] not in PLANES: raise ValueError("Plan inválido")
            permitidos["plan"] = cambios["plan"]
        if cambios.get("modulos"): permitidos["modulos"] = _modulos(cambios["modulos"])
        if "estado" in cambios:
            if cambios["estado"] not in ESTADOS: raise ValueError("Estado inválido")
            permitidos["estado"] = cambios["estado"]
        for k, n, maximo in (("max_usuarios", "usuarios", 100000), ("max_empresas", "empresas", 100000), ("max_instancias", "instancias", 1000), ("max_equipos", "equipos", 100000)):
            if k in cambios and cambios[k] not in (None, ""): permitidos[k] = _entero(cambios[k], n, 1, maximo)
        with self.eng.begin() as c:
            fila = c.execute(select(licencias).where(licencias.c.id == lid)).first()
            if fila is None: raise LookupError("Licencia inexistente")
            plan_final = permitidos.get("plan", fila.plan)
            if plan_final == "PERSONALIZADO" and not (permitidos.get("modulos") or fila.modulos):
                raise ValueError("El plan personalizado necesita sus módulos")
            if plan_final != "PERSONALIZADO":
                permitidos.pop("modulos", None)
                if "plan" in permitidos: permitidos["modulos"] = None
            if cambios.get("dias_extra"):
                permitidos["vence_en"] = max(fila.vence_en, self.reloj()) + _entero(cambios["dias_extra"], "días", 1, 3650) * 86400
            if permitidos: c.execute(update(licencias).where(licencias.c.id == lid).values(**permitidos))
        return permitidos

    def liberar(self, lid):
        with self.eng.begin() as c:
            c.execute(activaciones.delete().where(activaciones.c.licencia_id == lid))

    def listar(self, organizacion_ref=None):
        q = select(licencias, select(func.count()).where(activaciones.c.licencia_id == licencias.c.id).scalar_subquery().label("en_uso")).order_by(licencias.c.id.desc())
        if organizacion_ref: q = q.where(licencias.c.organizacion_ref == organizacion_ref)
        with self.eng.connect() as c:
            return [{k: v for k, v in r._mapping.items() if k != "key_hash"} for r in c.execute(q)]

    def eventos(self, lid, n=100):
        with self.eng.connect() as c:
            return [dict(r._mapping) for r in c.execute(select(eventos).where(eventos.c.licencia_id == lid).order_by(eventos.c.id.desc()).limit(n))]

    def log(self, con, lid, evento, detalle="", ip=None):
        con.execute(insert(eventos).values(ts=self.reloj(), licencia_id=lid, evento=evento, ip=ip, detalle=str(detalle)[:500]))

    # ---------------- nonces de la API admin (anti-repetición) ----------------
    def usar_nonce(self, nonce) -> bool:
        ahora = self.reloj()
        try:
            with self.eng.begin() as c:
                c.execute(nonces.delete().where(nonces.c.ts < ahora - 2 * TOLERANCIA_FIRMA_S))
                c.execute(insert(nonces).values(nonce=nonce, ts=ahora))
            return True
        except IntegrityError:
            return False

    # ---------------- precios ----------------
    def fijar_precio(self, plan, meses, monto_usd, max_usuarios, max_empresas, max_equipos):
        if plan not in PLANES_TIENDA: raise ValueError("Plan inválido para el catálogo")
        datos = dict(monto_usd=_dec(monto_usd, "precio"), max_usuarios=_entero(max_usuarios, "usuarios", 1), max_empresas=_entero(max_empresas, "empresas", 1),
                     max_equipos=_entero(max_equipos, "equipos", 1))
        meses = _entero(meses, "meses", 1, 60)
        with self.eng.begin() as c:
            if c.execute(select(precios.c.plan).where(precios.c.plan == plan, precios.c.meses == meses)).first():
                c.execute(update(precios).where(precios.c.plan == plan, precios.c.meses == meses).values(**datos))
            else:
                c.execute(insert(precios).values(plan=plan, meses=meses, **datos))

    def precios(self):
        with self.eng.connect() as c:
            return [{**dict(r._mapping), "monto_usd": str(r.monto_usd)} for r in c.execute(select(precios).order_by(precios.c.plan, precios.c.meses))]

    def sembrar_referencia(self, forzar=False) -> int:
        """Precios de referencia (mercado VE): solo agrega lo que falta; un precio ya fijado por el Maestro no se toca.
        Sin `forzar` corre una única vez por base (marca en lic_meta), para no reponer lo que el Maestro haya retirado."""
        n = 0
        with self.eng.begin() as c:
            if not forzar and c.execute(select(meta.c.valor).where(meta.c.clave == "precios_referencia")).first(): return 0
            for plan, (mensual, us, em, eq) in PRECIOS_REFERENCIA.items():
                for meses in (1, 12):
                    if c.execute(select(precios.c.plan).where(precios.c.plan == plan, precios.c.meses == meses)).first(): continue
                    monto = Decimal(mensual) * (10 if meses == 12 else 1)
                    c.execute(insert(precios).values(plan=plan, meses=meses, monto_usd=monto, max_usuarios=us, max_empresas=em, max_equipos=eq)); n += 1
            for mod, v in PRECIOS_MODULO_REFERENCIA.items():
                if not c.execute(select(precios_modulo.c.modulo).where(precios_modulo.c.modulo == mod)).first():
                    c.execute(insert(precios_modulo).values(modulo=mod, mensual_usd=Decimal(v))); n += 1
            if not c.execute(select(meta.c.clave).where(meta.c.clave == "precios_referencia")).first():
                c.execute(insert(meta).values(clave="precios_referencia", valor=str(self.reloj())))
        return n

    # ---------------- combos personalizados ----------------
    def precios_modulos(self) -> dict:
        with self.eng.connect() as c:
            return {r.modulo: str(r.mensual_usd) for r in c.execute(select(precios_modulo))}

    def fijar_precio_modulo(self, modulo, mensual_usd):
        if modulo not in MODULOS_VALIDOS: raise ValueError("Módulo inválido")
        v = _dec(mensual_usd, "precio del módulo")
        with self.eng.begin() as c:
            if c.execute(select(precios_modulo.c.modulo).where(precios_modulo.c.modulo == modulo)).first():
                c.execute(update(precios_modulo).where(precios_modulo.c.modulo == modulo).values(mensual_usd=v))
            else:
                c.execute(insert(precios_modulo).values(modulo=modulo, mensual_usd=v))

    def precio_sugerido(self, modulos, meses) -> Decimal:
        """Suma mensual de los módulos incluidos × meses; 12 meses o más pagan 10 de cada 12. Se redondea a dólares enteros."""
        mods, meses = _modulos(modulos).split(","), _entero(meses, "meses", 1, 60)
        tabla = self.precios_modulos()
        faltan = [m for m in mods if m not in tabla]
        if faltan: raise ValueError("Falta el precio mensual de: " + ", ".join(faltan))
        total = sum((Decimal(tabla[m]) for m in mods), Decimal(0)) * meses
        if meses >= 12: total = total * (100 - DESCUENTO_ANUAL_PCT) / 100
        return total.quantize(Decimal("1"), ROUND_HALF_UP).quantize(Decimal("0.01"))

    def crear_combo(self, nombre, modulos, meses, monto_usd=None, max_usuarios=5, max_empresas=2, max_equipos=5) -> int:
        nombre = str(nombre or "").strip()[:80]
        if not nombre: raise ValueError("Indique el nombre del combo")
        mods, meses = _modulos(modulos), _entero(meses, "meses", 1, 60)
        monto = _dec(monto_usd, "precio") if monto_usd not in (None, "") else self.precio_sugerido(mods, meses)
        with self.eng.begin() as c:
            return c.execute(insert(combos).values(nombre=nombre, modulos=mods, meses=meses, monto_usd=monto, max_usuarios=_entero(max_usuarios, "usuarios", 1),
                                                   max_empresas=_entero(max_empresas, "empresas", 1), max_equipos=_entero(max_equipos, "equipos", 1),
                                                   activo=1, creado_en=self.reloj()).returning(combos.c.id)).scalar()

    def activar_combo(self, cid, activo: bool):
        with self.eng.begin() as c:
            if not c.execute(update(combos).where(combos.c.id == int(cid)).values(activo=1 if activo else 0)).rowcount: raise LookupError("Combo inexistente")

    def combos(self, solo_activos=False):
        q = select(combos).order_by(combos.c.activo.desc(), combos.c.nombre, combos.c.meses)
        if solo_activos: q = q.where(combos.c.activo == 1)
        with self.eng.connect() as c:
            return [{**dict(r._mapping), "monto_usd": str(r.monto_usd)} for r in c.execute(q)]


# ======================================================================================================================
def create_app(db_url: str, clave_privada, reloj=time.time, secreto_admin: str | None = None, binance=None, correo=None, config=None) -> Flask:
    """`binance`: instancia de BinanceCuenta (o None si no se cobra en USDT). `correo(destino, asunto, texto)`: envío (Brevo).
    `config`: {"url_publica", "url_erp", "pago_movil"} para enlaces e instrucciones de pago."""
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
    st = Almacen(db_url, reloj)
    app.extensions["almacen"] = st
    cfg = config or {}
    historial = defaultdict(deque)

    def limitado(ip):
        n, ventana = LIMITE_PETICIONES
        q, ahora = historial[ip], reloj()
        while q and q[0] < ahora - ventana: q.popleft()
        q.append(ahora)
        return len(q) > n

    @app.after_request
    def cabeceras(resp):
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Strict-Transport-Security"] = "max-age=63072000"
        return resp

    @app.get("/salud")
    def salud():
        return jsonify(ok=True)

    # ------------------------------------------------------------------ activación pública
    def emitir(accion):
        ip = request.remote_addr or "?"
        if limitado(ip): return jsonify(message="Demasiadas solicitudes"), 429
        d = request.get_json(silent=True) or {}
        clave, huella, nonce = str(d.get("key", ""))[:64], str(d.get("fingerprint", "")), str(d.get("nonce", ""))[:64]
        if not (clave and re.fullmatch(r"[0-9a-f]{64}", huella) and nonce):
            return jsonify(message="Solicitud inválida"), 400
        ahora = reloj()
        with st.eng.begin() as con:
            lic = con.execute(select(licencias).where(licencias.c.key_hash == _h(clave)).with_for_update()).first()
            if not lic:
                st.log(con, None, "clave_desconocida", ip=ip)
                return jsonify(message="Clave de licencia no válida"), 404
            if lic.estado != "activa":
                st.log(con, lic.id, f"rechazo_{lic.estado}", ip=ip)
                return jsonify(message=f"Licencia {lic.estado}"), 403
            if ahora > lic.vence_en:
                st.log(con, lic.id, "rechazo_vencida", ip=ip)
                return jsonify(message="Licencia vencida"), 403
            act = con.execute(select(activaciones).where(activaciones.c.licencia_id == lic.id, activaciones.c.huella == huella)).first()
            if not act:
                en_uso = con.execute(select(func.count()).where(activaciones.c.licencia_id == lic.id)).scalar()
                if accion == "refresh" or en_uso >= lic.max_instancias:
                    st.log(con, lic.id, "rechazo_en_uso", huella[:12], ip)
                    return jsonify(message="Esta licencia ya está en uso en el máximo de instalaciones permitidas"), 409
                con.execute(insert(activaciones).values(licencia_id=lic.id, huella=huella, primera_vez=ahora, ultima_vez=ahora))
                st.log(con, lic.id, "activada", huella[:12], ip)
            else:
                con.execute(update(activaciones).where(activaciones.c.licencia_id == lic.id, activaciones.c.huella == huella).values(ultima_vez=ahora))
                st.log(con, lic.id, "renovada", huella[:12], ip)
        claims = {"lid": lic.id, "cliente": lic.cliente, "fp": huella, "nonce": nonce, "max_usuarios": lic.max_usuarios,
                  "max_empresas": lic.max_empresas, "iat": int(ahora), "exp": int(min(ahora + VIGENCIA_TOKEN_S, lic.vence_en)),
                  "vence_licencia": int(lic.vence_en)}
        if lic.plan: claims["plan"] = lic.plan            # firmado: el cliente no puede ampliar su plan
        if lic.plan == "PERSONALIZADO": claims["modulos"] = (lic.modulos or "").split(",")   # módulos a medida, también firmados
        if lic.max_equipos: claims["max_equipos"] = lic.max_equipos
        if lic.organizacion_ref: claims["org"] = lic.organizacion_ref
        return jsonify(token=firmar(claims, clave_privada))

    app.add_url_rule("/v1/activate", "activate", lambda: emitir("activate"), methods=["POST"])
    app.add_url_rule("/v1/refresh", "refresh", lambda: emitir("refresh"), methods=["POST"])

    # ------------------------------------------------------------------ entrega única por correo
    def enviar_clave(con, lic_id, clave, correo_destino, cliente, plan, vence_en, motivo):
        if correo is None: raise RuntimeError("Correo no configurado")
        from app import plantilla_correo as pc
        url_erp = cfg.get("url_erp") or ""
        parrafos = [f"Estimado(a) {cliente}:", motivo,
                    "Para activarla, ingrese a Contax con su usuario Administrador, vaya a Organización → Licencia, pegue la clave "
                    "en «Activar licencia» y pulse Activar."]
        from app.planes import PLANES as NOMBRES
        plan = NOMBRES[plan]["nombre"] if plan in NOMBRES else plan
        filas = [("Plan", plan), ("Vigente hasta", time.strftime("%d/%m/%Y %H:%M UTC" if plan.startswith("Demo") else "%d/%m/%Y", time.gmtime(vence_en)))]
        nota = ("Esta clave se envía una sola vez y no se guarda legible en nuestros sistemas. No la comparta: quien la tenga puede "
                "activarla. Si la pierde, solicite una nueva al soporte de Gigadevs; la anterior quedará anulada.")
        boton = ("Ingresar a Contax", f"{url_erp}/admin/licencia") if url_erp else None
        titulo = "Su licencia de Contax"
        correo(correo_destino, f"Su licencia Contax ({plan})", pc.texto(titulo, parrafos, boton, ("Clave de licencia", clave), filas, nota),
               html=pc.html(titulo, parrafos, url_erp or "https://contax", "Contax", "Tu empresa en orden. Tu fisco al día.", boton,
                            ("Clave de licencia", clave), filas, nota))
        con.execute(update(licencias).where(licencias.c.id == lic_id).values(clave_enviada_en=reloj()))
        st.log(con, lic_id, "clave_enviada", correo_destino)

    def entregar_orden(ref, detalle_pago="", ip=None) -> dict:
        """Idempotente: una orden pagada genera UNA licencia y UN correo, aunque el aviso llegue varias veces."""
        with st.eng.begin() as con:
            o = con.execute(select(ordenes).where(ordenes.c.ref == ref).with_for_update()).first()
            if o is None: raise LookupError("Orden inexistente")
            if o.estado == "entregada": return {"estado": "entregada", "licencia_id": o.licencia_id}
            if o.estado not in ("pendiente", "por_verificar", "pagada"): raise ValueError(f"Orden {o.estado}")
            lid = o.licencia_id
            if lid is None:
                lid, clave = st.crear_licencia(o.cliente, o.meses * 31, o.max_usuarios, o.max_empresas, 1, o.plan, o.max_equipos, o.correo,
                                               o.organizacion_ref, "pago", con=con, modulos=o.modulos)
                con.execute(update(ordenes).where(ordenes.c.id == o.id).values(estado="pagada", pagada_en=reloj(), licencia_id=lid,
                                                                               detalle_pago=str(detalle_pago)[:2000]))
                st.log(con, lid, "licencia_por_pago", f"orden={ref}", ip)
            else:
                clave = st.rotar_clave(lid, con)  # reintento tras un fallo de correo: nueva clave, la anterior queda anulada
            lic = con.execute(select(licencias).where(licencias.c.id == lid)).first()
            enviar_clave(con, lid, clave, o.correo, o.cliente, o.plan, lic.vence_en, f"Recibimos su pago (orden {ref}). Esta es su licencia:")
            con.execute(update(ordenes).where(ordenes.c.id == o.id).values(estado="entregada", entregada_en=reloj()))
        try:  # segundo correo: la factura. Si falla (p. ej. sin rango de control) la licencia ya está entregada y el Maestro factura después.
            facturar_orden(ref)
        except Exception as e:  # noqa: BLE001
            with st.eng.begin() as c: st.log(c, lid, "factura_pendiente", f"orden={ref} {str(e)[:200]}")
        return {"estado": "entregada", "licencia_id": lid}

    # ------------------------------------------------------------------ API admin (Maestro), firmada
    def autenticar():
        if not secreto_admin: return False
        cab = request.headers.get("X-ERP-Firma", "")
        partes = dict(p.split("=", 1) for p in cab.split(",") if "=" in p)
        t, n, f = partes.get("t", ""), partes.get("n", ""), partes.get("f", "")
        if not (t.isdigit() and re.fullmatch(r"[0-9a-f]{32}", n) and re.fullmatch(r"[0-9a-f]{64}", f)): return False
        if abs(reloj() - int(t)) > TOLERANCIA_FIRMA_S: return False
        base = "\n".join([t, n, request.method, request.full_path.rstrip("?"), hashlib.sha256(request.get_data()).hexdigest()])
        if not hmac.compare_digest(hmac.new(secreto_admin.encode(), base.encode(), hashlib.sha256).hexdigest(), f): return False
        return st.usar_nonce(n)  # cada firma sirve una sola vez

    @app.before_request
    def proteger_admin():
        if request.path.startswith("/admin/") and not autenticar():
            return jsonify(message="No autorizado"), 401

    def cuerpo():
        d = request.get_json(silent=True)
        if not isinstance(d, dict): raise ValueError("JSON inválido")
        return d

    @app.errorhandler(ValueError)
    def _valor(e): return jsonify(message=str(e)[:200]), 400

    @app.errorhandler(LookupError)
    def _nf(e): return jsonify(message=str(e)[:200]), 404

    @app.get("/admin/v1/licencias")
    def a_listar():
        return jsonify(licencias=st.listar(request.args.get("org") or None))

    @app.post("/admin/v1/licencias")
    def a_crear():
        d = cuerpo()
        if not RE_CORREO.match(str(d.get("correo", ""))): raise ValueError("Indique el correo al que se enviará la clave")
        with st.eng.begin() as con:
            lid, clave = st.crear_licencia(d.get("cliente") or "Cliente", d.get("dias", 365), d.get("max_usuarios", 5), d.get("max_empresas", 10),
                                           d.get("max_instancias", 1), d.get("plan", "ERP_CONTABLE"), d.get("max_equipos"), d["correo"],
                                           d.get("organizacion_ref"), "maestro", con=con, modulos=d.get("modulos"))
            st.log(con, lid, "creada_por_maestro", d.get("autor", ""))
            venta = d.get("venta") or {}
            if venta.get("monto"):
                mon = venta.get("moneda", "USD")
                if mon not in ("VES", "USD", "EUR", "USDT"): raise ValueError("Moneda de la venta inválida")
                rif = str(venta.get("cliente_rif") or "").strip().upper()
                if not re.fullmatch(r"[VEJPGC]-\d{8}-\d", rif): raise ValueError("RIF del cliente inválido")
                con.execute(update(licencias).where(licencias.c.id == lid).values(
                    venta_monto=_dec(venta["monto"], "monto de la venta"), venta_moneda=mon, venta_tasa=_tasa(venta["tasa"]) if venta.get("tasa") else None,
                    venta_forma_pago=str(venta.get("forma_pago") or "")[:120] or None, cliente_rif=rif))
            lic = con.execute(select(licencias).where(licencias.c.id == lid)).first()
            enviar_clave(con, lid, clave, d["correo"], lic.cliente, lic.plan, lic.vence_en, "Se emitió una licencia para su organización:")
        factura = None
        if lic.venta_monto or (d.get("venta") or {}).get("monto"):
            try:
                factura = facturar_licencia(lid)   # segundo correo: la factura
            except Exception as e:  # noqa: BLE001 — la licencia ya está entregada; queda pendiente de factura
                with st.eng.begin() as c: st.log(c, lid, "factura_pendiente", str(e)[:200])
                factura = {"error": str(e)[:200]}
        return jsonify(id=lid, factura=factura), 201

    @app.patch("/admin/v1/licencias/<int:lid>")
    def a_modificar(lid):
        d = cuerpo()
        cambios = st.modificar(lid, d)
        with st.eng.begin() as con: st.log(con, lid, "modificada", f"{sorted(cambios)} por {d.get('autor', '')}")
        return jsonify(cambios={k: str(v) for k, v in cambios.items()})

    @app.post("/admin/v1/licencias/<int:lid>/liberar")
    def a_liberar(lid):
        st.liberar(lid)
        with st.eng.begin() as con: st.log(con, lid, "instalaciones_liberadas", cuerpo().get("autor", ""))
        return jsonify(ok=True)

    @app.post("/admin/v1/licencias/<int:lid>/reenviar")
    def a_reenviar(lid):
        with st.eng.begin() as con:
            lic = con.execute(select(licencias).where(licencias.c.id == lid).with_for_update()).first()
            if lic is None: raise LookupError("Licencia inexistente")
            destino = cuerpo().get("correo") or lic.correo
            if not RE_CORREO.match(str(destino or "")): raise ValueError("La licencia no tiene correo de entrega")
            clave = st.rotar_clave(lid, con)
            enviar_clave(con, lid, clave, destino, lic.cliente, lic.plan, lic.vence_en, "Se generó una clave NUEVA para su licencia (la anterior quedó anulada):")
        return jsonify(ok=True)

    @app.get("/admin/v1/licencias/<int:lid>/eventos")
    def a_eventos(lid):
        return jsonify(eventos=st.eventos(lid))

    @app.get("/admin/v1/precios")
    def a_precios():
        return jsonify(precios=st.precios())

    @app.put("/admin/v1/precios")
    def a_fijar_precio():
        d = cuerpo()
        st.fijar_precio(d.get("plan"), d.get("meses"), d.get("monto_usd"), d.get("max_usuarios"), d.get("max_empresas"), d.get("max_equipos"))
        return jsonify(ok=True)

    @app.post("/admin/v1/precios/referencia")
    def a_sembrar_precios():
        """Vuelve a cargar los precios de referencia del mercado venezolano SOLO donde falten (no altera los existentes)."""
        return jsonify(agregados=st.sembrar_referencia(forzar=True))

    @app.get("/admin/v1/combos")
    def a_combos():
        return jsonify(combos=st.combos(request.args.get("activos") == "1"), precios_modulo=st.precios_modulos(),
                       descuento_anual_pct=str(DESCUENTO_ANUAL_PCT))

    @app.post("/admin/v1/combos")
    def a_crear_combo():
        d = cuerpo()
        cid = st.crear_combo(d.get("nombre"), d.get("modulos"), d.get("meses"), d.get("monto_usd"), d.get("max_usuarios", 5), d.get("max_empresas", 2), d.get("max_equipos", 5))
        return jsonify(id=cid), 201

    @app.patch("/admin/v1/combos/<int:cid>")
    def a_activar_combo(cid):
        st.activar_combo(cid, bool(cuerpo().get("activo")))
        return jsonify(ok=True)

    @app.get("/admin/v1/combos/sugerido")
    def a_precio_sugerido():
        return jsonify(monto_usd=str(st.precio_sugerido(request.args.get("modulos", ""), request.args.get("meses", 1))))

    @app.put("/admin/v1/precios-modulo")
    def a_precio_modulo():
        d = cuerpo()
        st.fijar_precio_modulo(d.get("modulo"), d.get("mensual_usd"))
        return jsonify(ok=True)

    @app.post("/admin/v1/demo")
    def a_demo():
        """Demo gratuita de 72 h (Facturación + Inventario). UNA por organización, por RIF y por correo."""
        d = cuerpo()
        org = str(d.get("organizacion_ref") or "")
        if not re.fullmatch(r"[0-9]{1,12}", org): raise ValueError("Organización inválida")
        correo_d = str(d.get("correo") or "").strip().lower()
        if not RE_CORREO.match(correo_d): raise ValueError("Correo inválido")
        rif = str(d.get("cliente_rif") or "").strip().upper()
        if rif and not re.fullmatch(r"[VEJPGC]-\d{8}-\d", rif): raise ValueError("RIF inválido")
        with st.eng.begin() as con:
            mismos = [licencias.c.organizacion_ref == org, licencias.c.correo == correo_d] + ([licencias.c.cliente_rif == rif] if rif else [])
            previa = select(licencias.c.id).where(licencias.c.plan == "DEMO", or_(*mismos))
            if con.execute(previa).first(): raise ValueError("Su organización ya utilizó su demo de 72 horas. Compre una licencia para continuar.")
            lid, clave = st.crear_licencia(d.get("cliente") or "Cliente", 3, DEMO_TOPES["max_usuarios"], DEMO_TOPES["max_empresas"], 1, "DEMO",
                                           DEMO_TOPES["max_equipos"], correo_d, org, "demo", con=con)
            if rif: con.execute(update(licencias).where(licencias.c.id == lid).values(cliente_rif=rif))
            st.log(con, lid, "demo_emitida", d.get("autor", ""))
            lic = con.execute(select(licencias).where(licencias.c.id == lid)).first()
            enviar_clave(con, lid, clave, correo_d, lic.cliente, "Demo 72 h — Facturación e Inventario", lic.vence_en,
                         "Su demo gratuita de 72 horas (módulos de Facturación e Inventario) está lista. Vence exactamente 72 horas después de emitida:")
        return jsonify(id=lid, vence_en=lic.vence_en), 201

    @app.post("/admin/v1/ordenes")
    def a_crear_orden():
        """El ERP pide una orden en nombre del Administrador de un cliente. El MONTO lo fija el catálogo del servidor."""
        d = cuerpo()
        moneda, combo = d.get("moneda"), None
        if d.get("combo_id") not in (None, ""):   # combo personalizado: plan PERSONALIZADO con los módulos del combo
            with st.eng.connect() as c:
                combo = c.execute(select(combos).where(combos.c.id == _entero(d["combo_id"], "combo", 1, 10**9), combos.c.activo == 1)).first()
            if combo is None: raise ValueError("Ese combo no está a la venta")
            plan, meses = "PERSONALIZADO", combo.meses
        else:
            plan, meses = d.get("plan"), _entero(d.get("meses"), "meses", 1, 60)
            if plan not in PLANES_TIENDA: raise ValueError("Plan o moneda inválidos")
        if moneda not in ("USDT", "VES"): raise ValueError("Plan o moneda inválidos")
        if not RE_CORREO.match(str(d.get("correo", ""))): raise ValueError("Correo inválido")
        org = str(d.get("organizacion_ref") or "")[:40]
        if not re.fullmatch(r"[0-9]{1,12}", org): raise ValueError("Organización inválida")
        if combo is not None:
            p = combo
        else:
            with st.eng.connect() as c:
                p = c.execute(select(precios).where(precios.c.plan == plan, precios.c.meses == meses)).first()
            if p is None: raise ValueError("Ese plan/período no está a la venta")
        tasa = _tasa(d.get("tasa_bcv")) if moneda == "VES" else (_tasa(d["tasa_bcv"]) if d.get("tasa_bcv") else None)
        cliente_rif = str(d.get("cliente_rif") or "").strip().upper()[:15]
        if cliente_rif and not re.fullmatch(r"[VEJPGC]-\d{8}-\d", cliente_rif): raise ValueError("RIF del cliente inválido")
        monto = p.monto_usd if moneda == "USDT" else (p.monto_usd * tasa).quantize(Decimal("0.01"), ROUND_HALF_UP)
        ref = "ERP" + secrets.token_hex(8).upper()
        fila = dict(ref=ref, organizacion_ref=org, cliente=str(d.get("cliente") or "Cliente")[:200], correo=d["correo"], plan=plan, meses=meses,
                    max_usuarios=p.max_usuarios, max_empresas=p.max_empresas, max_equipos=p.max_equipos, moneda=moneda, monto=monto,
                    monto_usd=p.monto_usd, tasa_bcv=tasa, proveedor="binance" if moneda == "USDT" else "pago_movil", estado="pendiente", creada_en=reloj(),
                    cliente_rif=cliente_rif or None, cliente_direccion=str(d.get("cliente_direccion") or "")[:300] or None,
                    combo_id=combo.id if combo is not None else None, modulos=combo.modulos if combo is not None else None)
        resp = {"ref": ref, "monto": str(monto), "moneda": moneda}
        if moneda == "USDT":
            if binance is None or not cfg.get("binance_pay_id"): raise ValueError("El cobro en USDT no está habilitado")
            exacto = monto_unico(p.monto_usd)
            expira = reloj() + VIGENCIA_ORDEN_USDT_S
            instr = (f"Envíe EXACTAMENTE {exacto} USDT por Binance Pay al Pay ID {cfg['binance_pay_id']}"
                     + (f", o deposítelos por la red {cfg['usdt_red']} a la dirección {cfg['usdt_direccion']}" if cfg.get("usdt_direccion") else "")
                     + f". El monto exacto identifica su orden: no lo redondee. Válido hasta {time.strftime('%d/%m/%Y %H:%M', time.gmtime(expira))} UTC.")
            fila.update(monto_esperado=exacto, expira_en=expira, instrucciones=instr)
            resp.update(monto_esperado=str(exacto), instrucciones=instr, expira_en=expira)
        else:
            resp["instrucciones"] = cfg.get("pago_movil") or "Solicite los datos de pago móvil al soporte."
        with st.eng.begin() as c: c.execute(insert(ordenes).values(**fila))
        return jsonify(resp), 201

    @app.get("/admin/v1/ordenes")
    def a_ordenes():
        q = select(ordenes).order_by(ordenes.c.id.desc()).limit(300)
        if request.args.get("org"): q = q.where(ordenes.c.organizacion_ref == request.args["org"])
        with st.eng.connect() as c:
            return jsonify(ordenes=[{k: (str(v) if isinstance(v, Decimal) else v) for k, v in r._mapping.items() if k not in ("detalle_pago",)} for r in c.execute(q)])

    @app.post("/admin/v1/ordenes/<ref>/referencia")
    def a_referencia(ref):
        """El cliente informa su pago móvil; queda «por verificar» hasta que el Maestro lo confirme contra el banco."""
        if not RE_REF.match(ref): raise ValueError("Referencia inválida")
        d = cuerpo()
        refpago = str(d.get("referencia", "")).strip()
        if not re.fullmatch(r"[0-9A-Za-z-]{4,30}", refpago): raise ValueError("Número de referencia inválido")
        with st.eng.begin() as c:
            n = c.execute(update(ordenes).where(ordenes.c.ref == ref, ordenes.c.proveedor == "pago_movil", ordenes.c.estado == "pendiente",
                                                ordenes.c.organizacion_ref == str(d.get("organizacion_ref")))
                          .values(estado="por_verificar", referencia_pago=json.dumps({"referencia": refpago, "banco": str(d.get("banco", ""))[:60],
                                                                                      "telefono": str(d.get("telefono", ""))[:20]}))).rowcount
        if not n: raise LookupError("Orden no disponible")
        return jsonify(ok=True)

    @app.post("/admin/v1/ordenes/<ref>/confirmar")
    def a_confirmar(ref):
        if not RE_REF.match(ref): raise ValueError("Referencia inválida")
        with st.eng.connect() as c:
            o = c.execute(select(ordenes).where(ordenes.c.ref == ref)).first()
        if o is None: raise LookupError("Orden inexistente")
        if o.estado not in ("pendiente", "por_verificar", "pagada", "entregada"): raise ValueError(f"Orden {o.estado}")  # entregada: doble clic inofensivo
        return jsonify(entregar_orden(ref, f"confirmada por {cuerpo().get('autor', '')}"))

    @app.post("/admin/v1/ordenes/<ref>/rechazar")
    def a_rechazar(ref):
        d = cuerpo()
        with st.eng.begin() as c:
            n = c.execute(update(ordenes).where(ordenes.c.ref == ref, ordenes.c.estado.in_(("pendiente", "por_verificar")))
                          .values(estado="rechazada", motivo=str(d.get("motivo", ""))[:300])).rowcount
        if not n: raise LookupError("Orden no disponible")
        return jsonify(ok=True)

    @app.post("/admin/v1/ordenes/<ref>/verificar")
    def a_verificar(ref):
        """Busca el pago de la orden en el historial de Binance (no se espera a la revisión periódica)."""
        return jsonify(verificar_binance(ref))

    def monto_unico(base) -> Decimal:
        """Precio del catálogo + 0,0001…0,0999 USDT, distinto de toda orden en USDT aún pendiente: identifica el pago."""
        with st.eng.connect() as c:
            ocupados = {r[0] for r in c.execute(select(ordenes.c.monto_esperado).where(
                ordenes.c.proveedor == "binance", ordenes.c.estado.in_(("pendiente", "pagada")), ordenes.c.expira_en > reloj()))}
        for _ in range(200):
            m = (Decimal(base) + Decimal(secrets.randbelow(999) + 1) / Decimal(10000)).quantize(Decimal("0.0001"))
            if m not in ocupados: return m
        raise ValueError("Demasiadas órdenes en USDT pendientes; intente en unos minutos")

    def leer_pagos(desde_s, hasta_s):
        desde_ms, hasta_ms = int(desde_s * 1000), int(hasta_s * 1000)
        pagos = binance.pagos_recibidos(desde_ms, hasta_ms)
        if cfg.get("usdt_direccion"):
            pagos += binance.depositos_recibidos(desde_ms, hasta_ms, cfg.get("usdt_red"))
        return pagos

    def verificar_binance(ref, pagos=None):
        """Empareja la orden con un ingreso en USDT de monto EXACTO, posterior a la orden y antes de su vencimiento.
        Cada transacción de Binance solo puede pagar una orden (clave primaria en lic_pagos_usados)."""
        if not RE_REF.match(ref) or binance is None: raise ValueError("Orden inválida")
        with st.eng.connect() as c:
            o = c.execute(select(ordenes).where(ordenes.c.ref == ref)).first()
            ya = c.execute(select(pagos_usados).where(pagos_usados.c.orden_ref == ref)).first()
        if o is None: raise LookupError("Orden inexistente")
        if o.proveedor != "binance": raise ValueError("No es una orden en USDT")
        if o.estado == "entregada": return {"estado": "entregada"}
        if o.estado not in ("pendiente", "pagada"): return {"estado": o.estado}
        ahora = reloj()
        if ya is None:
            hasta = min(ahora, o.expira_en or ahora)
            if pagos is None: pagos = leer_pagos(o.creada_en - 120, hasta)
            for pg in sorted(pagos, key=lambda x: x["ts"]):
                if pg["monto"] != o.monto_esperado or not (o.creada_en - 120 <= pg["ts"] / 1000 <= hasta): continue
                try:
                    with st.eng.begin() as c:
                        c.execute(insert(pagos_usados).values(pago_id=pg["id"], orden_ref=ref, monto=pg["monto"], registrado_en=ahora))
                        st.log(c, None, "pago_usdt_identificado", f"orden={ref} {pg['origen']} {pg['id']} pagador={pg['pagador']}")
                    ya = pg; break
                except IntegrityError:
                    continue  # esa transacción ya pagó otra orden
        if ya is None:
            if o.expira_en and ahora > o.expira_en:
                with st.eng.begin() as c:
                    c.execute(update(ordenes).where(ordenes.c.ref == ref, ordenes.c.estado == "pendiente").values(estado="vencida"))
                return {"estado": "vencida"}
            return {"estado": o.estado, "binance": "pago aún no recibido"}
        pid = ya["id"] if isinstance(ya, dict) else ya.pago_id
        return entregar_orden(ref, json.dumps({"binance": pid, "monto": str(o.monto_esperado)}))

    def revisar_pendientes():
        """Revisión periódica: una sola consulta a Binance para todas las órdenes en USDT pendientes."""
        if binance is None: return 0
        with st.eng.connect() as c:
            pend = c.execute(select(ordenes).where(ordenes.c.proveedor == "binance", ordenes.c.estado.in_(("pendiente", "pagada")))).all()
        if not pend: return 0
        ahora = reloj()
        try:
            pagos = leer_pagos(min(o.creada_en for o in pend) - 120, ahora)
        except Exception as e:  # noqa: BLE001 — se reintenta en la próxima vuelta
            with st.eng.begin() as c: st.log(c, None, "binance_no_disponible", str(e)[:200])
            return 0
        n = 0
        for o in pend:
            try:
                if verificar_binance(o.ref, pagos).get("estado") == "entregada": n += 1
            except Exception as e:  # noqa: BLE001 — p. ej. correo caído: la orden queda pagada y se reintenta
                with st.eng.begin() as c: st.log(c, o.licencia_id, "entrega_pendiente", f"orden={o.ref} {str(e)[:150]}")
        return n

    # ------------------------------------------------------------------ facturación de Gigadevs
    from . import facturas as fx
    from app import plantilla_correo as pc

    def enviar_factura(fid, intro=None):
        if correo is None: raise RuntimeError("Correo no configurado")
        with st.eng.connect() as c:
            f = fx.como_dict(c.execute(select(fx.facturas).where(fx.facturas.c.id == fid)).first())
        if not f.get("cliente_correo"): raise ValueError("La factura no tiene correo del cliente")
        intro = intro or (f"Estimado(a) {f['cliente_nombre']}: adjuntamos la factura de su compra. Su clave de licencia llegó en un correo aparte."
                          if f.get("orden_ref") else f"Estimado(a) {f['cliente_nombre']}: le enviamos su factura.")
        correo(f["cliente_correo"], f"Factura N° {f['numero_completo']} · Contax", fx_texto(f, intro), html=pc.factura_html(f, cfg.get("url_erp") or "", intro))
        with st.eng.begin() as c:
            c.execute(update(fx.facturas).where(fx.facturas.c.id == fid).values(correo_enviado_en=reloj()))
        return f

    def fx_texto(f, intro): return intro + "\n\n" + pc.factura_texto(f)

    def facturar_orden(ref):
        """Factura de una orden pagada (idempotente: una orden, una factura)."""
        with st.eng.begin() as con:
            o = con.execute(select(ordenes).where(ordenes.c.ref == ref).with_for_update()).first()
            if o is None: raise LookupError("Orden inexistente")
            if o.estado != "entregada": raise ValueError("Solo se facturan órdenes pagadas y entregadas")
            ya = con.execute(select(fx.facturas.c.id).where(fx.facturas.c.orden_ref == ref)).scalar()
            if ya: return ya
            if not o.cliente_rif: raise ValueError("La orden no tiene el RIF del cliente")
            usdt = o.moneda == "USDT"
            f = fx.emitir(con, cliente_nombre=o.cliente, cliente_rif=o.cliente_rif, cliente_direccion=o.cliente_direccion, cliente_correo=o.correo,
                          moneda="USDT" if usdt else "VES", tasa_bcv=o.tasa_bcv, monto_con_iva=(o.monto_esperado or o.monto) if usdt else o.monto,
                          concepto=_concepto(o.plan, o.modulos, meses=o.meses), forma_pago="USDT por Binance" if usdt else "Pago móvil (Bs)",
                          orden_ref=ref, licencia_id=o.licencia_id, reloj=reloj)
            st.log(con, o.licencia_id, "factura_emitida", f"orden={ref} factura={f['serie']}{f['numero']} control={f['numero_control']}")
        enviar_factura(f["id"])
        return f["id"]

    def _concepto(plan, modulos, meses=None, dias=None):
        from app.planes import MODULOS
        nombre = {"ERP_CONTABLE": "Contax Contable", "CASH": "Cash — Facturación", "ERP_ADMIN": "Contax Administrativo", "ALL_IN_ONE": "All in One",
                  "DEMO": "Demo 72 h"}.get(plan, plan or "Contax")
        if plan == "PERSONALIZADO": nombre = "Personalizado (" + ", ".join(MODULOS.get(m, m) for m in (modulos or "").split(",") if m) + ")"
        tiempo = f"{meses} mes(es)" if meses else (f"{dias} días" if dias else "")
        return f"Licencia de uso Contax — plan {nombre}" + (f", {tiempo}" if tiempo else "")

    def facturar_licencia(lid):
        """Factura de una licencia emitida por el Maestro con venta registrada (idempotente)."""
        with st.eng.begin() as con:
            l = con.execute(select(licencias).where(licencias.c.id == lid).with_for_update()).first()
            if l is None: raise LookupError("Licencia inexistente")
            ya = con.execute(select(fx.facturas.c.id).where(fx.facturas.c.licencia_id == lid, fx.facturas.c.estado == "Emitida")).scalar()
            if ya: return ya
            if not l.venta_monto: raise ValueError("Esa licencia no tiene una venta registrada")
            f = fx.emitir(con, cliente_nombre=l.cliente, cliente_rif=l.cliente_rif, cliente_direccion=None, cliente_correo=l.correo,
                          moneda=l.venta_moneda or "USD", tasa_bcv=l.venta_tasa, monto_con_iva=l.venta_monto,
                          concepto=_concepto(l.plan, l.modulos, dias=l.dias), forma_pago=l.venta_forma_pago, licencia_id=lid, reloj=reloj)
            st.log(con, lid, "factura_emitida", f"factura={f['serie']}{f['numero']} control={f['numero_control']}")
        enviar_factura(f["id"])
        return f["id"]

    def pendientes_de_factura(org=None):
        """Ventas sin factura: órdenes pagadas y licencias del Maestro con venta registrada."""
        with st.eng.connect() as c:
            facturadas_o = {r[0] for r in c.execute(select(fx.facturas.c.orden_ref).where(fx.facturas.c.orden_ref.is_not(None)))}
            facturadas_l = {r[0] for r in c.execute(select(fx.facturas.c.licencia_id).where(fx.facturas.c.licencia_id.is_not(None), fx.facturas.c.estado == "Emitida"))}
            qo = select(ordenes).where(ordenes.c.estado == "entregada")
            ql = select(licencias).where(licencias.c.venta_monto.is_not(None), licencias.c.origen == "maestro")
            if org: qo, ql = qo.where(ordenes.c.organizacion_ref == str(org)), ql.where(licencias.c.organizacion_ref == str(org))
            out = [{"tipo": "orden", "ref": o.ref, "organizacion_ref": o.organizacion_ref, "cliente": o.cliente, "concepto": _concepto(o.plan, o.modulos, meses=o.meses),
                    "moneda": "USDT" if o.moneda == "USDT" else "VES", "monto": str(o.monto_esperado or o.monto), "fecha": o.entregada_en}
                   for o in c.execute(qo) if o.ref not in facturadas_o]
            out += [{"tipo": "licencia", "ref": str(l.id), "organizacion_ref": l.organizacion_ref, "cliente": l.cliente, "concepto": _concepto(l.plan, l.modulos, dias=l.dias),
                     "moneda": l.venta_moneda, "monto": str(l.venta_monto), "fecha": l.creada_en} for l in c.execute(ql) if l.id not in facturadas_l]
        return sorted(out, key=lambda x: x["fecha"] or 0)

    # ------------------------------------------------------------------ presupuestos de Gigadevs
    from . import presupuestos as px

    def emisor_px():
        with st.eng.connect() as c:
            cf = dict(c.execute(select(fx.config).where(fx.config.c.id == 1)).first()._mapping)
        return {k: (str(cf.get(k)) if cf.get(k) is not None else "") for k in ("razon_social", "rif", "domicilio", "telefono", "correo", "iva_pct")}

    @app.get("/admin/v1/presupuestos")
    def a_px_listar():
        with st.eng.connect() as c:
            return jsonify(presupuestos=[px.como_dict(r) for r in c.execute(select(px.presupuestos).order_by(px.presupuestos.c.id.desc()).limit(500))])

    @app.get("/admin/v1/presupuestos/catalogo")
    def a_px_catalogo():
        return jsonify(catalogo=px.catalogo(st), iva_pct=emisor_px()["iva_pct"] or "16")

    @app.post("/admin/v1/presupuestos")
    def a_px_crear():
        with st.eng.begin() as c:
            pid = px.crear(c, cuerpo(), emisor_px()["iva_pct"] or 16, reloj=reloj)
        return jsonify(id=pid), 201

    @app.get("/admin/v1/presupuestos/<int:pid>")
    def a_px_ver(pid):
        with st.eng.connect() as c:
            r = c.execute(select(px.presupuestos).where(px.presupuestos.c.id == pid)).first()
        if r is None: raise LookupError("Presupuesto inexistente")
        return jsonify(presupuesto=px.como_dict(r), emisor=emisor_px())

    @app.post("/admin/v1/presupuestos/<int:pid>/estado")
    def a_px_estado(pid):
        with st.eng.begin() as c: px.cambiar_estado(c, pid, cuerpo().get("estado"))
        return jsonify(ok=True)

    @app.post("/admin/v1/presupuestos/<int:pid>/enviar")
    def a_px_enviar(pid):
        if correo is None: raise RuntimeError("Correo no configurado")
        with st.eng.connect() as c:
            r = c.execute(select(px.presupuestos).where(px.presupuestos.c.id == pid)).first()
        if r is None: raise LookupError("Presupuesto inexistente")
        p, em = px.como_dict(r), emisor_px()
        destino = str(cuerpo().get("correo") or p.get("cliente_correo") or "").strip().lower()
        if not RE_CORREO.match(destino): raise ValueError("Indique el correo del cliente")
        intro = f"Estimado(a) {p.get('atencion') or p['cliente_nombre']}: le enviamos el presupuesto {p['numero_txt']}. Quedamos atentos a sus comentarios."
        correo(destino, f"Presupuesto {p['numero_txt']} · Contax", intro + "\n\n" + pc.presupuesto_texto(p, em),
               html=pc.presupuesto_html(p, em, cfg.get("url_erp") or "", intro))
        with st.eng.begin() as c:
            c.execute(update(px.presupuestos).where(px.presupuestos.c.id == pid).values(correo_enviado_en=reloj(),
                      estado="Enviado" if p["estado"] == "Emitido" else p["estado"]))
        return jsonify(ok=True)

    def _contrato(con, cid):
        r = con.execute(select(px.contratos).where(px.contratos.c.id == cid)).first()
        if r is None: raise LookupError("Contrato inexistente")
        n = con.execute(select(px.presupuestos.c.numero).where(px.presupuestos.c.id == r.presupuesto_id)).scalar()
        return px.contrato_dict(r, n)

    @app.get("/admin/v1/contratos")
    def a_ct_listar():
        with st.eng.connect() as c:
            ids = [r[0] for r in c.execute(select(px.contratos.c.id).order_by(px.contratos.c.id.desc()).limit(500))]
            return jsonify(contratos=[_contrato(c, i) for i in ids])

    @app.post("/admin/v1/contratos")
    def a_ct_crear():
        with st.eng.begin() as c:
            cid = px.crear_contrato(c, cuerpo(), reloj=reloj)
        return jsonify(id=cid), 201

    @app.get("/admin/v1/contratos/<int:cid>")
    def a_ct_ver(cid):
        with st.eng.connect() as c:
            return jsonify(contrato=_contrato(c, cid), emisor=emisor_px())

    @app.post("/admin/v1/contratos/<int:cid>/estado")
    def a_ct_estado(cid):
        e = cuerpo().get("estado")
        if e not in ("Enviado", "Anulado"): raise ValueError("Estado inválido")
        with st.eng.begin() as c:
            ct = _contrato(c, cid)
            if ct["estado"] == "Firmado" and e != "Anulado": raise ValueError("El contrato ya está firmado")
            c.execute(update(px.contratos).where(px.contratos.c.id == cid).values(estado=e, **({"enviado_en": reloj()} if e == "Enviado" else {})))
        return jsonify(ok=True)

    @app.post("/admin/v1/contratos/<int:cid>/firmar")
    def a_ct_firmar(cid):
        """Aceptación electrónica del cliente (Decreto-Ley sobre Mensajes de Datos y Firmas Electrónicas): queda la huella del texto."""
        d = cuerpo()
        if not re.fullmatch(r"[0-9a-f]{64}", str(d.get("hash") or "")): raise ValueError("Huella inválida")
        nombre, ci = str(d.get("nombre") or "").strip()[:120], str(d.get("ci") or "").strip().upper()[:20]
        if len(nombre) < 5 or not re.fullmatch(r"[VE]?-?\d{5,9}", ci): raise ValueError("Indique su nombre completo y su cédula")
        with st.eng.begin() as c:
            n = c.execute(update(px.contratos).where(px.contratos.c.id == cid, px.contratos.c.estado.in_(("Borrador", "Enviado"))).values(
                estado="Firmado", firmado_en=reloj(), firma_nombre=nombre, firma_ci=ci, firma_ip=str(d.get("ip") or "")[:60],
                firma_agente=str(d.get("agente") or "")[:200], firma_hash=d["hash"])).rowcount
        if not n: raise ValueError("Este contrato ya no admite firma")
        return jsonify(ok=True)

    @app.post("/admin/v1/contratos/<int:cid>/enviar")
    def a_ct_enviar(cid):
        if correo is None: raise RuntimeError("Correo no configurado")
        d = cuerpo()
        with st.eng.connect() as c: ct = _contrato(c, cid)
        destino = str(d.get("correo") or ct.get("cliente_correo") or "").strip().lower()
        if not RE_CORREO.match(destino): raise ValueError("Indique el correo del cliente")
        url = str(d.get("url") or "")
        if not url.startswith("https://"): raise ValueError("Enlace inválido")
        titulo = f"Contrato {ct['numero_txt']} para su firma"
        parrafos = [f"Estimado(a) {ct.get('representante') or ct['cliente_nombre']}:",
                    f"Gigadevs Corp le envía el contrato {ct['numero_txt']} de los servicios de Contax® aceptados en el presupuesto {ct['presupuesto_txt']}.",
                    "Puede leerlo completo y aceptarlo electrónicamente con su nombre y cédula en el siguiente enlace (válido 30 días). La aceptación "
                    "tiene plena validez conforme al Decreto con Fuerza de Ley sobre Mensajes de Datos y Firmas Electrónicas."]
        correo(destino, titulo + " · Contax", pc.texto(titulo, parrafos, ("Leer y firmar el contrato", url)),
               html=pc.html(titulo, parrafos, cfg.get("url_erp") or "", "Contax", "Tu empresa en orden. Tu fisco al día.", ("Leer y firmar el contrato", url)))
        with st.eng.begin() as c:
            c.execute(update(px.contratos).where(px.contratos.c.id == cid, px.contratos.c.estado == "Borrador").values(estado="Enviado", enviado_en=reloj()))
        return jsonify(ok=True)

    @app.get("/admin/v1/facturas/pendientes")
    def a_fx_pendientes():
        return jsonify(pendientes=pendientes_de_factura(request.args.get("org")))

    @app.post("/admin/v1/facturas/cliente")
    def a_fx_cliente():
        """«Construir la factura» eligiendo solo el cliente: se factura cada venta suya pendiente con sus datos reales."""
        org = str(cuerpo().get("organizacion_ref") or "")
        if not re.fullmatch(r"[0-9]{1,12}", org): raise ValueError("Cliente inválido")
        pend = pendientes_de_factura(org)
        if not pend: raise ValueError("Ese cliente no tiene ventas pendientes de factura")
        ids = [facturar_orden(p["ref"]) if p["tipo"] == "orden" else facturar_licencia(int(p["ref"])) for p in pend]
        return jsonify(ids=ids), 201

    @app.get("/admin/v1/facturacion/config")
    def a_fx_config():
        with st.eng.connect() as c:
            d = fx.leer_config(c); ok, rota = fx.verificar_cadena(c)
        return jsonify(config={k: (str(v) if isinstance(v, Decimal) else v) for k, v in d.items()}, cadena_integra=ok, rota_en=rota)

    @app.put("/admin/v1/facturacion/config")
    def a_fx_guardar():
        fx.guardar_config(st.eng, cuerpo()); return jsonify(ok=True)

    @app.post("/admin/v1/facturacion/rango")
    def a_fx_rango():
        d = cuerpo(); fx.nuevo_rango(st.eng, d.get("desde"), d.get("hasta"), d.get("fecha"), d.get("providencia")); return jsonify(ok=True)

    @app.get("/admin/v1/facturas")
    def a_fx_listar():
        with st.eng.connect() as c:
            fs = [fx.como_dict(r) for r in c.execute(select(fx.facturas).order_by(fx.facturas.c.id.desc()).limit(500))]
        return jsonify(facturas=fs)

    @app.get("/admin/v1/facturas/<int:fid>")
    def a_fx_ver(fid):
        with st.eng.connect() as c:
            r = c.execute(select(fx.facturas).where(fx.facturas.c.id == fid)).first()
        if r is None: raise LookupError("Factura inexistente")
        return jsonify(factura=fx.como_dict(r))

    @app.post("/admin/v1/facturas")
    def a_fx_crear():
        """«Construir la factura»: el Maestro emite una factura manual (líneas sin IVA) o la de una licencia (monto con IVA)."""
        d = cuerpo()
        correo_cli = str(d.get("cliente_correo") or "").strip().lower() or None
        if correo_cli and not RE_CORREO.match(correo_cli): raise ValueError("Correo del cliente inválido")
        with st.eng.begin() as con:
            f = fx.emitir(con, cliente_nombre=str(d.get("cliente_nombre") or "").strip() or "Cliente", cliente_rif=str(d.get("cliente_rif") or ""),
                          cliente_direccion=d.get("cliente_direccion"), cliente_correo=correo_cli, moneda=d.get("moneda", "USD"), tasa_bcv=d.get("tasa_bcv"),
                          lineas=d.get("lineas"), monto_con_iva=d.get("monto_con_iva"), concepto=str(d.get("concepto") or "")[:300] or None,
                          forma_pago=d.get("forma_pago"), licencia_id=d.get("licencia_id"), reloj=reloj)
            st.log(con, d.get("licencia_id"), "factura_manual", f"factura={f['serie']}{f['numero']} por {d.get('autor', '')}")
        if correo_cli and d.get("enviar", True): enviar_factura(f["id"])
        return jsonify(id=f["id"]), 201

    @app.post("/admin/v1/facturas/<int:fid>/reenviar")
    def a_fx_reenviar(fid):
        enviar_factura(fid); return jsonify(ok=True)

    @app.post("/admin/v1/facturas/<int:fid>/anular")
    def a_fx_anular(fid):
        from app.tiempo import hoy_ve
        motivo = str(cuerpo().get("motivo") or "").strip()[:300]
        if not motivo: raise ValueError("Indique el motivo de la anulación")
        with st.eng.begin() as c:
            f = c.execute(select(fx.facturas).where(fx.facturas.c.id == fid).with_for_update()).first()
            if f is None: raise LookupError("Factura inexistente")
            if f.estado != "Emitida": raise ValueError("La factura ya está anulada")
            if f.fecha != str(hoy_ve()): raise ValueError("Solo se anula el mismo día de emisión; después corresponde una nota de crédito")
            c.execute(update(fx.facturas).where(fx.facturas.c.id == fid).values(estado="Anulada", motivo_anulacion=motivo, anulada_en=reloj()))
        return jsonify(ok=True)

    @app.post("/admin/v1/ordenes/<ref>/facturar")
    def a_fx_orden(ref):
        if not RE_REF.match(ref): raise ValueError("Referencia inválida")
        return jsonify(id=facturar_orden(ref))

    @app.errorhandler(fx.ErrorFacturaPlataforma)
    def _efx(e): return jsonify(message=str(e)[:300]), 400

    app.extensions["facturar_orden"] = facturar_orden
    app.extensions["verificar_binance"] = verificar_binance
    app.extensions["revisar_pendientes"] = revisar_pendientes
    app.extensions["entregar_orden"] = entregar_orden
    return app
