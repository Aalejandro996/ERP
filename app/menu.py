"""Navegación de la empresa, con el MISMO orden y ubicación de módulos del sistema de escritorio (main.py, barra lateral
«NAVEGACIÓN»): catálogo de cuentas → asientos → mayor → diario → balance general → comprobación → resultados → dashboard →
libros fiscales → conciliación bancaria → divisas → configuración; luego Fiscal Venezuela, Administración (CxC/CxP,
inventario) y Auditoría. Los sub-módulos que en escritorio eran pestañas internas (sub_notebook) aquí son pestañas
arriba de la página. Una sola definición alimenta la barra lateral y las pestañas."""
from flask import g, request, url_for

from .planes import modulo_de_permiso
from .security import tiene_permiso

# (grupo, [(endpoint, icono, texto, permiso)])
MENU = [
    ("Facturación", [
        ("web.facturas", "🧾", "Documentos de venta", "facturacion.ver"),
        ("web.factura_nueva", "➕", "Nueva factura", "facturacion.emitir"),
        ("web.libro_ventas", "📗", "Libro de ventas", "facturacion.ver"),
        ("web.clientes_fact", "👥", "Clientes", "facturacion.ver"),
        ("web.series", "🔢", "Series y control", "facturacion.configurar")]),
    ("Contabilidad", [
        ("web.cuentas", "📝", "Catálogo de cuentas", "asientos.ver"),
        ("web.asientos", "⚖️", "Asientos contables", "asientos.ver"),
        ("web.mayor", "🔍", "Libro mayor", "reportes.ver"),
        ("web.diario", "📖", "Libro diario", "asientos.ver"),
        ("web.balance_general", "🏛️", "Balance general", "reportes.ver"),
        ("web.comprobacion", "📊", "Balance de comprobación", "reportes.ver"),
        ("web.resultados", "💰", "Estado de resultados", "reportes.ver"),
        ("web.dashboard", "📈", "Dashboard gráfico", "reportes.ver"),
        ("web.fiscal_libros", "📋", "Libros fiscales", "fiscal.ver"),
        ("web.bancos", "🏦", "Conciliación bancaria", "cartera.ver"),
        ("web.divisas", "💱", "Divisas y moneda", "asientos.ver"),
        ("web.periodos", "⚙️", "Configuración y cierres", "asientos.ver"),
        ("web.cuadre", "✅", "Cuadre de libros", "reportes.ver")]),
    ("Fiscal Venezuela", [
        ("web.fiscal_islr", "🇻🇪", "ISLR", "fiscal.ver"),
        ("web.fiscal_retenciones", "✂️", "Retenciones IVA / ISLR", "fiscal.ver")]),
    ("Administración", [
        ("web.cxc", "💳", "Cuentas por cobrar", "cartera.ver"),
        ("web.cxp", "💸", "Cuentas por pagar", "cartera.ver"),
        ("web.libro_compras", "📒", "Libro de compras", "cartera.ver"),
        ("web.ordenes_compra", "🛒", "Órdenes de compra", "compras.ver"),
        ("web.proveedores", "🚚", "Proveedores", "compras.ver"),
        ("web.inventario", "📦", "Inventario", "inventario.ver"),
        ("web.pedidos", "💬", "Pedidos por WhatsApp", "whatsapp.ver"),
        ("web.whatsapp_config", "🤖", "Bot de WhatsApp", "whatsapp.configurar")]),
    ("Auditoría", [
        ("web.auditoria", "🕵️", "Auditoría financiera", "auditoria.ver")]),
    ("Empresa", [
        ("web.empresa_datos", "🏢", "Datos, logo y módulos", "admin"),
        ("web.migracion", "📥", "Migrar datos de otro sistema", "admin")]),
]

# Pestañas internas (equivalentes a los sub_notebook del escritorio)
PESTANAS = [
    ["web.cuentas", "web.asientos"],
    ["web.mayor", "web.diario"],
    ["web.balance_general", "web.comprobacion", "web.resultados", "web.dashboard"],
    ["web.cxc", "web.cxp", "web.libro_compras"],
    ["web.ordenes_compra", "web.proveedores"],
    ["web.fiscal_islr", "web.fiscal_retenciones", "web.fiscal_libros"],
    ["web.pedidos", "web.whatsapp_config"],
    ["web.periodos", "web.cuadre"],
    ["web.empresa_datos", "web.migracion"],
]
_TEXTO = {ep: (ic, tx, pm) for _, items in MENU for ep, ic, tx, pm in items}


def _incluido(permiso, modulos) -> bool:
    m = modulo_de_permiso(permiso)
    if m is None: return True
    return any(x in modulos for x in m) if isinstance(m, tuple) else m in modulos


def menu_empresa(eid):
    """Grupos desplegables con sus ítems: «ok» (enlace), «plan» (no incluido en el plan: bloqueado) u omitido (rol, o módulo
    que la empresa no trabaja). Solo el grupo de la página actual viene abierto, para que el menú no sea interminable."""
    modulos, u, out = getattr(g, "modulos", frozenset()), g.usuario, []
    del_plan = getattr(g, "modulos_licencia", modulos)
    for grupo, items in MENU:
        vis = []
        for ep, ic, tx, pm in items:
            if not _incluido(pm, del_plan): vis.append(dict(texto=tx, icono=ic, estado="plan"))
            elif _incluido(pm, modulos) and tiene_permiso(u, pm):
                vis.append(dict(texto=tx, icono=ic, estado="ok", url=url_for(ep, eid=eid), activo=request.endpoint == ep))
        if any(i["estado"] == "ok" for i in vis):
            out.append(dict(titulo=grupo, icono=items[0][1], enlaces=vis, abierto=any(i.get("activo") for i in vis)))
        elif vis:
            out.append(dict(titulo=grupo, icono=items[0][1], enlaces=vis, abierto=False, bloqueado=True))
    return out


def pestanas(eid):
    ep = request.endpoint
    for grupo in PESTANAS:
        if ep in grupo:
            tabs = [dict(texto=f"{_TEXTO[e][0]} {_TEXTO[e][1]}", url=url_for(e, eid=eid), activo=e == ep)
                    for e in grupo if _incluido(_TEXTO[e][2], getattr(g, "modulos", frozenset())) and tiene_permiso(g.usuario, _TEXTO[e][2])]
            return tabs if len(tabs) > 1 else []
    return []
