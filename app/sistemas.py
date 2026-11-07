"""Sistemas que administra la Consola Maestro de Gigadevs y los módulos de cada uno.

Al iniciar sesión el Maestro solo ve el selector de sistemas, sin menú lateral. Al elegir un sistema, el menú lateral
muestra únicamente los módulos de ese sistema. La elección se guarda en la sesión para que las páginas compartidas
(presupuestos, contratos y facturación) trabajen con el sistema elegido.

Contax es el sistema central de Gigadevs: lleva su contabilidad, administración y parte fiscal (licencia All in One de
pago único) y concentra la normativa legal. Cada sistema emite sus propios presupuestos, contratos y facturas, pero con
el mismo emisor (Gigadevs Corp), la misma numeración fiscal y las mismas cláusulas legales de Contax.
"""
import os

from flask import g, request, session, url_for

_MARCA = os.path.join(os.path.dirname(__file__), "static", "marca")


def _logo(*nombres):
    """Primer logo que exista en app/static/marca (o None)."""
    return next((f"marca/{n}" for n in nombres if os.path.isfile(os.path.join(_MARCA, n))), None)

CLAVE_SESION = "maestro_sistema"

# Módulos licenciables de cada producto (los de Contax están en app/planes.py y los demás en el servidor de licencias).
SISTEMAS = {
    "contax": {
        "licencia": "Licencia All in One · pago único",
        "nombre": "Contax", "marca": "Contax®", "color": "#7fdc45", "logo": "marca/logo-icono.svg",
        "lema": "Contabilidad, administración, fiscal y normativa legal de Gigadevs · All in One · pago único",
        "modulos_licencia": ["Contabilidad y fiscal", "Administración", "Auditoría financiera", "Inventario", "Facturación"],
    },
    "criteriseg": {
        "licencia": "Por organización · nube o entorno cerrado",
        "nombre": "CriteriSeg", "marca": "CriteriSeg", "color": "#2f7fd6", "logo": "marca/criteriseg.svg",
        "lema": "Auditoría y cumplimiento · suscripciones por organización",
        "modulos_licencia": ["Planificación de auditoría", "Trabajo de campo y evidencias", "Cumplimiento normativo",
                             "Informes y planes de acción", "Analítica y dashboards"],
    },
    "bot_whatsapp": {
        "licencia": "Por organización · nube o entorno cerrado",
        "nombre": "Enlace MIT", "marca": "Enlace MIT", "color": "#ff9a0a", "monograma": "EM",
        "logo": _logo("enlace-mit.svg", "enlace-mit.png"),   # sin archivo subido se muestra el monograma
        "lema": "Mensajería · organizaciones y entornos cerrados",
        "modulos_licencia": ["Sesiones y conexiones", "Envío de mensajes", "Historial de mensajes", "Incidencias y seguimiento"],
    },
    "surtia": {
        "licencia": "Por empresa · nube",
        "nombre": "Surtia", "marca": "Surtia™", "color": "#16C79A", "logo": "marca/surtia.svg",
        "lema": "Pedidos por WhatsApp conectados a Profit Plus y otros sistemas · licencia por empresa",
        "modulos_licencia": ["Bot de pedidos por WhatsApp", "Presupuestos", "Conversaciones y atención",
                             "Lectura de órdenes de compra (PDF)", "Integración con el sistema administrativo", "Avisos por correo"],
    },
    "control_financiero": {
        "licencia": "Cuenta individual · nube",
        "nombre": "Quincena VE", "marca": "Quincena VE", "color": "#19b37d", "logo": "marca/quincena.svg",
        "lema": "Finanzas personales y Negocio Plus · suscripción por cuenta, pagada en la app con Ávila Pay",
        "modulos_licencia": ["Panel de finanzas", "Ingresos y gastos", "Deudas y compromisos", "Pagos programados", "Herramientas de negocio"],
    },
}

# Páginas que solo existen en Contax: si el Maestro entra directo a una de ellas, Contax queda como sistema activo.
SOLO_CONTAX = {
    "web.plataforma", "web.maestro_licencias", "web.maestro_precios", "web.maestro_pagos", "web.maestro_dolares",
    "web.maestro_avilapay", "web.maestro_whatsapp", "web.maestro_bd", "web.plataforma_bitacora", "web.maestro_gigadevs",
    "web.maestro_legal", "web.maestro_legal_documento", "web.maestro_legal_aceptaciones", "web.maestro_legal_abogada",
    "web.maestro_drive", "web.maestro_drive_google",
}
# Páginas de un sistema concreto, que llevan el sistema en el parámetro «producto».
POR_PRODUCTO = {"web.maestro_suscripciones", "web.maestro_suscripcion_token"}
# Módulos comerciales de cada sistema, con el sistema en la ruta (/plataforma/sistemas/<pid>/…)
POR_RUTA = {"web.maestro_producto_clientes", "web.maestro_producto_planes", "web.maestro_producto_ordenes"}


def valido(sid) -> bool:
    return sid in SISTEMAS


def activo():
    """Sistema activo del Maestro en esta petición (o None: selector o usuario que no es Maestro). Se calcula una vez."""
    if "sistema" not in g:
        u = getattr(g, "usuario", None)
        g.sistema = _resolver() if u is not None and u.rol == "Maestro" else None
    return g.sistema


def _resolver():
    ep = request.endpoint or ""
    if ep == "web.maestro_sistemas":
        session.pop(CLAVE_SESION, None)
        return None
    sid = None
    if ep in POR_PRODUCTO and valido(request.args.get("producto")):
        sid = request.args["producto"]
    elif ep in POR_RUTA and valido((request.view_args or {}).get("pid")):
        sid = request.view_args["pid"]
    elif valido(request.args.get("sistema")):
        sid = request.args["sistema"]
    elif ep in SOLO_CONTAX:
        sid = "contax"
    elif request.method == "POST" and valido(request.form.get("sistema")):
        sid = request.form["sistema"]
    if sid:
        session[CLAVE_SESION] = sid
        return sid
    actual = session.get(CLAVE_SESION)
    return actual if valido(actual) else None


def actual() -> str:
    """Sistema con el que trabajan las páginas compartidas (presupuestos, contratos, facturas). Por defecto, Contax."""
    sid = activo()
    return sid if valido(sid) else "contax"


def inicio(sid) -> str:
    return url_for("web.plataforma") if sid == "contax" else url_for("web.maestro_producto_clientes", pid=sid)


def _enlace(texto, endpoint, icono, activos=(), **kw):
    ep = request.endpoint or ""
    return {"texto": texto, "url": url_for(endpoint, **kw), "icono": icono, "activo": ep == endpoint or ep in activos}


def menu(sid):
    """Grupos del menú lateral del sistema `sid`: solo sus módulos."""
    comercial = [
        _enlace("Presupuestos", "web.maestro_presupuestos", "🧾", ("web.maestro_presupuesto_nuevo", "web.maestro_presupuesto_ver"), sistema=sid),
        _enlace("Contratos", "web.maestro_contratos", "✍️", ("web.maestro_contrato_ver",), sistema=sid),
        _enlace("Facturación", "web.maestro_facturas", "🧮", ("web.maestro_factura_ver", "web.maestro_factura_ejemplo"), sistema=sid),
    ]
    if sid == "contax":
        return [
            {"titulo": "Clientes y licencias", "enlaces": [
                _enlace("Clientes", "web.plataforma", "🏢"),
                _enlace("Licencias y equipos", "web.maestro_licencias", "🔑")]},
            {"titulo": "Cobros y precios", "enlaces": [
                _enlace("Precios y planes", "web.maestro_precios", "🏷️"),
                _enlace("Órdenes", "web.maestro_pagos", "📦"),
                _enlace("Pagos en dólares", "web.maestro_dolares", "💵"),
                _enlace("Pagos en bolívares", "web.maestro_avilapay", "💳")]},
            {"titulo": "Comercial", "enlaces": comercial},
            {"titulo": "Gigadevs", "enlaces": [_enlace("Gigadevs en Contax", "web.maestro_gigadevs", "⭐")]},
            {"titulo": "Legal", "enlaces": [
                _enlace("Documentos legales", "web.maestro_legal", "📜", ("web.maestro_legal_documento",)),
                _enlace("Aceptaciones", "web.maestro_legal_aceptaciones", "🗂️"),
                _enlace("Abogada", "web.maestro_legal_abogada", "✒️"),
                _enlace("Google Drive", "web.maestro_drive", "🗄️", ("web.maestro_drive_google",))]},
            {"titulo": "Normativa legal", "enlaces": [
                _enlace("Parámetros legales", "web.admin_fiscal", "⚖️"),
                _enlace("Textos legales", "web.legal", "📜", pagina="terminos")]},
            {"titulo": "Plataforma", "enlaces": [
                _enlace("WhatsApp (OpenWA)", "web.maestro_whatsapp", "💬"),
                _enlace("Base de datos", "web.maestro_bd", "🗄️"),
                _enlace("Bitácora", "web.plataforma_bitacora", "📚")]},
        ]
    return [
        {"titulo": "Clientes y licencias", "enlaces": [
            _enlace("Clientes", "web.maestro_producto_clientes", "🏢", pid=sid),
            _enlace("Suscripciones", "web.maestro_suscripciones", "🔑", ("web.maestro_suscripcion_token",), producto=sid)]},
        {"titulo": "Cobros y precios", "enlaces": [
            _enlace("Precios y planes", "web.maestro_producto_planes", "🏷️", pid=sid),
            _enlace("Órdenes y pagos", "web.maestro_producto_ordenes", "💵", pid=sid)]},
        {"titulo": "Comercial", "enlaces": comercial},
        {"titulo": "Normativa (Contax)", "enlaces": [
            _enlace("Parámetros legales", "web.admin_fiscal", "⚖️"),
            _enlace("Textos legales", "web.legal", "📜", pagina="terminos")]},
    ]
