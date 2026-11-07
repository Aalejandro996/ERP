"""Servidor de licencias del PROVEEDOR (servicio aparte del ERP; en Render: `erp-licencias`).

Responsabilidades:
  * /v1/activate y /v1/refresh — emite tokens Ed25519 atados a una instalación (plan, topes, equipos).
  * /admin/v1/...             — API del usuario Maestro (la consume el ERP por la red privada). Cada petición va firmada
                                con HMAC-SHA256 (LICENSE_ADMIN_SECRET) + marca de tiempo + nonce de un solo uso.
  * Órdenes de pago           — USDT con cuenta personal de Binance (monto único, validado leyendo el historial con una clave
                                de SOLO LECTURA) y bolívares por Ávila Pay (checkout alojado, confirmación automática) o, si
                                Ávila Pay no está configurado, por pago móvil (verificación del Maestro).
  * Entrega única por correo  — la clave se genera al entregar, se envía UNA vez por Brevo y solo se guarda su SHA-256.

Persistencia: PostgreSQL (o SQLite en pruebas) con SQLAlchemy Core; todo SQL va parametrizado (sin inyecciones).
"""
import base64
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

from . import avilapay as AV

VIGENCIA_ORDEN_USDT_S = 24 * 3600    # una orden en USDT espera su pago 24 h
VIGENCIA_ORDEN_AVILA_S = 30 * 60     # la sesión de Ávila Pay vence a los 30 min
VIGENCIA_ORDEN_AVILA_USD_S = 24 * 3600   # Zelle o tarjeta internacional: el pago espera 24 h
PROVEEDORES_AVILA = ("avilapay", "avilapay_usd")
# Tarifario de lista de Ávila Pay (Portal → Gateway → Tarifas): comisión por método + IGTF 3 %. El Maestro las ajusta si negocia otras.
# El precio se sube lo justo para que, descontadas comisión e impuesto, llegue completo el precio del catálogo:
#   total = precio / (1 − (comisión + impuesto)),  ajuste = total − precio.  El cliente ve solo «ajuste por forma de pago».
COMISIONES_LISTA = {"pago_movil": "1.00", "debito": "1.50", "credito": "1.00", "tdc": "3.90",
                    "zelle": "2.00", "tarjeta": "4.20", "billetera": "2.90"}
IMPUESTO_LISTA = "3.00"   # IGTF
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
    Column("cliente_rif", Text), Column("cliente_direccion", Text), Column("combo_id", Integer), Column("modulos", Text),
    Column("metodo", Text),   # Ávila Pay: el método que eligió el cliente (pago_movil, zelle, tarjeta…)
    Column("ajuste", Numeric(18, 2)))   # ajuste por forma de pago (comisión de la pasarela) incluido en «monto»
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
# Ávila Pay: una sola fila (id=1) que el Maestro configura desde su consola. Clave y secreto van cifrados (Fernet con una
# clave derivada de LICENSE_ADMIN_SECRET): la base nunca los guarda legibles. Lo demás sirve para monitorear la pasarela.
avila_cfg = Table(
    "lic_avilapay", md,
    Column("id", Integer, primary_key=True), Column("api_key_cifrada", Text), Column("secreto_cifrado", Text),
    Column("activa", Integer, nullable=False, default=1), Column("metodos", Text), Column("actualizado_en", Float), Column("actualizado_por", Text),
    Column("ultimo_aviso_en", Float), Column("ultimo_aviso", Text), Column("ultimo_error_en", Float), Column("ultimo_error", Text),
    # dólares por Ávila Pay (Zelle y tarjeta internacional): misma clave, aviso con su propio secreto
    Column("activa_usd", Integer), Column("metodos_usd", Text), Column("secreto_usd_cifrado", Text))
# Binance (USDT): la clave de SOLO LECTURA y el Pay ID se cargan en la consola Maestro (cifrados). Sin fila: variables de entorno.
binance_cfg = Table(
    "lic_binance", md,
    Column("id", Integer, primary_key=True), Column("api_key_cifrada", Text), Column("secreto_cifrado", Text), Column("pay_id", Text),
    Column("red", Text), Column("direccion", Text), Column("activa", Integer, nullable=False, default=1), Column("actualizado_en", Float),
    Column("actualizado_por", Text), Column("ultima_revision_en", Float), Column("ultimo_error_en", Float), Column("ultimo_error", Text))
meta = Table("lic_meta", md, Column("clave", String(40), primary_key=True), Column("valor", Text))
# Cada transacción de Binance paga como máximo UNA orden (la clave primaria lo impone en la base de datos)
pagos_usados = Table(
    "lic_pagos_usados", md,
    Column("pago_id", String(120), primary_key=True), Column("orden_ref", String(32), nullable=False),
    Column("monto", Numeric(18, 8), nullable=False), Column("registrado_en", Float, nullable=False))

# Suscripciones de los productos que administra el Maestro. ERP conserva su modelo
# anterior; estos registros son genéricos y no comparten datos de negocio con los productos.
PRODUCTOS = {
    "criteriseg": {"nombre": "CriteriSeg", "tipo_sujeto": "organizacion", "entornos": ["nube", "cerrado"], "primario": "#17365D", "acento": "#2F6FAD",
                   "planes": [("institucional", "Licencia institucional", "Cotizar")], "modulos": [
                       ("planificacion", "Planificación de auditoría"), ("trabajo_campo", "Trabajo de campo y evidencias"),
                       ("cumplimiento", "Cumplimiento normativo"), ("informes", "Informes y planes de acción"), ("analitica", "Analítica y dashboards")]},
    "bot_whatsapp": {"nombre": "Enlace MIT", "tipo_sujeto": "organizacion", "entornos": ["nube", "cerrado"], "primario": "#0033A0", "acento": "#F39200",
                      "planes": [("organizacion", "Licencia por organización", "Cotizar")], "modulos": [
                          ("sesiones", "Sesiones y conexiones"), ("envio_mensajes", "Envío de mensajes"),
                          ("historial", "Historial de mensajes"), ("incidencias", "Incidencias y seguimiento")]},
    "surtia": {"nombre": "Surtia", "tipo_sujeto": "organizacion", "entornos": ["nube"], "primario": "#0B2530", "acento": "#16C79A",
               "planes": [("empresa", "Licencia por empresa", "Cotizar")], "modulos": [
                   ("bot_pedidos", "Bot de pedidos por WhatsApp"), ("presupuestos", "Presupuestos"),
                   ("conversaciones", "Conversaciones y atención"), ("ordenes_compra", "Lectura de órdenes de compra (PDF)"),
                   ("integracion", "Integración con el sistema administrativo"), ("avisos", "Avisos por correo")]},
    "control_financiero": {"nombre": "Quincena VE", "tipo_sujeto": "cuenta", "entornos": ["nube"], "primario": "#0B6B4D", "acento": "#4FC79C",
                            "planes": [("individual", "Cuenta individual", "Cotizar")], "modulos": [
                                ("inicio", "Panel de finanzas"), ("movimientos", "Ingresos y gastos"),
                                ("deudas", "Deudas y compromisos"), ("pagos", "Pagos programados"), ("negocio", "Herramientas de negocio")]},
}
productos = Table(
    "lic_productos", md,
    Column("id", String(40), primary_key=True), Column("nombre", String(80), nullable=False),
    Column("tipo_sujeto", String(20), nullable=False), Column("entornos", Text, nullable=False),
    Column("primario", String(7), nullable=False), Column("acento", String(7), nullable=False),
    Column("activo", Integer, nullable=False, default=1))
planes_producto = Table(
    "lic_planes_producto", md,
    Column("producto_id", String(40), ForeignKey("lic_productos.id"), primary_key=True),
    Column("codigo", String(32), primary_key=True), Column("nombre", String(100), nullable=False),
    Column("precio_estado", String(20), nullable=False, default="Cotizar"), Column("activo", Integer, nullable=False, default=1),
    # precios (USD, IVA incluido), límites y módulos del plan; las bases anteriores los reciben en comercial.instalar()
    Column("descripcion", Text), Column("precio_mensual", Numeric(18, 2)), Column("precio_anual", Numeric(18, 2)), Column("precio_unico", Numeric(18, 2)),
    Column("max_usuarios", Integer), Column("max_empresas", Integer), Column("max_equipos", Integer), Column("max_instancias", Integer),
    Column("modulos", Text))
suscripciones = Table(
    "lic_suscripciones", md,
    Column("id", Integer, primary_key=True), Column("producto_id", String(40), ForeignKey("lic_productos.id"), nullable=False),
    Column("cliente", String(200), nullable=False), Column("correo", String(254), nullable=False),
    Column("tipo_sujeto", String(20), nullable=False), Column("referencia_sujeto", String(200), nullable=False),
    Column("plan", String(32), nullable=False), Column("estado", String(12), nullable=False, default="activa"),
    Column("entorno", String(12), nullable=False), Column("max_usuarios", Integer, nullable=False, default=1),
    Column("max_instancias", Integer, nullable=False, default=1), Column("max_empresas", Integer, nullable=False, default=1),
    Column("max_equipos", Integer, nullable=False, default=1), Column("modulos", Text), Column("creada_en", Float, nullable=False),
    Column("inicia_en", Float, nullable=False), Column("vence_en", Float, nullable=False), Column("notas", Text))
COLUMNAS_NUEVAS = {"lic_licencias": [("modulos", "TEXT"), ("venta_monto", "NUMERIC(18,4)"), ("venta_moneda", "VARCHAR(4)"), ("venta_tasa", "NUMERIC(18,6)"),
                                     ("venta_forma_pago", "TEXT"), ("cliente_rif", "TEXT"), ("dias", "INTEGER")], "lic_ordenes": [("monto_esperado", "NUMERIC(18,4)"), ("expira_en", "FLOAT"), ("instrucciones", "TEXT"),
                                   ("cliente_rif", "TEXT"), ("cliente_direccion", "TEXT"), ("combo_id", "INTEGER"), ("modulos", "TEXT"),
                                   ("metodo", "TEXT"), ("ajuste", "NUMERIC(18,2)")],
                 "lic_avilapay": [("activa_usd", "INTEGER"), ("metodos_usd", "TEXT"), ("secreto_usd_cifrado", "TEXT")],
                 "lic_suscripciones": [("modulos", "TEXT")],
                 "lic_presupuestos": [("sistema", "VARCHAR(40)")], "lic_contratos": [("sistema", "VARCHAR(40)")], "lic_facturas": [("sistema", "VARCHAR(40)")]}


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


def _modulos_producto(producto_id, valores=None):
    cfg = PRODUCTOS.get(producto_id)
    if not cfg: raise ValueError("Producto inválido")
    permitidos = {codigo for codigo, _ in cfg["modulos"]}
    elegidos = permitidos if valores is None else {str(x).strip() for x in valores if str(x).strip()}
    if not elegidos or not elegidos.issubset(permitidos): raise ValueError("Seleccione al menos un módulo válido para el sistema")
    return ",".join(sorted(elegidos))


class Almacen:
    """Acceso a datos del servidor de licencias. Todo SQL es SQLAlchemy Core parametrizado."""

    def __init__(self, url, reloj=time.time):
        if url.startswith("postgres://"): url = "postgresql+psycopg://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"): url = "postgresql+psycopg://" + url[len("postgresql://"):]
        self.eng = create_engine(url, pool_pre_ping=True)
        self.reloj = reloj
        md.create_all(self.eng)
        self.sembrar_productos()
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

    def sembrar_productos(self):
        """Registra el catálogo sin fijar precios públicos; esos productos se cotizan."""
        with self.eng.begin() as c:
            for pid, cfg in PRODUCTOS.items():
                if not c.execute(select(productos.c.id).where(productos.c.id == pid)).first():
                    c.execute(insert(productos).values(id=pid, nombre=cfg["nombre"], tipo_sujeto=cfg["tipo_sujeto"],
                        entornos=json.dumps(cfg["entornos"]), primario=cfg["primario"], acento=cfg["acento"], activo=1))
                else:
                    c.execute(update(productos).where(productos.c.id == pid).values(nombre=cfg["nombre"], tipo_sujeto=cfg["tipo_sujeto"],
                        entornos=json.dumps(cfg["entornos"]), primario=cfg["primario"], acento=cfg["acento"]))
                for code, name, price_state in cfg["planes"]:
                    if not c.execute(select(planes_producto.c.codigo).where(planes_producto.c.producto_id == pid, planes_producto.c.codigo == code)).first():
                        c.execute(insert(planes_producto).values(producto_id=pid, codigo=code, nombre=name, precio_estado=price_state, activo=1))

    def listar_productos(self):
        with self.eng.connect() as c:
            rows = c.execute(select(productos).order_by(productos.c.nombre)).mappings().all()
            result = []
            for row in rows:
                d = dict(row); d["entornos"] = json.loads(d["entornos"])
                d["modulos"] = [{"codigo": code, "nombre": name} for code, name in PRODUCTOS[d["id"]]["modulos"]]
                d["planes"] = [dict(p) for p in c.execute(select(planes_producto).where(planes_producto.c.producto_id == d["id"]).order_by(planes_producto.c.nombre)).mappings()]
                result.append(d)
            return result

    def crear_plan_producto(self, producto_id, codigo, nombre):
        if producto_id not in PRODUCTOS: raise ValueError("Producto inválido")
        codigo = str(codigo or "").strip().lower()
        nombre = str(nombre or "").strip()[:100]
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,30}", codigo): raise ValueError("Use un código corto en minúsculas, sin espacios")
        if not nombre: raise ValueError("Indique el nombre del plan")
        with self.eng.begin() as c:
            if c.execute(select(planes_producto.c.codigo).where(planes_producto.c.producto_id == producto_id, planes_producto.c.codigo == codigo)).first():
                raise ValueError("Ya existe un plan con ese código para el producto")
            c.execute(insert(planes_producto).values(producto_id=producto_id, codigo=codigo, nombre=nombre, precio_estado="Cotizar", activo=1))
        return {"producto_id": producto_id, "codigo": codigo, "nombre": nombre, "precio_estado": "Cotizar", "activo": 1}

    def activar_plan_producto(self, producto_id, codigo, activo):
        with self.eng.begin() as c:
            if not c.execute(update(planes_producto).where(planes_producto.c.producto_id == producto_id,
                planes_producto.c.codigo == codigo).values(activo=1 if activo else 0)).rowcount:
                raise LookupError("Plan inexistente")

    def crear_suscripcion(self, producto_id, cliente, correo, referencia_sujeto, plan, dias=365, entorno="nube",
                          max_usuarios=1, max_instancias=1, max_empresas=1, max_equipos=1, notas="", modulos=None):
        cfg = next((p for p in self.listar_productos() if p["id"] == producto_id and p["activo"]), None)
        if cfg is None: raise ValueError("Producto inválido o inactivo")
        if not RE_CORREO.match(str(correo or "")): raise ValueError("Correo inválido")
        if entorno not in cfg["entornos"]: raise ValueError("Entorno no permitido para este producto")
        if plan not in {p["codigo"] for p in cfg["planes"] if p["activo"]}: raise ValueError("Plan inválido o inactivo para este producto")
        modulos = _modulos_producto(producto_id, modulos)
        cliente = str(cliente or "").strip()[:200]
        referencia_sujeto = str(referencia_sujeto or "").strip()[:200]
        if not cliente or not referencia_sujeto: raise ValueError("Indique el cliente y el identificador de la organización o cuenta")
        dias = _entero(dias, "días", 1, 3650); ahora = self.reloj()
        limites = {"max_usuarios": _entero(max_usuarios, "usuarios", 1, 100000),
                   "max_instancias": _entero(max_instancias, "instancias", 1, 1000),
                   "max_empresas": _entero(max_empresas, "empresas", 1, 100000),
                   "max_equipos": _entero(max_equipos, "equipos", 1, 100000)}
        if cfg["tipo_sujeto"] == "cuenta" and limites["max_usuarios"] != 1:
            raise ValueError("Control financiero se licencia por una cuenta individual")
        with self.eng.begin() as c:
            existente = c.execute(select(suscripciones.c.id).where(suscripciones.c.producto_id == producto_id,
                suscripciones.c.referencia_sujeto == referencia_sujeto, suscripciones.c.estado == "activa",
                suscripciones.c.vence_en > ahora)).first()
            if existente: raise ValueError("Ya existe una suscripción activa para esa cuenta u organización")
            sid = c.execute(insert(suscripciones).values(producto_id=producto_id, cliente=cliente, correo=correo.strip().lower(),
                tipo_sujeto=cfg["tipo_sujeto"], referencia_sujeto=referencia_sujeto, plan=plan, estado="activa", entorno=entorno,
                **limites, modulos=modulos, creada_en=ahora, inicia_en=ahora, vence_en=ahora + dias * 86400, notas=str(notas or "")[:1000]).returning(suscripciones.c.id)).scalar()
        return sid

    def listar_suscripciones(self, producto_id=None):
        q = select(suscripciones, productos.c.nombre.label("producto_nombre"), productos.c.primario, productos.c.acento).join(productos, productos.c.id == suscripciones.c.producto_id).order_by(suscripciones.c.id.desc())
        if producto_id: q = q.where(suscripciones.c.producto_id == producto_id)
        with self.eng.connect() as c: return [dict(r) for r in c.execute(q).mappings()]

    def cambiar_suscripcion(self, sid, estado=None, dias_extra=None, notas=None, plan=None, entorno=None,
                             max_usuarios=None, max_instancias=None, max_empresas=None, max_equipos=None):
        if estado is not None and estado not in ESTADOS: raise ValueError("Estado inválido")
        cambios = {}
        with self.eng.begin() as c:
            row = c.execute(select(suscripciones).where(suscripciones.c.id == int(sid)).with_for_update()).first()
            if row is None: raise LookupError("Suscripción inexistente")
            cfg = next((p for p in self.listar_productos() if p["id"] == row.producto_id), None)
            if plan not in (None, ""):
                if cfg is None or plan not in {p["codigo"] for p in cfg["planes"] if p["activo"]}: raise ValueError("Plan inválido o inactivo")
                cambios["plan"] = plan
            if entorno not in (None, ""):
                if cfg is None or entorno not in cfg["entornos"]: raise ValueError("Entorno no permitido para este producto")
                cambios["entorno"] = entorno
            for k, val, label, maximum in (("max_usuarios", max_usuarios, "usuarios", 100000),
                    ("max_instancias", max_instancias, "instancias", 1000), ("max_empresas", max_empresas, "empresas", 100000),
                    ("max_equipos", max_equipos, "equipos", 100000)):
                if val not in (None, ""): cambios[k] = _entero(val, label, 1, maximum)
            if row.tipo_sujeto == "cuenta" and cambios.get("max_usuarios", row.max_usuarios) != 1:
                raise ValueError("Control financiero se licencia por una cuenta individual")
            if estado is not None: cambios["estado"] = estado
            if dias_extra not in (None, ""): cambios["vence_en"] = max(row.vence_en, self.reloj()) + _entero(dias_extra, "días", 1, 3650) * 86400
            if notas is not None: cambios["notas"] = str(notas)[:1000]
            if cambios: c.execute(update(suscripciones).where(suscripciones.c.id == int(sid)).values(**cambios))
        return cambios

    def token_suscripcion(self, sid, clave_privada):
        row = next((s for s in self.listar_suscripciones() if s["id"] == int(sid)), None)
        if row is None: raise LookupError("Suscripción inexistente")
        if row["estado"] != "activa" or row["vence_en"] <= self.reloj(): raise ValueError("Solo se puede exportar una suscripción activa y vigente")
        ahora = int(self.reloj())
        modulos = row.get("modulos") or _modulos_producto(row["producto_id"])
        claims = {"producto": row["producto_id"], "suscripcion": row["id"], "cliente": row["cliente"],
                  "tipo_sujeto": row["tipo_sujeto"], "sujeto": row["referencia_sujeto"], "plan": row["plan"],
                  "estado": row["estado"], "entorno": row["entorno"], "iat": ahora, "exp": int(row["vence_en"]),
                  "vence_suscripcion": int(row["vence_en"]), "modulos": modulos.split(","),
                  "limites": {k: row[k] for k in ("max_usuarios", "max_instancias", "max_empresas", "max_equipos")}}
        return firmar(claims, clave_privada)

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
def create_app(db_url: str, clave_privada, reloj=time.time, secreto_admin: str | None = None, binance=None, correo=None, config=None,
               avilapay_transporte=None, binance_transporte=None) -> Flask:
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

    @app.get("/v1/clave-publica")
    def clave_publica():
        """Clave PÚBLICA Ed25519 (no es secreta): con ella el relé de Binance verifica que las consultas vienen de aquí."""
        from cryptography.hazmat.primitives import serialization
        raw = clave_privada.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return jsonify(ed25519=base64.urlsafe_b64encode(raw).decode().rstrip("="))

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

    # Clientes, planes con precio y órdenes de los demás sistemas, y la API firmada de cada sistema (licensing_server/comercial.py)
    from . import comercial as CM
    from . import facturas as _fx
    com = CM.Comercial(st, PRODUCTOS)
    app.extensions["comercial"] = com
    CM.registrar_rutas(app, st, com, cfg.get("sync") or {}, clave_privada, lambda con, **k: _fx.emitir(con, reloj=reloj, **k), cuerpo)

    @app.get("/admin/v1/productos")
    def a_productos():
        return jsonify(productos=st.listar_productos())

    @app.post("/admin/v1/productos/<producto_id>/planes")
    def a_crear_plan_producto(producto_id):
        d = cuerpo()
        plan = st.crear_plan_producto(producto_id, d.get("codigo"), d.get("nombre"))
        with st.eng.begin() as con: st.log(con, None, "plan_producto_creado", f"{producto_id}/{plan['codigo']} por {d.get('autor', '')}")
        return jsonify(plan=plan), 201

    @app.patch("/admin/v1/productos/<producto_id>/planes/<codigo>")
    def a_estado_plan_producto(producto_id, codigo):
        d = cuerpo()
        if not isinstance(d.get("activo"), bool): raise ValueError("El estado del plan debe ser booleano")
        st.activar_plan_producto(producto_id, codigo, d["activo"])
        with st.eng.begin() as con: st.log(con, None, "plan_producto_estado", f"{producto_id}/{codigo} activo={d['activo']} por {d.get('autor', '')}")
        return jsonify(ok=True)

    @app.get("/admin/v1/suscripciones")
    def a_suscripciones():
        return jsonify(suscripciones=st.listar_suscripciones(request.args.get("producto") or None))

    @app.post("/admin/v1/suscripciones")
    def a_crear_suscripcion():
        d = cuerpo()
        sid = st.crear_suscripcion(d.get("producto_id"), d.get("cliente"), d.get("correo"), d.get("referencia_sujeto"),
            d.get("plan"), d.get("dias", 365), d.get("entorno", "nube"), d.get("max_usuarios", 1),
            d.get("max_instancias", 1), d.get("max_empresas", 1), d.get("max_equipos", 1), d.get("notas", ""), d.get("modulos"))
        with st.eng.begin() as con: st.log(con, None, "suscripcion_creada", f"#{sid} producto={d.get('producto_id')} por {d.get('autor', '')}")
        return jsonify(id=sid), 201

    @app.patch("/admin/v1/suscripciones/<int:sid>")
    def a_cambiar_suscripcion(sid):
        d = cuerpo()
        cambios = st.cambiar_suscripcion(sid, d.get("estado"), d.get("dias_extra"), d.get("notas") if "notas" in d else None,
            d.get("plan"), d.get("entorno"), d.get("max_usuarios"), d.get("max_instancias"), d.get("max_empresas"), d.get("max_equipos"))
        with st.eng.begin() as con: st.log(con, None, "suscripcion_modificada", f"#{sid} {sorted(cambios)} por {d.get('autor', '')}")
        return jsonify(cambios=cambios)

    @app.get("/admin/v1/suscripciones/<int:sid>/token")
    def a_token_suscripcion(sid):
        token = st.token_suscripcion(sid, clave_privada)
        with st.eng.begin() as con: st.log(con, None, "token_suscripcion_emitido", f"id={sid}", request.remote_addr or "?")
        return jsonify(token=token)

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
        metodo_usd = metodo_ves = None
        if isinstance(moneda, str) and moneda.startswith("VES:"):   # bolívares con el método elegido en Contax
            moneda, metodo_ves = "VES", moneda[4:]
            if metodo_ves not in AV.METODOS: raise ValueError("Método de pago inválido")
        if moneda in AV.METODOS_USD:          # «zelle» / «tarjeta»: dólares por Ávila Pay
            moneda, metodo_usd = "USD", moneda
        elif moneda == "USD":
            metodo_usd = str(d.get("metodo") or "")
            if metodo_usd not in AV.METODOS_USD: raise ValueError("Elija Zelle o tarjeta internacional")
        if moneda not in ("USDT", "VES", "USD"): raise ValueError("Plan o moneda inválidos")
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
        monto = p.monto_usd if moneda in ("USDT", "USD") else (p.monto_usd * tasa).quantize(Decimal("0.01"), ROUND_HALF_UP)
        ref = "ERP" + secrets.token_hex(8).upper()
        fila = dict(ref=ref, organizacion_ref=org, cliente=str(d.get("cliente") or "Cliente")[:200], correo=d["correo"], plan=plan, meses=meses,
                    max_usuarios=p.max_usuarios, max_empresas=p.max_empresas, max_equipos=p.max_equipos, moneda=moneda, monto=monto,
                    monto_usd=p.monto_usd, tasa_bcv=tasa, proveedor="binance" if moneda == "USDT" else "pago_movil", estado="pendiente", creada_en=reloj(),
                    cliente_rif=cliente_rif or None, cliente_direccion=str(d.get("cliente_direccion") or "")[:300] or None,
                    combo_id=combo.id if combo is not None else None, modulos=combo.modulos if combo is not None else None)
        resp = {"ref": ref, "monto": str(monto), "moneda": moneda}
        if moneda == "USD":
            av, conf = avila_cliente(solo_activa=False)
            habilitados = [m for m in ((conf.metodos_usd if conf is not None else "") or "").split(",") if m]
            if av is None or conf is None or not conf.activa_usd or metodo_usd not in habilitados:
                raise ValueError(f"El cobro con {AV.METODOS_USD[metodo_usd][1]} no está habilitado")
            retorno = f"{cfg['url_erp']}/admin/licencia?orden={ref}" if cfg.get("url_erp") else None
            total, ajuste = ajustar(monto, [metodo_usd])
            try:
                pg = av.crear_pago_usd(total, ref, f"Licencia Contax · orden {ref}", [metodo_usd], correo=d["correo"], return_url=retorno, cancel_url=retorno)
            except AV.ErrorAvila as e:
                avila_error(f"crear cobro USD {ref}: {e}"); raise ValueError(f"Ávila Pay no respondió; intente de nuevo en unos minutos ({e})") from e
            nombre = AV.METODOS_USD[metodo_usd][1]
            instr = (f"Pague US$ {total} con {nombre} en la página segura de Ávila Pay. La confirmación es automática y la clave llega a su "
                     "correo en cuanto se apruebe el pago.")
            fila.update(proveedor="avilapay_usd", metodo=metodo_usd, checkout_url=pg["url"], prepay_id=pg["id"], expira_en=reloj() + VIGENCIA_ORDEN_AVILA_USD_S,
                        instrucciones=instr, monto=total, ajuste=ajuste)
            resp.update(checkout_url=pg["url"], instrucciones=instr, proveedor="avilapay_usd", metodo=metodo_usd, monto=str(total), ajuste=str(ajuste),
                        precio=str(monto))
        elif moneda == "USDT":
            b = bn()
            if b["cuenta"] is None or not b["activa"] or not b["pay_id"]: raise ValueError("El cobro en USDT no está habilitado")
            exacto = monto_unico(p.monto_usd)
            expira = reloj() + VIGENCIA_ORDEN_USDT_S
            instr = (f"Envíe EXACTAMENTE {exacto} USDT por Binance Pay al Pay ID {b['pay_id']}"
                     + (f", o deposítelos por la red {b['red']} a la dirección {b['direccion']}" if b["direccion"] else "")
                     + f". El monto exacto identifica su orden: no lo redondee. Válido hasta {time.strftime('%d/%m/%Y %H:%M', time.gmtime(expira))} UTC.")
            fila.update(monto_esperado=exacto, expira_en=expira, instrucciones=instr)
            resp.update(monto_esperado=str(exacto), instrucciones=instr, expira_en=expira)
        else:
            av, conf = avila_cliente(solo_activa=True)
            if av is not None:   # Ávila Pay: checkout alojado y confirmación automática; si falla, queda el pago móvil manual
                url_erp, url_pub = cfg.get("url_erp") or "", cfg.get("url_publica") or ""
                retorno = f"{url_erp}/admin/licencia?orden={ref}" if url_erp else None
                habilitados = [m for m in (conf.metodos or "").split(",") if m in AV.METODOS] or list(AV.METODOS)
                if metodo_ves and metodo_ves not in habilitados: raise ValueError("Ese método de pago no está habilitado")
                elegidos = [metodo_ves] if metodo_ves else habilitados
                total, ajuste = ajustar(monto, elegidos)   # sin método elegido: el ajuste del método más caro habilitado
                try:
                    s = av.crear_sesion(total, ref, f"Licencia Contax · orden {ref}", correo=d["correo"], nombre=fila["cliente"], return_url=retorno,
                                        cancel_url=retorno, webhook_url=f"{url_pub}/v1/avilapay/aviso" if url_pub else None, metodos=elegidos)
                    como = AV.METODOS[metodo_ves] if metodo_ves else "Pago Móvil, débito, crédito inmediato o tarjeta nacional"
                    instr = (f"Pague Bs {total} con {como} en la página segura de Ávila Pay. "
                             "La confirmación es automática y la clave llega a su correo en cuanto el banco confirme. El enlace vence en 30 minutos.")
                    fila.update(proveedor="avilapay", metodo=metodo_ves, checkout_url=s["url"], prepay_id=s["session_key"], expira_en=reloj() + VIGENCIA_ORDEN_AVILA_S,
                                instrucciones=instr, monto=total, ajuste=ajuste)
                    resp.update(checkout_url=s["url"], instrucciones=instr, expira_en=fila["expira_en"], proveedor="avilapay", monto=str(total), ajuste=str(ajuste),
                                precio=str(monto))
                except AV.ErrorAvila as e:
                    avila_error(f"crear cobro {ref}: {e}")
            if fila["proveedor"] == "pago_movil":
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
        """Busca el pago de la orden en Binance o consulta su cobro en Ávila Pay (no se espera a la revisión periódica)."""
        if not RE_REF.match(ref): raise ValueError("Referencia inválida")
        with st.eng.connect() as c:
            prov = c.execute(select(ordenes.c.proveedor).where(ordenes.c.ref == ref)).scalar()
        if prov is None: raise LookupError("Orden inexistente")
        return jsonify(verificar_avilapay(ref) if prov in PROVEEDORES_AVILA else verificar_binance(ref))

    # ------------------------------------------------------------------ Ávila Pay (bolívares)
    from cryptography.fernet import Fernet, InvalidToken
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    fernet = Fernet(base64.urlsafe_b64encode(HKDF(hashes.SHA256(), 32, b"contax-avilapay", b"configuracion-pasarela").derive(secreto_admin.encode()))) \
        if secreto_admin else None

    def avila_conf():
        with st.eng.connect() as c:
            return c.execute(select(avila_cfg).where(avila_cfg.c.id == 1)).first()

    def avila_firma(r, crudo, preferir_usd):
        """None si la firma es válida; si no, el motivo para el registro (sin revelar secretos). Ávila Pay usa
        «X-AvilaPay-Signature» (bolívares) y «AvilaPay-Signature» (dólares): se acepta cualquiera, con cualquiera de los
        dos secretos guardados (por si se pegó en la página del otro)."""
        firma = request.headers.get("AvilaPay-Signature") or request.headers.get("X-AvilaPay-Signature") or ""
        secretos = [x for x in ((descifrar(r.secreto_usd_cifrado), descifrar(r.secreto_cifrado)) if preferir_usd
                                else (descifrar(r.secreto_cifrado), descifrar(r.secreto_usd_cifrado))) if x] if r is not None else []
        if not secretos: return "no hay secreto de avisos cargado en la consola (webhook secret)"
        if not firma: return "el aviso no trae la cabecera de firma (AvilaPay-Signature)"
        if any(AV.firma_valida(x, crudo, firma) for x in secretos): return None
        return "la firma no coincide con el secreto cargado: vuelva a copiar el webhook secret del portal"

    def descifrar(v):
        if not v or fernet is None: return ""
        try:
            return fernet.decrypt(v.encode()).decode()
        except InvalidToken:
            return ""   # cambió LICENSE_ADMIN_SECRET: el Maestro debe volver a cargar la clave

    def avila_cliente(solo_activa=True):
        """(cliente, fila de configuración). Pausada: no se crean cobros nuevos, pero los ya creados se siguen verificando."""
        r = avila_conf()
        if r is None or (solo_activa and not r.activa): return None, r
        try:
            return AV.AvilaPay(descifrar(r.api_key_cifrada), avilapay_transporte), r
        except AV.ErrorAvila:
            return None, r

    def avila_marcar(**valores):
        with st.eng.begin() as c:
            if not c.execute(update(avila_cfg).where(avila_cfg.c.id == 1).values(**valores)).rowcount:
                c.execute(insert(avila_cfg).values(**{"id": 1, "activa": 0, **valores}))

    def avila_error(texto):
        avila_marcar(ultimo_error_en=reloj(), ultimo_error=str(texto)[:300])
        with st.eng.begin() as c: st.log(c, None, "avilapay_error", texto)

    def verificar_avilapay(ref, referencia_bancaria=None):
        """Confirma el cobro LEYENDO a Ávila Pay (el aviso solo dispara la consulta): pagado, en la moneda de la orden (Bs, o USD
        para Zelle y tarjeta internacional) y por el monto EXACTO. Cada cobro de Ávila Pay paga como máximo una orden."""
        if not RE_REF.match(ref): raise ValueError("Referencia inválida")
        with st.eng.connect() as c:
            o = c.execute(select(ordenes).where(ordenes.c.ref == ref)).first()
        if o is None: raise LookupError("Orden inexistente")
        if o.proveedor not in PROVEEDORES_AVILA: raise ValueError("No es una orden de Ávila Pay")
        if o.estado == "entregada": return {"estado": "entregada"}
        if o.estado not in ("pendiente", "pagada"): return {"estado": o.estado}
        av, _ = avila_cliente(solo_activa=False)
        if av is None: raise ValueError("Ávila Pay no está configurado en la consola Maestro")
        usd = o.proveedor == "avilapay_usd"
        s = av.pago_usd(o.prepay_id) if usd else av.sesion(o.prepay_id)
        if s["estado"] == "REFUNDED":
            with st.eng.begin() as c: st.log(c, o.licencia_id, "avilapay_reembolsado", f"orden={ref}")
            return {"estado": o.estado, "avilapay": "reembolsado"}
        if s["estado"] == ("SUCCEEDED" if usd else "PAID"):
            if s["moneda"] != ("USD" if usd else "VES") or s["monto"] != Decimal(o.monto).quantize(Decimal("0.01")):
                with st.eng.begin() as c:
                    c.execute(update(ordenes).where(ordenes.c.ref == ref, ordenes.c.estado == "pendiente")
                              .values(estado="por_verificar", motivo=f"Ávila Pay informó {s['moneda']} {s['monto']}; la orden es Bs {o.monto}"))
                    st.log(c, None, "avilapay_monto_distinto", f"orden={ref} {s['moneda']} {s['monto']} esperado={o.monto}")
                return {"estado": "por_verificar", "avilapay": "monto distinto: revíselo y confirme a mano"}
            clave_pago = f"{o.proveedor}:{o.prepay_id}"
            try:
                with st.eng.begin() as c:
                    c.execute(insert(pagos_usados).values(pago_id=clave_pago, orden_ref=ref, monto=s["monto"], registrado_en=reloj()))
                    st.log(c, None, "avilapay_pago_confirmado", f"orden={ref} método={s['metodo'] or '—'} ref. bancaria={referencia_bancaria or '—'}")
            except IntegrityError:
                with st.eng.connect() as c:
                    if c.execute(select(pagos_usados.c.orden_ref).where(pagos_usados.c.pago_id == clave_pago)).scalar() != ref:
                        return {"estado": o.estado, "avilapay": "esa sesión ya pagó otra orden"}
            return entregar_orden(ref, json.dumps({"avilapay": o.prepay_id, "metodo": s["metodo"], "referencia_bancaria": referencia_bancaria,
                                                   "pagado_en": s["pagado_en"]}))
        if s["estado"] == "EXPIRED" or (s["estado"] in ("PENDING", "FAILED", "PROCESSING") and o.expira_en and reloj() > o.expira_en + 600 and
                                        (usd or s["estado"] != "PROCESSING")):
            with st.eng.begin() as c:
                c.execute(update(ordenes).where(ordenes.c.ref == ref, ordenes.c.estado == "pendiente").values(estado="vencida"))
            return {"estado": "vencida"}
        return {"estado": o.estado, "avilapay": (AV.ESTADOS_USD if usd else AV.ESTADOS).get(s["estado"], s["estado"])}

    def revisar_avilapay():
        """Respaldo de los avisos: consulta las órdenes de Ávila Pay pendientes (Bs y USD) que aún pueden pagarse."""
        with st.eng.connect() as c:
            pend = c.execute(select(ordenes.c.ref).where(or_(ordenes.c.proveedor == "avilapay", ordenes.c.proveedor == "avilapay_usd"),
                                                         ordenes.c.estado.in_(("pendiente", "pagada")),
                                                         ordenes.c.creada_en > reloj() - VIGENCIA_ORDEN_AVILA_USD_S - 3600)).scalars().all()
        n = 0
        for ref in pend:
            try:
                if verificar_avilapay(ref).get("estado") == "entregada": n += 1
            except Exception as e:  # noqa: BLE001 — Ávila Pay caído o correo caído: se reintenta en la próxima vuelta
                avila_error(f"revisión {ref}: {e}")
        for ref in com.pendientes_avila(reloj() - VIGENCIA_ORDEN_AVILA_USD_S - 3600):   # suscripciones de los demás sistemas
            try:
                if verificar_avila_producto(ref).get("estado") == "Pagada": n += 1
            except Exception as e:  # noqa: BLE001
                avila_error(f"revisión {ref}: {e}")
        return n

    # ------------------------------------------------------------------ suscripciones de los sistemas pagadas con Ávila Pay
    # (p. ej. Negocio Plus de Quincena VE: la persona elige su plan y paga desde la app; aquí se cobra, se crea o renueva la
    # suscripción y se factura con la marca del sistema; la app consulta el resultado y activa la cuenta)
    def checkout_producto(pid, d):
        if PRODUCTOS[pid]["tipo_sujeto"] == "cuenta" and not d.get("nombre"): d = {**d, "nombre": d.get("referencia")}
        cli, _ = com.guardar_cliente(pid, {k: d.get(k) for k in ("referencia", "nombre", "correo", "documento")}, origen="sistema")
        metodo = str(d.get("metodo") or "")
        usd = metodo in AV.METODOS_USD
        if not usd and metodo not in AV.METODOS: raise ValueError("Elija la forma de pago")
        if usd:
            av, conf = avila_cliente(solo_activa=False)
            if av is None or conf is None or not conf.activa_usd or metodo not in ((conf.metodos_usd or "").split(",")):
                raise ValueError(f"El pago con {AV.METODOS_USD[metodo][1]} no está habilitado")
        else:
            av, conf = avila_cliente(solo_activa=True)
            if av is None: raise ValueError("El pago en bolívares con Ávila Pay no está habilitado")
            if metodo not in ([m for m in (conf.metodos or "").split(",") if m in AV.METODOS] or list(AV.METODOS)):
                raise ValueError("Esa forma de pago no está habilitada")
        ref = com.crear_orden(pid, {"cliente_id": cli["id"], "plan": d.get("plan"), "periodo": d.get("periodo"), "moneda": "USD",
                                    "forma_pago": "Ávila Pay", "autor": "app"})
        o = com.orden(ref)
        precio = Decimal(o["monto"])
        desc = f"Suscripción {PRODUCTOS[pid]['nombre']} · {o['plan']} · orden {ref}"
        retorno = str(d.get("retorno") or "")[:300] or None
        if retorno and not retorno.startswith("https://"): raise ValueError("Dirección de regreso inválida")
        try:
            if usd:
                total, ajuste = ajustar(precio, [metodo])
                pg = av.crear_pago_usd(total, ref, desc, [metodo], correo=cli.get("correo"), return_url=retorno, cancel_url=retorno)
                com.marcar_cobro(ref, proveedor="avilapay_usd", metodo=metodo, prepay_id=pg["id"], checkout_url=pg["url"], monto=total, moneda="USD",
                                 precio_usd=precio, ajuste=ajuste, expira_en=reloj() + VIGENCIA_ORDEN_AVILA_USD_S, forma_pago=f"{AV.METODOS_USD[metodo][1]} (Ávila Pay)")
                return {"ref": ref, "checkout_url": pg["url"], "monto": str(total), "moneda": "USD"}
            tasa = _tasa(d.get("tasa_bcv"))
            total, ajuste = ajustar((precio * tasa).quantize(Decimal("0.01"), ROUND_HALF_UP), [metodo])
            url_pub = cfg.get("url_publica") or ""
            ses = av.crear_sesion(total, ref, desc, correo=cli.get("correo"), nombre=cli["nombre"], return_url=retorno, cancel_url=retorno,
                                  webhook_url=f"{url_pub}/v1/avilapay/aviso" if url_pub else None, metodos=[metodo])
            com.marcar_cobro(ref, proveedor="avilapay", metodo=metodo, prepay_id=ses["session_key"], checkout_url=ses["url"], monto=total, moneda="VES",
                             tasa=tasa, precio_usd=precio, ajuste=ajuste, expira_en=reloj() + VIGENCIA_ORDEN_AVILA_S, forma_pago=f"{AV.METODOS[metodo]} (Ávila Pay)")
            return {"ref": ref, "checkout_url": ses["url"], "monto": str(total), "moneda": "VES"}
        except AV.ErrorAvila as e:
            avila_error(f"crear cobro {ref}: {e}"); com.anular(ref, f"Ávila Pay no respondió: {e}")
            raise ValueError(f"Ávila Pay no respondió; intente de nuevo en unos minutos ({e})") from e

    def verificar_avila_producto(ref, referencia_bancaria=None):
        """Igual que con las órdenes de Contax: el pago se confirma LEYENDO a Ávila Pay, por el monto y la moneda exactos, y cada
        cobro de Ávila Pay paga una sola orden."""
        o = com.orden(ref)
        if o["estado"] != "Pendiente" or o.get("proveedor") not in PROVEEDORES_AVILA: return {"estado": o["estado"]}
        av, _ = avila_cliente(solo_activa=False)
        if av is None: raise ValueError("Ávila Pay no está configurado en la consola Maestro")
        usd = o["proveedor"] == "avilapay_usd"
        s_ = av.pago_usd(o["prepay_id"]) if usd else av.sesion(o["prepay_id"])
        if s_["estado"] == ("SUCCEEDED" if usd else "PAID"):
            if s_["moneda"] != ("USD" if usd else "VES") or s_["monto"] != Decimal(o["monto"]).quantize(Decimal("0.01")):
                com.marcar_cobro(ref, nota=f"Ávila Pay informó {s_['moneda']} {s_['monto']}; la orden es {o['moneda']} {o['monto']}: revisar y confirmar a mano")
                with st.eng.begin() as c: st.log(c, None, "avilapay_monto_distinto", f"orden={ref} {s_['moneda']} {s_['monto']}")
                return {"estado": "Pendiente", "avilapay": "monto distinto: Gigadevs lo revisará"}
            clave_pago = f"{o['proveedor']}:{o['prepay_id']}"
            try:
                with st.eng.begin() as c:
                    c.execute(insert(pagos_usados).values(pago_id=clave_pago, orden_ref=ref, monto=s_["monto"], registrado_en=reloj()))
            except IntegrityError:
                with st.eng.connect() as c:
                    if c.execute(select(pagos_usados.c.orden_ref).where(pagos_usados.c.pago_id == clave_pago)).scalar() != ref:
                        return {"estado": "Pendiente", "avilapay": "ese cobro ya pagó otra orden"}
            r = com.pagar(ref, {"forma_pago": o.get("forma_pago"), "referencia_pago": referencia_bancaria or o["prepay_id"], "tasa": o.get("tasa")},
                          lambda con, **k: _fx_emitir(con, reloj=reloj, **k))
            with st.eng.begin() as c: st.log(c, None, "avilapay_pago_confirmado", f"orden={ref} suscripción #{r['suscripcion_id']} {r['suscripcion']}")
            return {"estado": "Pagada", **r}
        if s_["estado"] == "EXPIRED" or (s_["estado"] in ("PENDING", "FAILED") and o.get("expira_en") and reloj() > o["expira_en"] + 600):
            com.anular(ref, "El pago con Ávila Pay venció o no se completó")
            return {"estado": "Anulada"}
        return {"estado": "Pendiente", "avilapay": (AV.ESTADOS_USD if usd else AV.ESTADOS).get(s_["estado"], s_["estado"])}

    from . import facturas as _fx_mod
    _fx_emitir = _fx_mod.emitir

    def metodos_habilitados():
        _, conf = avila_cliente(solo_activa=False)
        if conf is None: return {"bolivares": [], "dolares": []}
        ves = ([m for m in (conf.metodos or "").split(",") if m in AV.METODOS] or list(AV.METODOS)) if conf.activa else []
        usd = [m for m in (conf.metodos_usd or "").split(",") if m in AV.METODOS_USD] if conf.activa_usd else []
        return {"bolivares": [{"codigo": m, "nombre": AV.METODOS[m]} for m in ves], "dolares": [{"codigo": m, "nombre": AV.METODOS_USD[m][1]} for m in usd]}

    def _sync_ok(pid):
        return pid in PRODUCTOS and CM.autenticar_sistema(st, cfg.get("sync") or {}, pid)

    @app.get("/sync/v1/<pid>/planes")
    def s_planes(pid):
        if not _sync_ok(pid): return jsonify(message="No autorizado"), 401
        return jsonify(planes=com.planes_publicos(pid), metodos=metodos_habilitados())

    @app.post("/sync/v1/<pid>/checkout")
    def s_checkout(pid):
        if not _sync_ok(pid): return jsonify(message="No autorizado"), 401
        r = checkout_producto(pid, cuerpo())
        with st.eng.begin() as c: st.log(c, None, "checkout_producto", f"{pid} orden={r['ref']} {r['moneda']} {r['monto']}")
        return jsonify(r), 201

    @app.get("/sync/v1/<pid>/ordenes/<ref>")
    def s_orden(pid, ref):
        if not _sync_ok(pid): return jsonify(message="No autorizado"), 401
        if not CM.RE_ORDEN.match(ref): raise LookupError("Orden inexistente")
        o = com.orden(ref)
        if o["producto_id"] != pid: raise LookupError("Orden inexistente")
        r = verificar_avila_producto(ref) if o["estado"] == "Pendiente" and o.get("proveedor") else {"estado": o["estado"]}
        o = com.orden(ref)
        resp = {"ref": ref, "estado": o["estado"], "plan": o["plan"], "avilapay": r.get("avilapay")}
        if o["estado"] == "Pagada" and o.get("suscripcion_id"):
            sub = next((x for x in st.listar_suscripciones(pid) if x["id"] == o["suscripcion_id"]), None)
            if sub and sub["estado"] == "activa":
                resp.update(vence_en=int(sub["vence_en"]), token=st.token_suscripcion(sub["id"], clave_privada))
        return jsonify(resp)

    @app.post("/v1/avilapay/aviso")
    def avila_aviso():
        """Aviso de Ávila Pay. Solo se acepta con firma válida, y aun así el pago se confirma consultando a Ávila Pay."""
        r = avila_conf()
        cuerpo_crudo = request.get_data()
        motivo = avila_firma(r, cuerpo_crudo, preferir_usd=False)
        if motivo:
            with st.eng.begin() as c: st.log(c, None, "avilapay_aviso_rechazado", f"Bs: {motivo}", request.remote_addr)
            return jsonify(message="Firma inválida"), 401
        try:
            aviso = json.loads(cuerpo_crudo)
            ev, d = str(aviso.get("event") or "")[:40], aviso.get("data") or {}
            ref, sk = str(d.get("reference") or ""), str(d.get("session_key") or "")
        except (ValueError, AttributeError):
            return jsonify(message="JSON inválido"), 400
        avila_marcar(ultimo_aviso_en=reloj(), ultimo_aviso=f"{ev} {ref}"[:200])
        with st.eng.connect() as c:
            o = c.execute(select(ordenes).where(ordenes.c.ref == ref)).first() if RE_REF.match(ref) else None
        if o is None and CM.RE_ORDEN.match(ref):          # orden de otro sistema de Gigadevs (p. ej. Negocio Plus de Quincena VE)
            try:
                op = com.orden(ref)
                if op.get("proveedor") != "avilapay" or not hmac.compare_digest(sk, op.get("prepay_id") or ""): raise LookupError
                return jsonify(ok=True, estado=verificar_avila_producto(ref, str(d.get("payment_reference") or "")[:60] or None).get("estado"))
            except LookupError:
                pass
            except Exception as e:  # noqa: BLE001 — Ávila Pay reintenta el aviso
                avila_error(f"aviso {ref}: {e}"); return jsonify(message="Reintente"), 503
        if o is None or o.proveedor != "avilapay" or not hmac.compare_digest(sk, o.prepay_id or ""):
            with st.eng.begin() as c: st.log(c, None, "avilapay_aviso_ignorado", f"{ev} orden={ref[:40]} (no corresponde a una orden de Contax)")
            return jsonify(ok=True, ignorado=True)
        with st.eng.begin() as c: st.log(c, o.licencia_id, "avilapay_aviso", f"{ev} orden={ref} ref_banco={str(d.get('payment_reference') or '—')[:60]}")
        try:
            res = verificar_avilapay(ref, str(d.get("payment_reference") or "")[:60] or None)
        except Exception as e:  # noqa: BLE001 — 503: Ávila Pay reintenta el aviso (5 veces con espera creciente)
            avila_error(f"aviso {ref}: {e}")
            return jsonify(message="Reintente"), 503
        return jsonify(ok=True, estado=res.get("estado"))

    @app.post("/v1/avilapay/aviso-usd")
    def avila_aviso_usd():
        """Aviso de los pagos en dólares (Zelle / tarjeta internacional). La URL se registra en el portal de Ávila Pay;
        firma en «AvilaPay-Signature» con el secreto de avisos en dólares (o, si no se cargó, el mismo de bolívares)."""
        r = avila_conf()
        crudo = request.get_data()
        motivo = avila_firma(r, crudo, preferir_usd=True)
        if motivo:
            with st.eng.begin() as c: st.log(c, None, "avilapay_aviso_rechazado", f"USD: {motivo}", request.remote_addr)
            return jsonify(message="Firma inválida"), 401
        try:
            d = json.loads(crudo)
            ev = str(request.headers.get("AvilaPay-Event") or d.get("event_type") or "")[:40]
            ref, pid = str(d.get("external_order_id") or ""), str(d.get("payment_id") or "")
        except (ValueError, AttributeError):
            return jsonify(message="JSON inválido"), 400
        avila_marcar(ultimo_aviso_en=reloj(), ultimo_aviso=f"USD {ev} {ref}"[:200])
        with st.eng.connect() as c:
            o = c.execute(select(ordenes).where(ordenes.c.ref == ref)).first() if RE_REF.match(ref) else None
        if o is None and CM.RE_ORDEN.match(ref):
            try:
                op = com.orden(ref)
                if op.get("proveedor") != "avilapay_usd" or not hmac.compare_digest(pid, op.get("prepay_id") or ""): raise LookupError
                return jsonify(ok=True, estado=verificar_avila_producto(ref).get("estado"))
            except LookupError:
                pass
            except Exception as e:  # noqa: BLE001
                avila_error(f"aviso USD {ref}: {e}"); return jsonify(message="Reintente"), 503
        if o is None or o.proveedor != "avilapay_usd" or not hmac.compare_digest(pid, o.prepay_id or ""):
            with st.eng.begin() as c: st.log(c, None, "avilapay_aviso_ignorado", f"USD {ev} orden={ref[:40]} (no corresponde a una orden de Contax)")
            return jsonify(ok=True, ignorado=True)
        with st.eng.begin() as c: st.log(c, o.licencia_id, "avilapay_aviso", f"USD {ev} orden={ref}")
        try:
            res = verificar_avilapay(ref)
        except Exception as e:  # noqa: BLE001 — Ávila Pay reintenta el aviso
            avila_error(f"aviso USD {ref}: {e}")
            return jsonify(message="Reintente"), 503
        return jsonify(ok=True, estado=res.get("estado"))

    # ------------------------------------------------------------------ Binance configurable desde la consola
    from .binance import BinanceCuenta, ErrorBinance

    def transporte_binance():
        """Pruebas: transporte simulado. Producción: el relé de Frankfurt si BINANCE_RELAY_URL está definido (Binance bloquea EE. UU.)."""
        if binance_transporte: return {"transporte": binance_transporte}
        if cfg.get("binance_relay"):
            from .relay_binance import transporte_por_rele
            return {"transporte": transporte_por_rele(cfg["binance_relay"], clave_privada, reloj)}
        return {}

    def binance_conf():
        with st.eng.connect() as c:
            return c.execute(select(binance_cfg).where(binance_cfg.c.id == 1)).first()

    def bn():
        """Binance vigente: la configuración de la consola Maestro si existe; si no, la de las variables de entorno."""
        r = binance_conf()
        if r is not None and r.api_key_cifrada:
            try:
                cuenta = BinanceCuenta(descifrar(r.api_key_cifrada), descifrar(r.secreto_cifrado), **transporte_binance(), reloj=reloj)
            except ErrorBinance:
                cuenta = None
            return {"cuenta": cuenta, "pay_id": r.pay_id or "", "red": r.red or "", "direccion": r.direccion or "", "activa": bool(r.activa), "origen": "consola"}
        return {"cuenta": binance, "pay_id": cfg.get("binance_pay_id") or "", "red": cfg.get("usdt_red") or "", "direccion": cfg.get("usdt_direccion") or "",
                "activa": binance is not None, "origen": "variables de entorno" if binance is not None else None}

    def binance_marcar(**valores):
        with st.eng.begin() as c:
            c.execute(update(binance_cfg).where(binance_cfg.c.id == 1).values(**valores))   # sin fila (variables de entorno): no se guarda

    def binance_probar(cuenta, ahora=None):
        ahora = ahora or reloj()
        try:
            cuenta.pagos_recibidos(int((ahora - 3600) * 1000), int(ahora * 1000))
        except ErrorBinance as e:
            raise ValueError(f"Binance: {e}") from e

    @app.put("/admin/v1/binance")
    def a_binance_guardar():
        """El Maestro carga la clave de SOLO LECTURA de Binance, su secreto y el Pay ID. Se prueban antes de guardar."""
        if fernet is None: raise ValueError("El servidor no tiene LICENSE_ADMIN_SECRET para cifrar la clave")
        d, r = cuerpo(), binance_conf()
        clave, secreto = str(d.get("api_key") or "").strip(), str(d.get("api_secret") or "").strip()
        actual_k = descifrar(r.api_key_cifrada) if r is not None else ""
        actual_s = descifrar(r.secreto_cifrado) if r is not None else ""
        if bool(clave) != bool(secreto) and not (actual_k and actual_s): raise ValueError("Indique la clave y el secreto de la API de Binance")
        k, sec = clave or actual_k, secreto or actual_s
        if not (k and sec): raise ValueError("Indique la clave y el secreto de la API de Binance (solo lectura)")
        pay_id = str(d.get("pay_id") or (r.pay_id if r is not None else "") or "").strip()
        if not re.fullmatch(r"[0-9]{5,20}", pay_id): raise ValueError("El Binance Pay ID son solo números (5 a 20 dígitos)")
        red, direccion = str(d.get("red") or "").strip().upper()[:20], str(d.get("direccion") or "").strip()[:120]
        if direccion and not re.fullmatch(r"[0-9A-Za-z]{20,120}", direccion): raise ValueError("Dirección de depósito inválida")
        if direccion and not red: raise ValueError("Indique la red de la dirección de depósito (p. ej. TRC20 o BSC)")
        try:
            cuenta = BinanceCuenta(k, sec, **transporte_binance(), reloj=reloj)
        except ErrorBinance as e:
            raise ValueError(str(e)) from e
        binance_probar(cuenta)                         # se verifica ANTES de guardar
        valores = dict(pay_id=pay_id, red=red or None, direccion=direccion or None, activa=1 if d.get("activa", True) else 0,
                       actualizado_en=reloj(), actualizado_por=str(d.get("autor") or "")[:60], ultimo_error=None, ultimo_error_en=None)
        if clave: valores["api_key_cifrada"] = fernet.encrypt(clave.encode()).decode()
        if secreto: valores["secreto_cifrado"] = fernet.encrypt(secreto.encode()).decode()
        with st.eng.begin() as c:
            if not c.execute(update(binance_cfg).where(binance_cfg.c.id == 1).values(**valores)).rowcount:
                c.execute(insert(binance_cfg).values(id=1, **valores))
            st.log(c, None, "binance_configurada", f"activa={valores['activa']} clave_nueva={bool(clave)} pay_id=…{pay_id[-4:]} red={red or '—'} "
                                                   f"por={valores['actualizado_por']}")
        return jsonify(ok=True)

    @app.post("/admin/v1/binance/probar")
    def a_binance_probar():
        b = bn()
        if b["cuenta"] is None: raise ValueError("Binance no está configurado")
        try:
            binance_probar(b["cuenta"])
        except ValueError as e:
            binance_marcar(ultimo_error_en=reloj(), ultimo_error=str(e)[:300]); raise
        binance_marcar(ultimo_error=None, ultimo_error_en=None, ultima_revision_en=reloj())
        return jsonify(ok=True)

    @app.get("/admin/v1/cobros/metodos")
    def a_metodos():
        """Formas de pago que ve el cliente en su tienda, según lo que el Maestro tiene activo."""
        out = []
        b = bn()
        if b["cuenta"] is not None and b["activa"] and b["pay_id"]: out.append({"codigo": "USDT", "nombre": "USDT (Binance)"})
        av, r = avila_cliente(solo_activa=False)
        if av is not None and r is not None and r.activa_usd:
            for m in [x for x in (r.metodos_usd or "").split(",") if x in AV.METODOS_USD]:
                out.append({"codigo": m, "nombre": f"Dólares · {AV.METODOS_USD[m][1]}"})
        if av is not None and r is not None and r.activa:
            for m in [x for x in (r.metodos or "").split(",") if x in AV.METODOS] or list(AV.METODOS):
                out.append({"codigo": f"VES:{m}", "nombre": f"Bolívares · {AV.METODOS[m]}"})
        else:
            out.append({"codigo": "VES", "nombre": "Bolívares (pago móvil)"})
        return jsonify(metodos=out)

    # ------------------------------------------------------------------ ajuste por forma de pago (comisiones)
    def comisiones() -> dict:
        with st.eng.connect() as c:
            v = c.execute(select(meta.c.valor).where(meta.c.clave == "comisiones")).scalar()
        try:
            guardado = json.loads(v) if v else {}
        except ValueError:
            guardado = {}
        mets = {**COMISIONES_LISTA, **{k: str(x) for k, x in (guardado.get("metodos") or {}).items() if k in COMISIONES_LISTA}}
        return {"metodos": mets, "impuesto": str(guardado.get("impuesto", IMPUESTO_LISTA)), "lista": COMISIONES_LISTA, "impuesto_lista": IMPUESTO_LISTA}

    def ajustar(precio, metodos):
        """(total, ajuste): el total que, descontadas la comisión del método (el más caro si son varios) y el impuesto, deja el precio completo."""
        cm = comisiones()
        pct = (max(Decimal(cm["metodos"].get(m, "0")) for m in metodos) if metodos else Decimal(0)) + Decimal(cm["impuesto"])
        if pct <= 0: return Decimal(precio).quantize(Decimal("0.01")), Decimal("0.00")
        total = (Decimal(precio) / (1 - pct / 100)).quantize(Decimal("0.01"), rounding="ROUND_CEILING")
        return total, total - Decimal(precio).quantize(Decimal("0.01"))

    @app.get("/admin/v1/comisiones")
    def a_comisiones():
        return jsonify(comisiones())

    @app.put("/admin/v1/comisiones")
    def a_fijar_comisiones():
        d = cuerpo()
        def pct(v, nombre):
            try:
                x = Decimal(str(v).replace(",", ".")).quantize(Decimal("0.01"))
            except (InvalidOperation, ValueError) as e:
                raise ValueError(f"Porcentaje inválido en {nombre}") from e
            if not Decimal(0) <= x <= Decimal(25): raise ValueError(f"{nombre}: el porcentaje debe estar entre 0 y 25")
            return str(x)
        actual = comisiones()
        mets = {m: pct(d.get("metodos", {}).get(m, actual["metodos"][m]), m) for m in COMISIONES_LISTA}
        valor = json.dumps({"metodos": mets, "impuesto": pct(d.get("impuesto", actual["impuesto"]), "impuesto")})
        with st.eng.begin() as c:
            if not c.execute(update(meta).where(meta.c.clave == "comisiones").values(valor=valor)).rowcount:
                c.execute(insert(meta).values(clave="comisiones", valor=valor))
            st.log(c, None, "comisiones_actualizadas", f"{valor[:300]} por={str(d.get('autor') or '')[:60]}")
        return jsonify(comisiones())

    @app.get("/admin/v1/cobros/dolares")
    def a_dolares():
        """Monitoreo de los cobros en dólares: Binance (USDT) y Ávila Pay (Zelle / tarjeta internacional)."""
        b, rb, ra = bn(), binance_conf(), avila_conf()
        with st.eng.connect() as c:
            ords = [dict(x._mapping) for x in c.execute(select(ordenes).where(ordenes.c.proveedor.in_(("binance", "avilapay_usd")))
                                                         .order_by(ordenes.c.id.desc()).limit(100))]
            evs = [dict(x._mapping) for x in c.execute(select(eventos).where(or_(eventos.c.evento.like("binance%"), eventos.c.evento.like("pago_usdt%"),
                                                                                 eventos.c.detalle.like("USD %"), eventos.c.detalle.like("crear cobro USD%")))
                                                       .order_by(eventos.c.id.desc()).limit(60))]
        clave_av = descifrar(ra.api_key_cifrada) if ra is not None else ""
        url_pub = cfg.get("url_publica") or ""
        return jsonify(
            binance={"configurada": b["cuenta"] is not None, "activa": bool(b["activa"] and b["cuenta"] is not None), "origen": b["origen"],
                     "pay_id": b["pay_id"], "red": b["red"], "direccion": b["direccion"],
                     "clave": (lambda k: f"{k[:4]}…{k[-4:]}" if k else None)(descifrar(rb.api_key_cifrada) if rb is not None else ""),
                     "actualizado_en": rb.actualizado_en if rb is not None else None, "actualizado_por": rb.actualizado_por if rb is not None else None,
                     "ultima_revision_en": rb.ultima_revision_en if rb is not None else None,
                     "ultimo_error_en": rb.ultimo_error_en if rb is not None else None, "ultimo_error": rb.ultimo_error if rb is not None else None},
            avilapay={"configurada": bool(clave_av), "modo": AV.modo(clave_av) if clave_av else None, "clave": f"{clave_av[:8]}…{clave_av[-4:]}" if clave_av else None,
                      "activa": bool(ra is not None and ra.activa_usd and clave_av),
                      "metodos": [m for m in ((ra.metodos_usd if ra is not None else "") or "").split(",") if m in AV.METODOS_USD],
                      "secreto": bool(ra is not None and (descifrar(ra.secreto_usd_cifrado) or descifrar(ra.secreto_cifrado))),
                      "secreto_propio": bool(ra is not None and descifrar(ra.secreto_usd_cifrado)),
                      "webhook_url": f"{url_pub}/v1/avilapay/aviso-usd" if url_pub else None},
            ordenes=[{k: (str(v) if isinstance(v, Decimal) else v) for k, v in o.items() if k in (
                "ref", "organizacion_ref", "cliente", "monto", "monto_esperado", "moneda", "proveedor", "metodo", "estado", "creada_en", "pagada_en", "motivo",
                "detalle_pago")} for o in ords],
            eventos=[{"ts": e["ts"], "evento": e["evento"], "detalle": e["detalle"], "ip": e["ip"]} for e in evs])

    @app.put("/admin/v1/avilapay/dolares")
    def a_avila_usd():
        """Activa Zelle / tarjeta internacional con la MISMA clave de Ávila Pay (configurada en bolívares o aquí mismo)."""
        if fernet is None: raise ValueError("El servidor no tiene LICENSE_ADMIN_SECRET para cifrar la clave")
        d, r = cuerpo(), avila_conf()
        clave, secreto = str(d.get("api_key") or "").strip(), str(d.get("webhook_secret") or "").strip()
        actual = descifrar(r.api_key_cifrada) if r is not None else ""
        if not (clave or actual): raise ValueError("Indique la clave de API de Ávila Pay")
        if secreto and len(secreto) < 12: raise ValueError("El secreto de avisos (webhook secret) parece incompleto")
        metodos = [m for m in (d.get("metodos") or []) if m in AV.METODOS_USD]
        activa = bool(d.get("activa", True))
        if activa and not metodos: raise ValueError("Elija al menos un método: Zelle, tarjeta internacional o Apple Pay / Google Pay")
        try:
            AV.AvilaPay(clave or actual, avilapay_transporte).probar()
        except AV.ErrorAvila as e:
            raise ValueError(str(e)) from e
        valores = dict(activa_usd=1 if activa else 0, metodos_usd=",".join(metodos), actualizado_en=reloj(), actualizado_por=str(d.get("autor") or "")[:60])
        if clave: valores["api_key_cifrada"] = fernet.encrypt(clave.encode()).decode()
        if secreto: valores["secreto_usd_cifrado"] = fernet.encrypt(secreto.encode()).decode()
        if r is None: valores["activa"] = 0          # primera vez desde dólares: bolívares queda pausado hasta configurarlo
        avila_marcar(**valores)
        with st.eng.begin() as c:
            st.log(c, None, "avilapay_configurada", f"USD activa={valores['activa_usd']} metodos={valores['metodos_usd'] or '—'} clave_nueva={bool(clave)} "
                                                    f"secreto_nuevo={bool(secreto)} por={valores['actualizado_por']}")
        return jsonify(ok=True, modo=AV.modo(clave or actual))

    @app.errorhandler(AV.ErrorAvila)
    def _eav(e): return jsonify(message=str(e)[:250]), 502

    def avila_estado():
        r = avila_conf()
        clave = descifrar(r.api_key_cifrada) if r is not None else ""
        with st.eng.connect() as c:
            ords = [dict(x._mapping) for x in c.execute(select(ordenes).where(ordenes.c.proveedor == "avilapay").order_by(ordenes.c.id.desc()).limit(100))]
            evs = [dict(x._mapping) for x in c.execute(select(eventos).where(eventos.c.evento.like("avilapay%")).order_by(eventos.c.id.desc()).limit(60))]
        url_pub = cfg.get("url_publica") or ""
        return {"configurada": bool(clave), "activa": bool(r is not None and r.activa and clave), "modo": AV.modo(clave) if clave else None,
                "clave": f"{clave[:8]}…{clave[-4:]}" if clave else None, "secreto": bool(r is not None and descifrar(r.secreto_cifrado)),
                "clave_ilegible": bool(r is not None and r.api_key_cifrada and not clave),
                "metodos": [m for m in ((r.metodos if r is not None else "") or "").split(",") if m] or list(AV.METODOS),
                "actualizado_en": r.actualizado_en if r is not None else None, "actualizado_por": r.actualizado_por if r is not None else None,
                "ultimo_aviso_en": r.ultimo_aviso_en if r is not None else None, "ultimo_aviso": r.ultimo_aviso if r is not None else None,
                "ultimo_error_en": r.ultimo_error_en if r is not None else None, "ultimo_error": r.ultimo_error if r is not None else None,
                "webhook_url": f"{url_pub}/v1/avilapay/aviso" if url_pub else None, "retorno_url": bool(cfg.get("url_erp")),
                "ordenes": [{k: (str(v) if isinstance(v, Decimal) else v) for k, v in o.items() if k in (
                    "ref", "organizacion_ref", "cliente", "monto", "estado", "creada_en", "pagada_en", "entregada_en", "prepay_id", "detalle_pago", "motivo")} for o in ords],
                "eventos": [{"ts": e["ts"], "evento": e["evento"], "detalle": e["detalle"], "ip": e["ip"]} for e in evs]}

    @app.get("/admin/v1/avilapay")
    def a_avila():
        return jsonify(avila_estado())

    @app.put("/admin/v1/avilapay")
    def a_avila_guardar():
        """El Maestro carga la clave (sk_live_/sk_test_) y el secreto de avisos. La clave se PRUEBA antes de guardarla."""
        if fernet is None: raise ValueError("El servidor no tiene LICENSE_ADMIN_SECRET para cifrar la clave")
        d, r = cuerpo(), avila_conf()
        clave, secreto = str(d.get("api_key") or "").strip(), str(d.get("webhook_secret") or "").strip()
        actual = descifrar(r.api_key_cifrada) if r is not None else ""
        if not (clave or actual): raise ValueError("Indique la clave de API de Ávila Pay")
        if secreto and len(secreto) < 12: raise ValueError("El secreto de avisos (webhook secret) parece incompleto")
        metodos = [m for m in (d.get("metodos") or list(AV.METODOS)) if m in AV.METODOS]
        if not metodos: raise ValueError("Elija al menos un método de pago")
        try:
            n = AV.AvilaPay(clave or actual, avilapay_transporte).probar()   # se verifica ANTES de guardar
        except AV.ErrorAvila as e:
            raise ValueError(str(e)) from e
        valores = dict(activa=1 if d.get("activa", True) else 0, metodos=",".join(metodos), actualizado_en=reloj(),
                       actualizado_por=str(d.get("autor") or "")[:60], ultimo_error=None, ultimo_error_en=None)
        if clave: valores["api_key_cifrada"] = fernet.encrypt(clave.encode()).decode()
        if secreto: valores["secreto_cifrado"] = fernet.encrypt(secreto.encode()).decode()
        avila_marcar(**valores)
        with st.eng.begin() as c:
            st.log(c, None, "avilapay_configurada", f"modo={AV.modo(clave or actual)} activa={valores['activa']} clave_nueva={bool(clave)} "
                                                    f"secreto_nuevo={bool(secreto)} metodos={valores['metodos']} por={valores['actualizado_por']}")
        return jsonify(ok=True, sesiones=n, modo=AV.modo(clave or actual))

    @app.post("/admin/v1/avilapay/probar")
    def a_avila_probar():
        av, _ = avila_cliente(solo_activa=False)
        if av is None: raise ValueError("Ávila Pay no está configurado")
        try:
            n = av.probar()
        except AV.ErrorAvila as e:
            avila_error(f"prueba de conexión: {e}"); raise ValueError(str(e)) from e
        avila_marcar(ultimo_error=None, ultimo_error_en=None)
        return jsonify(ok=True, sesiones=n)

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
        b = bn()
        if b["cuenta"] is None: raise ValueError("Binance no está configurado")
        pagos = b["cuenta"].pagos_recibidos(desde_ms, hasta_ms)
        if b["direccion"]:
            pagos += b["cuenta"].depositos_recibidos(desde_ms, hasta_ms, b["red"])
        return pagos

    def verificar_binance(ref, pagos=None):
        """Empareja la orden con un ingreso en USDT de monto EXACTO, posterior a la orden y antes de su vencimiento.
        Cada transacción de Binance solo puede pagar una orden (clave primaria en lic_pagos_usados)."""
        if not RE_REF.match(ref) or bn()["cuenta"] is None: raise ValueError("Orden inválida")
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
        """Revisión periódica: Ávila Pay (respaldo de los avisos) y una sola consulta a Binance para todas las órdenes en USDT."""
        entregadas = revisar_avilapay()
        return entregadas + revisar_binance()

    def revisar_binance():
        if bn()["cuenta"] is None: return 0
        with st.eng.connect() as c:
            pend = c.execute(select(ordenes).where(ordenes.c.proveedor == "binance", ordenes.c.estado.in_(("pendiente", "pagada")))).all()
        if not pend: return 0
        ahora = reloj()
        try:
            pagos = leer_pagos(min(o.creada_en for o in pend) - 120, ahora)
        except Exception as e:  # noqa: BLE001 — se reintenta en la próxima vuelta
            with st.eng.begin() as c: st.log(c, None, "binance_no_disponible", str(e)[:200])
            binance_marcar(ultimo_error_en=reloj(), ultimo_error=str(e)[:300])
            return 0
        binance_marcar(ultima_revision_en=reloj())
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
        correo(f["cliente_correo"], f"Factura N° {f['numero_completo']} · {pc.MARCAS.get(f.get('sistema') or 'contax', pc.MARCAS['contax'])[0]}",
               fx_texto(f, intro), html=pc.factura_html(f, cfg.get("url_erp") or "", intro))
        with st.eng.begin() as c:
            c.execute(update(fx.facturas).where(fx.facturas.c.id == fid).values(correo_enviado_en=reloj()))
        return f

    def enviar_orden_producto(ref):
        """Orden de compra de un sistema (CriteriSeg, Surtia…) con la plantilla y la marca de ese sistema."""
        o, cli = None, None
        o = com.orden(ref); cli = com.cliente(o["cliente_id"])
        if not cli.get("correo") or correo is None: return False
        nombre = pc.MARCAS.get(o["producto_id"], pc.MARCAS["contax"])[0]
        periodo = {"mensual": "Mensual", "anual": "Anual (12 meses)", "unico": "Pago único"}.get(o["periodo"], o["periodo"])
        titulo = f"Orden de compra {o['ref']}"
        parrafos = [f"Estimado(a) {cli['nombre']}:", f"Registramos su orden de compra de {nombre}. Al confirmarse el pago se activa su suscripción "
                    "y le enviamos la factura a este correo."]
        filas = [("Orden", o["ref"]), ("Plan", o["plan"]), ("Período", periodo), ("Monto", f"{o['moneda']} {Decimal(o['monto']):,.2f}"), ("Estado", "Pendiente de pago")]
        nota = (f"Datos de pago: {cfg['pago_movil']}" if cfg.get("pago_movil") else "Gigadevs le indicará los datos de pago.") + f" Indique la orden {o['ref']} al pagar."
        correo(cli["correo"], f"{titulo} · {nombre}", pc.texto(titulo, parrafos, filas=filas, nota=nota, nombre=nombre),
               html=pc.html(titulo, parrafos, cfg.get("url_erp") or "", filas=filas, nota=nota, sistema=o["producto_id"]))
        with st.eng.begin() as c: st.log(c, None, "orden_producto_enviada", f"{ref} a {cli['correo']}")
        return True

    com.al_crear_orden = enviar_orden_producto
    com.al_facturar = lambda fid: enviar_factura(fid)

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
            forma = {"binance": "USDT por Binance", "avilapay": "Bolívares por Ávila Pay",
                     "avilapay_usd": f"{AV.METODOS_USD.get(o.metodo, ('', 'Dólares'))[1]} por Ávila Pay"}.get(o.proveedor, "Pago móvil (Bs)")
            f = fx.emitir(con, cliente_nombre=o.cliente, cliente_rif=o.cliente_rif, cliente_direccion=o.cliente_direccion, cliente_correo=o.correo,
                          moneda=o.moneda if o.moneda in ("USDT", "USD") else "VES", tasa_bcv=o.tasa_bcv,
                          monto_con_iva=(o.monto_esperado or o.monto) if usdt else o.monto,
                          concepto=_concepto(o.plan, o.modulos, meses=o.meses), forma_pago=forma,
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

    def filtro_sistema(columna):
        """?sistema=… del listado. Los registros sin sistema (anteriores a la columna) son de Contax."""
        sid = request.args.get("sistema")
        if not sid: return None
        sid = px.sistema_de(sid)
        return or_(columna == "contax", columna.is_(None)) if sid == "contax" else columna == sid

    def emisor_px():
        with st.eng.connect() as c:
            cf = dict(c.execute(select(fx.config).where(fx.config.c.id == 1)).first()._mapping)
        return {k: (str(cf.get(k)) if cf.get(k) is not None else "") for k in ("razon_social", "rif", "domicilio", "telefono", "correo", "iva_pct")}

    @app.get("/admin/v1/presupuestos")
    def a_px_listar():
        with st.eng.connect() as c:
            q, f = select(px.presupuestos).order_by(px.presupuestos.c.id.desc()).limit(500), filtro_sistema(px.presupuestos.c.sistema)
            return jsonify(presupuestos=[px.como_dict(r) for r in c.execute(q if f is None else q.where(f))])

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
        correo(destino, f"Presupuesto {p['numero_txt']} · {p['sistema_nombre'].rstrip('®')}", intro + "\n\n" + pc.presupuesto_texto(p, em),
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
            q, f = select(px.contratos.c.id).order_by(px.contratos.c.id.desc()).limit(500), filtro_sistema(px.contratos.c.sistema)
            ids = [r[0] for r in c.execute(q if f is None else q.where(f))]
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
                    f"Gigadevs Corp le envía el contrato {ct['numero_txt']} de los servicios de {ct['sistema_nombre']} aceptados en el presupuesto {ct['presupuesto_txt']}.",
                    "Puede leerlo completo y aceptarlo electrónicamente con su nombre y cédula en el siguiente enlace (válido 30 días). La aceptación "
                    "tiene plena validez conforme al Decreto con Fuerza de Ley sobre Mensajes de Datos y Firmas Electrónicas."]
        correo(destino, titulo + " · " + ct["sistema_nombre"].rstrip("®"),
               pc.texto(titulo, parrafos, ("Leer y firmar el contrato", url), nombre=ct["sistema_nombre"].rstrip("®")),
               html=pc.html(titulo, parrafos, cfg.get("url_erp") or "", "Contax", "Tu empresa en orden. Tu fisco al día.", ("Leer y firmar el contrato", url),
                            sistema=ct.get("sistema")))
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
            q, f = select(fx.facturas).order_by(fx.facturas.c.id.desc()).limit(500), filtro_sistema(fx.facturas.c.sistema)
            fs = [fx.como_dict(r) for r in c.execute(q if f is None else q.where(f))]
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
                          forma_pago=d.get("forma_pago"), licencia_id=d.get("licencia_id"), reloj=reloj, sistema=d.get("sistema"))
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
    app.extensions["verificar_avilapay"] = verificar_avilapay
    return app
