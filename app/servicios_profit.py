"""Servicios de la integración con Profit Plus (solo lectura): conexión de cada empresa, recepción de la foto del conector
y el catálogo que usa el bot de WhatsApp cuando la empresa trabaja con Profit."""
import secrets
from decimal import ROUND_HALF_UP, Decimal

from flask import current_app
from sqlalchemy import delete, func, insert, select

from .extensions import db
from .models import _ahora
from .models_profit import ProfitArticulo, ProfitCliente, ProfitConexion, ProfitPlataforma, ProfitSincronizacion
from .tiempo import hoy_ve

GUARDAR_SINC = 200


def plataforma() -> ProfitPlataforma | None:
    return db.session.get(ProfitPlataforma, 1)


def conexion(eid) -> ProfitConexion | None:
    return db.session.get(ProfitConexion, eid)


def activa(eid) -> bool:
    """La empresa atiende el bot con el catálogo de Profit (conexión activa y con al menos una foto recibida)."""
    c = conexion(eid)
    return c is not None and c.activo and c.ultima_sync is not None


def crear_conexion(eid) -> ProfitConexion:
    c = conexion(eid)
    if c is not None: return c
    c = ProfitConexion(empresa_id=eid, conector_id="pp_" + secrets.token_hex(10),
                       secreto_cifrado=current_app.extensions["cifrador"].cifrar(secrets.token_urlsafe(32)), activo=True,
                       almacenes=[], lista_precio="01", restar_comprometido=True, solo_con_existencia=False, disponibles={})
    db.session.add(c); db.session.flush()
    return c


def rotar_secreto(c: ProfitConexion):
    c.secreto_cifrado = current_app.extensions["cifrador"].cifrar(secrets.token_urlsafe(32))


def secreto(c: ProfitConexion) -> str:
    return current_app.extensions["cifrador"].descifrar(c.secreto_cifrado)


def url_api() -> str:
    p = plataforma()
    return ((p.url_publica if p and p.url_publica else None) or current_app.config["APP_BASE_URL"]).rstrip("/")


def conector_ini(c: ProfitConexion) -> str:
    """Archivo de configuración del conector, listo para copiar junto al programa en el servidor de Profit Plus.
    Solo falta la conexión a SQL Server (que nunca sale de la red del cliente)."""
    return f"""; Conector Contax ↔ Profit Plus (SOLO LECTURA). Generado por Contax el {hoy_ve().isoformat()}.
; Guarde este archivo como «contax_profit.ini» junto a contax_profit.exe. No lo comparta: la clave identifica a su empresa.

[contax]
url = {url_api()}
conector = {c.conector_id}
clave = {secreto(c)}

[sqlserver]
; Servidor y base de datos de Profit Plus. Use un usuario de SOLO LECTURA (vea crear_usuario_lectura.sql).
servidor = localhost\\SQLEXPRESS
base_datos = NOMBRE_DE_LA_EMPRESA_EN_PROFIT
usuario = contax_lectura
contrasena =
; driver = ODBC Driver 18 for SQL Server
; cifrar = no

[profit]
; Versión: 2k12 (Profit Plus 2K12 / 12.x). Para otra versión ajuste las consultas en la sección [consultas].
esquema = 2k12
; Códigos de tipo de stock en saStockAlmacen
stock_actual = ACT
stock_comprometido = COM

[alicuotas]
; tipo_imp de Profit → alícuota en Contax (G general, R reducida, A adicional/lujo, E exento)
1 = G
2 = R
3 = A
6 = E
7 = E
"""


# ====================================================================== recepción de la foto
def aplicar_foto(eid, c: ProfitConexion, foto: dict, ip=None, duracion_ms=None):
    """Reemplaza artículos y clientes de la empresa por la foto recibida, en UNA transacción (o queda la anterior)."""
    db.session.execute(delete(ProfitArticulo).where(ProfitArticulo.empresa_id == eid))
    db.session.execute(delete(ProfitCliente).where(ProfitCliente.empresa_id == eid))
    if foto["articulos"]:
        db.session.execute(insert(ProfitArticulo), [dict(empresa_id=eid, **a) for a in foto["articulos"]])
    if foto["clientes"]:
        db.session.execute(insert(ProfitCliente), [dict(empresa_id=eid, **x) for x in foto["clientes"]])
    ahora = _ahora()
    c.ultima_sync = c.ultimo_contacto = ahora
    c.ultimo_estado, c.ultimo_error = "ok", None
    c.n_articulos, c.n_clientes = len(foto["articulos"]), len(foto["clientes"])
    c.agente_version, c.agente_equipo = foto.get("version") or c.agente_version, foto.get("equipo") or c.agente_equipo
    if foto["disponibles"]["almacenes"] or foto["disponibles"]["listas_precio"]: c.disponibles = foto["disponibles"]
    anotar(eid, "ok", articulos=c.n_articulos, clientes=c.n_clientes, duracion_ms=duracion_ms, ip=ip)


def anotar(eid, resultado, articulos=0, clientes=0, duracion_ms=None, detalle=None, ip=None):
    db.session.add(ProfitSincronizacion(empresa_id=eid, resultado=resultado, articulos=articulos, clientes=clientes,
                                        duracion_ms=duracion_ms, detalle=(detalle or "")[:500] or None, ip=(ip or "")[:60] or None))
    db.session.flush()
    corte = db.session.execute(select(ProfitSincronizacion.id).where(ProfitSincronizacion.empresa_id == eid)
                               .order_by(ProfitSincronizacion.id.desc()).offset(GUARDAR_SINC).limit(1)).scalar()
    if corte: db.session.execute(delete(ProfitSincronizacion).where(ProfitSincronizacion.empresa_id == eid, ProfitSincronizacion.id <= corte))


def error_conector(eid, c: ProfitConexion, mensaje, ip=None):
    c.ultimo_contacto, c.ultimo_estado, c.ultimo_error = _ahora(), "error", str(mensaje)[:500]
    anotar(eid, "error", detalle=str(mensaje), ip=ip)


# ====================================================================== catálogo para el bot
def _tasas():
    from .tasas import tasa_vigente
    cache = {}
    def t(mon):
        if mon not in cache:
            try: cache[mon] = tasa_vigente(hoy_ve(), mon)
            except Exception:  # noqa: BLE001 — sin tasa se omiten los precios en divisas, nunca se inventa una
                cache[mon] = None
        return cache[mon]
    return t


def precio_bs(a: ProfitArticulo, tasa) -> Decimal | None:
    if a.moneda == "VES": return Decimal(a.precio)
    t = tasa(a.moneda)
    return (Decimal(a.precio) * Decimal(str(t))).quantize(Decimal("0.01"), ROUND_HALF_UP) if t else None


def catalogo(eid, todos=False) -> dict:
    """{codigo: (nombre, precio_bs, existencia)} desde la foto de Profit. Los precios en divisas se llevan a Bs con la tasa BCV
    del día (sin tasa, esos artículos no se ofrecen). `todos`: incluye los que no tienen precio o existencia (para leer PDF)."""
    c, tasa, out = conexion(eid), _tasas(), {}
    q = select(ProfitArticulo).where(ProfitArticulo.empresa_id == eid).order_by(ProfitArticulo.codigo)
    if not todos:
        q = q.where(ProfitArticulo.precio > 0)
        if c is not None and c.solo_con_existencia: q = q.where(ProfitArticulo.existencia > 0)
    for a in db.session.execute(q).scalars():
        p = precio_bs(a, tasa)
        if p is None and not todos: continue
        out[a.codigo] = (a.nombre, p or Decimal(0), Decimal(a.existencia))
    return out


def articulo(eid, codigo) -> ProfitArticulo | None:
    return db.session.get(ProfitArticulo, (eid, codigo)) if codigo else None


def cliente_por_rif(eid, rif) -> ProfitCliente | None:
    if not rif: return None
    return db.session.execute(select(ProfitCliente).where(ProfitCliente.empresa_id == eid, ProfitCliente.rif == rif).limit(1)).scalar_one_or_none()


def cliente_por_whatsapp(eid, tel) -> ProfitCliente | None:
    return db.session.execute(select(ProfitCliente).where(ProfitCliente.empresa_id == eid, ProfitCliente.whatsapp == tel).limit(1)).scalar_one_or_none()


def ultimas(eid, n=15):
    return db.session.execute(select(ProfitSincronizacion).where(ProfitSincronizacion.empresa_id == eid)
                              .order_by(ProfitSincronizacion.id.desc()).limit(n)).scalars().all()


def resumen(eid) -> dict:
    con_exist = db.session.execute(select(func.count()).where(ProfitArticulo.empresa_id == eid, ProfitArticulo.existencia > 0)).scalar()
    en_divisas = db.session.execute(select(func.count()).where(ProfitArticulo.empresa_id == eid, ProfitArticulo.moneda != "VES")).scalar()
    return dict(con_existencia=con_exist or 0, en_divisas=en_divisas or 0)
