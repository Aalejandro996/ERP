"""Planes de licencia → módulos habilitados. El plan viaja FIRMADO en el token de licencia (claim "plan"):
ni el cliente ni un usuario con acceso a la BD pueden ampliarlo. Sin dependencias (probable de forma aislada)."""

MODULOS = {
    "contabilidad": "Contabilidad y fiscal",           # plan de cuentas, diario, mayor, cierres, estados VEN-NIF, ISLR, retenciones, libros
    "administrativo": "Administración",                 # CxC, CxP, bancos/tesorería
    "auditoria": "Auditoría financiera",                # pruebas de integridad, hallazgos, papeles de trabajo, bitácora
    "inventario": "Inventario",                         # productos, kardex Promedio/PEPS
    "facturacion": "Facturación",                       # facturas, NC/ND, series y control, libro de ventas
}

PLANES = {
    "ERP_CONTABLE": {"nombre": "Contax Contable", "modulos": frozenset({"contabilidad", "administrativo", "auditoria"})},
    "CASH": {"nombre": "Cash — Facturación", "modulos": frozenset({"facturacion", "inventario"})},
    "ERP_ADMIN": {"nombre": "Contax Administrativo", "modulos": frozenset({"administrativo", "inventario"})},
    "ALL_IN_ONE": {"nombre": "All in One", "modulos": frozenset(MODULOS)},
    # A medida: el Maestro elige los módulos y viajan FIRMADOS en el token (claim "modulos")
    "PERSONALIZADO": {"nombre": "Personalizado", "modulos": frozenset()},
    # Prueba gratuita de 72 horas (una por organización): Facturación e Inventario
    "DEMO": {"nombre": "Demo 72 h — Facturación e Inventario", "modulos": frozenset({"facturacion", "inventario"})},
}
PLANES_TIENDA = ("ERP_CONTABLE", "CASH", "ERP_ADMIN", "ALL_IN_ONE")   # los que se venden con precio fijo en la tienda (+ combos)
DEMO_HORAS = 72
# Licencias emitidas antes de existir los planes (token sin claim "plan"): conservan lo que el sistema ya ofrecía
# (todo menos facturación) hasta que el proveedor las reemita con un plan. Nadie pierde funciones al actualizar.
MODULOS_LEGADO = frozenset({"contabilidad", "administrativo", "auditoria", "inventario"})

# Prefijo del permiso RBAC → módulo de licencia que lo habilita. Lo no listado (p. ej. "admin") es núcleo.
PERMISO_MODULO = {
    "asientos": "contabilidad", "cuentas": "contabilidad", "periodos": "contabilidad", "reportes": "contabilidad",
    "fiscal": "contabilidad", "config": "contabilidad",
    "cartera": "administrativo",
    "auditoria": "auditoria",
    "inventario": "inventario",
    "facturacion": "facturacion",
    # Funciones compartidas: basta con que el plan traiga UNO de estos módulos
    "compras": ("administrativo", "inventario", "facturacion"),      # órdenes de compra y proveedores
    "whatsapp": ("facturacion", "inventario"),                       # bot de WhatsApp y pedidos
}


def modulos_de(claims: dict | None) -> frozenset:
    if not claims:
        return frozenset()
    plan = claims.get("plan")
    if plan is None:
        return MODULOS_LEGADO
    if plan == "PERSONALIZADO":
        return frozenset(m for m in (claims.get("modulos") or []) if m in MODULOS)
    return PLANES[plan]["modulos"] if plan in PLANES else frozenset()  # plan desconocido: falla cerrado


def nombre_plan(claims: dict | None) -> str:
    plan = (claims or {}).get("plan")
    if plan == "PERSONALIZADO":
        return "Personalizado (" + ", ".join(MODULOS[m] for m in sorted(modulos_de(claims))) + ")"
    return PLANES[plan]["nombre"] if plan in PLANES else ("Licencia anterior a los planes" if claims and plan is None else "—")


def modulo_de_permiso(permiso: str):
    """Módulo (str) o módulos alternativos (tupla) que habilitan el permiso; None = núcleo."""
    return PERMISO_MODULO.get(permiso.split(".", 1)[0])


def permiso_habilitado(permiso: str, modulos) -> bool:
    m = modulo_de_permiso(permiso)
    if m is None: return True
    return any(x in modulos for x in m) if isinstance(m, tuple) else m in modulos
