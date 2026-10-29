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
from sqlalchemy import (Column, Float, ForeignKey, Integer, MetaData, Numeric, String, Table, Text, create_engine, func,
                        insert, select, update)
from sqlalchemy.exc import IntegrityError

from app.licensing.token import firmar

VIGENCIA_ORDEN_USDT_S = 24 * 3600    # una orden en USDT espera su pago 24 h
VIGENCIA_TOKEN_S = 7 * 24 * 3600   # ventana offline máxima: 7 días
LIMITE_PETICIONES = (20, 60)        # 20 peticiones por 60 s y por IP (endpoints públicos)
TOLERANCIA_FIRMA_S = 300
PLANES = ("ERP_CONTABLE", "CASH", "ERP_ADMIN", "ALL_IN_ONE", "PERSONALIZADO")
PLANES_TIENDA = PLANES[:4]   # PERSONALIZADO se arma por módulos y lo emite el Maestro; no está en el catálogo
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
    Column("cliente_rif", Text), Column("cliente_direccion", Text))
# Cada transacción de Binance paga como máximo UNA orden (la clave primaria lo impone en la base de datos)
pagos_usados = Table(
    "lic_pagos_usados", md,
    Column("pago_id", String(120), primary_key=True), Column("orden_ref", String(32), nullable=False),
    Column("monto", Numeric(18, 8), nullable=False), Column("registrado_en", Float, nullable=False))
COLUMNAS_NUEVAS = {"lic_licencias": [("modulos", "TEXT"), ("venta_monto", "NUMERIC(18,4)"), ("venta_moneda", "VARCHAR(4)"), ("venta_tasa", "NUMERIC(18,6)"),
                                     ("venta_forma_pago", "TEXT"), ("cliente_rif", "TEXT"), ("dias", "INTEGER")], "lic_ordenes": [("monto_esperado", "NUMERIC(18,4)"), ("expira_en", "FLOAT"), ("instrucciones", "TEXT"),
                                   ("cliente_rif", "TEXT"), ("cliente_direccion", "TEXT")]}


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
        fx.instalar(self.eng)
        from sqlalchemy import inspect, text
        with self.eng.begin() as c:  # bases creadas antes: se agregan las columnas nuevas (nombres fijos, no hay datos del usuario)
            for tabla, cols in COLUMNAS_NUEVAS.items():
                existentes = {x["name"] for x in inspect(c).get_columns(tabla)}
                for nombre, tipo in cols:
                    if nombre not in existentes: c.execute(text(f"ALTER TABLE {tabla} ADD COLUMN {nombre} {tipo}"))

    # ---------------- licencias ----------------
    def crear_licencia(self, cliente, dias=365, max_usuarios=5, max_empresas=10, max_instancias=1, plan="ERP_CONTABLE",
                       max_equipos=None, correo=None, organizacion_ref=None, origen="maestro", con=None, modulos=None) -> tuple[int, str]:
        if plan not in PLANES: raise ValueError(f"Plan inválido: {plan}")
        modulos = _modulos(modulos) if plan == "PERSONALIZADO" else None
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
                    "Para activarla, ingrese al ERP con su usuario Administrador, vaya a Organización → Licencia, pegue la clave "
                    "en «Activar licencia» y pulse Activar."]
        filas = [("Plan", plan), ("Vigente hasta", time.strftime("%d/%m/%Y", time.gmtime(vence_en)))]
        nota = ("Esta clave se envía una sola vez y no se guarda legible en nuestros sistemas. No la comparta: quien la tenga puede "
                "activarla. Si la pierde, solicite una nueva al soporte de Gigadevs; la anterior quedará anulada.")
        boton = ("Ingresar al ERP", f"{url_erp}/admin/licencia") if url_erp else None
        titulo = "Su licencia de ERP"
        correo(correo_destino, f"Su licencia ERP ({plan})", pc.texto(titulo, parrafos, boton, ("Clave de licencia", clave), filas, nota),
               html=pc.html(titulo, parrafos, url_erp or "https://erp", "ERP", "Tu empresa en orden. Tu fisco al día.", boton,
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
                                               o.organizacion_ref, "pago", con=con)
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

    @app.post("/admin/v1/ordenes")
    def a_crear_orden():
        """El ERP pide una orden en nombre del Administrador de un cliente. El MONTO lo fija el catálogo del servidor."""
        d = cuerpo()
        plan, meses, moneda = d.get("plan"), _entero(d.get("meses"), "meses", 1, 60), d.get("moneda")
        if plan not in PLANES_TIENDA or moneda not in ("USDT", "VES"): raise ValueError("Plan o moneda inválidos")
        if not RE_CORREO.match(str(d.get("correo", ""))): raise ValueError("Correo inválido")
        org = str(d.get("organizacion_ref") or "")[:40]
        if not re.fullmatch(r"[0-9]{1,12}", org): raise ValueError("Organización inválida")
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
                    cliente_rif=cliente_rif or None, cliente_direccion=str(d.get("cliente_direccion") or "")[:300] or None)
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
        correo(f["cliente_correo"], f"Factura N° {f['numero_completo']} · ERP", fx_texto(f, intro), html=pc.factura_html(f, cfg.get("url_erp") or "", intro))
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
                          concepto=_concepto(o.plan, None, meses=o.meses), forma_pago="USDT por Binance" if usdt else "Pago móvil (Bs)",
                          orden_ref=ref, licencia_id=o.licencia_id, reloj=reloj)
            st.log(con, o.licencia_id, "factura_emitida", f"orden={ref} factura={f['serie']}{f['numero']} control={f['numero_control']}")
        enviar_factura(f["id"])
        return f["id"]

    def _concepto(plan, modulos, meses=None, dias=None):
        from app.planes import MODULOS
        nombre = {"ERP_CONTABLE": "ERP Contable", "CASH": "Cash — Facturación", "ERP_ADMIN": "ERP Administrativo", "ALL_IN_ONE": "All in One"}.get(plan, plan or "ERP")
        if plan == "PERSONALIZADO": nombre = "Personalizado (" + ", ".join(MODULOS.get(m, m) for m in (modulos or "").split(",") if m) + ")"
        tiempo = f"{meses} mes(es)" if meses else (f"{dias} días" if dias else "")
        return f"Licencia de uso ERP — plan {nombre}" + (f", {tiempo}" if tiempo else "")

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
            out = [{"tipo": "orden", "ref": o.ref, "organizacion_ref": o.organizacion_ref, "cliente": o.cliente, "concepto": _concepto(o.plan, None, meses=o.meses),
                    "moneda": "USDT" if o.moneda == "USDT" else "VES", "monto": str(o.monto_esperado or o.monto), "fecha": o.entregada_en}
                   for o in c.execute(qo) if o.ref not in facturadas_o]
            out += [{"tipo": "licencia", "ref": str(l.id), "organizacion_ref": l.organizacion_ref, "cliente": l.cliente, "concepto": _concepto(l.plan, l.modulos, dias=l.dias),
                     "moneda": l.venta_moneda, "monto": str(l.venta_monto), "fecha": l.creada_en} for l in c.execute(ql) if l.id not in facturadas_l]
        return sorted(out, key=lambda x: x["fecha"] or 0)

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
