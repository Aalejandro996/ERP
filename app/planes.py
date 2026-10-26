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
    "ERP_CONTABLE": {"nombre": "ERP Contable", "modulos": frozenset({"contabilidad", "administrativo", "auditoria"})},
    "CASH": {"nombre": "Cash — Facturación", "modulos": frozenset({"facturacion", "inventario"})},
    "ERP_ADMIN": {"nombre": "ERP Administrativo", "modulos": frozenset({"administrativo", "inventario"})},
    "ALL_IN_ONE": {"nombre": "All in One", "modulos": frozenset(MODULOS)},
}
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
}


def modulos_de(claims: dict | None) -> frozenset:
    if not claims:
        return frozenset()
    plan = claims.get("plan")
    if plan is None:
        return MODULOS_LEGADO
    return PLANES[plan]["modulos"] if plan in PLANES else frozenset()  # plan desconocido: falla cerrado


def nombre_plan(claims: dict | None) -> str:
    plan = (claims or {}).get("plan")
    return PLANES[plan]["nombre"] if plan in PLANES else ("Licencia anterior a los planes" if claims and plan is None else "—")


def modulo_de_permiso(permiso: str) -> str | None:
    return PERMISO_MODULO.get(permiso.split(".", 1)[0])


def permiso_habilitado(permiso: str, modulos) -> bool:
    m = modulo_de_permiso(permiso)
    return m is None or m in modulos
