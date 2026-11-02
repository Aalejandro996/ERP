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
        ("web.presupuestos", "📑", "Presupuestos", "presupuestos.ver"),
        ("web.presupuesto_nuevo", "➕", "Nuevo presupuesto", "presupuestos.emitir"),
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
    ["web.presupuestos", "web.presupuesto_nuevo"],
]
_TEXTO = {ep: (ic, tx, pm) for _, items in MENU for ep, ic, tx, pm in items}


def _incluido(permiso, modulos) -> bool:
    m = modulo_de_permiso(permiso)
    if m is None: return True
    return any(x in modulos for x in m) if isinstance(m, tuple) else m in modulos


# Áreas de la barra superior (pedido de clientes: «separar Administración, Contabilidad, Clientes, Inventario arriba y que
# el menú de la izquierda muestre solo lo de esa gestión»). Cada área agrupa grupos de MENU, en este orden.
AREAS = [
    ("Contabilidad", "📒", [("Contabilidad", ("web.cuentas", "web.asientos", "web.mayor", "web.diario", "web.balance_general", "web.comprobacion",
                                              "web.resultados", "web.dashboard", "web.fiscal_libros", "web.bancos", "web.divisas", "web.periodos", "web.cuadre")),
                           ("Fiscal Venezuela", None), ("Auditoría", None)]),
    # la conciliación bancaria (módulo administrativo) va en el orden de IQ_Contable y también aquí, por si la contabilidad está apagada
    ("Administración", "💼", [("Administración", ("web.cxc", "web.cxp", "web.libro_compras", "web.bancos"))]),
    ("Clientes", "👥", [("Facturación", None), ("WhatsApp", ("web.pedidos", "web.whatsapp_config"))]),
    ("Inventario", "📦", [("Inventario", ("web.inventario",)), ("Compras", ("web.ordenes_compra", "web.proveedores"))]),
    ("Empresa", "🏢", [("Empresa", None)]),
]
COMPARTIDOS = {"web.bancos": "Administración"}   # aparece en dos áreas; por sí solo solo hace visible la suya
_ITEMS = {ep: (ep, ic, tx, pm) for _, items in MENU for ep, ic, tx, pm in items}
_GRUPO = {grupo: [i[0] for i in items] for grupo, items in MENU}
# pantallas de detalle que no están en el menú: su área se deduce de la ruta
PREFIJOS = (("/bancos", "Administración"), ("/facturacion", "Clientes"), ("/presupuestos", "Clientes"), ("/whatsapp", "Clientes"), ("/compras", "Inventario"),
            ("/inventario", "Inventario"), ("/cxc", "Administración"), ("/cxp", "Administración"), ("/empresa", "Empresa"), ("/migracion", "Empresa"))


def areas_empresa(eid):
    """Áreas para la barra superior y, de la activa, sus grupos para la barra lateral (lista plana, sin desplegables).
    Un área cuyo contenido no está en el plan se muestra bloqueada; una que el rol o la empresa no usan se omite."""
    modulos, u = getattr(g, "modulos", frozenset()), g.usuario
    del_plan = getattr(g, "modulos_licencia", modulos)
    ep, ruta, out = request.endpoint, request.path, []
    for area, icono, grupos in AREAS:
        gs = []
        for titulo, eps in grupos:
            enl = []
            for e in (eps or _GRUPO.get(titulo, ())):
                _, ic, tx, pm = _ITEMS[e]
                if not _incluido(pm, del_plan): enl.append(dict(texto=tx, icono=ic, estado="plan"))
                elif _incluido(pm, modulos) and tiene_permiso(u, pm):
                    enl.append(dict(texto=tx, icono=ic, estado="ok", url=url_for(e, eid=eid), activo=ep == e, ep=e))
            if enl: gs.append(dict(titulo=titulo, enlaces=enl))
        ok = [x for gr in gs for x in gr["enlaces"] if x["estado"] == "ok"]
        propios = [x for x in ok if COMPARTIDOS.get(x["ep"], area) == area]
        if propios: out.append(dict(titulo=area, icono=icono, url=propios[0]["url"], grupos=gs, activo=any(x["activo"] for x in ok)))
        elif any(x["estado"] == "plan" for gr in gs for x in gr["enlaces"]): out.append(dict(titulo=area, icono=icono, bloqueado=True, grupos=[]))
    activas = [a for a in out if a.get("activo")]
    for a in activas[1:]: a["activo"] = False            # pantalla compartida: queda activa la primera área (orden de IQ_Contable)
    if not activas:
        destino = next((ar for pre, ar in PREFIJOS if f"/e/{eid}{pre}" in ruta), None)
        for a in out:
            if a["titulo"] == destino and not a.get("bloqueado"): a["activo"] = True
    if not any(a.get("activo") for a in out):
        primera = next((a for a in out if not a.get("bloqueado")), None)
        if primera: primera["activo"] = True
    return out


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
