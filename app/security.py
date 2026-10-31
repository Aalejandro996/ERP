import hashlib
from functools import wraps

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from flask import abort, g
from sqlalchemy import select

from . import legacy
from .extensions import db
from .models import Empresa, HistorialClave, UsuarioEmpresa
from .planes import permiso_habilitado

_ph = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2)  # Argon2id
_HASH_FALSO = _ph.hash("relleno-para-tiempo-constante")
HISTORIAL_CLAVES = 5


def hashear(clave: str) -> str:
    return _ph.hash(clave)


def sha256(texto: str) -> str:
    return hashlib.sha256(texto.encode()).hexdigest()


def verificar_clave(clave: str, guardado: str | None) -> tuple[bool, bool]:
    """Devuelve (coincide, necesita_rehash). Con usuario inexistente se gasta el mismo tiempo."""
    if guardado is None:
        try: _ph.verify(_HASH_FALSO, clave)
        except VerificationError: pass
        return False, False
    if legacy.es_legacy(guardado):  # formato escrito por scripts/migrar_sqlite_a_pg.py
        return legacy.verificar_legacy(clave, guardado), True  # siempre re-hashear a Argon2id tras un login correcto
    try:
        _ph.verify(guardado, clave)
        return True, _ph.check_needs_rehash(guardado)
    except (VerifyMismatchError, InvalidHashError, VerificationError):
        return False, False


COMUNES = {"admin123", "password1234", "contrasena123", "contraseña123", "123456789012", "qwertyuiop12", "venezuela123", "administrador"}


def politica_clave(clave: str, usuario: str = "", email: str = "") -> str | None:
    if len(clave) < 12: return "La contraseña debe tener al menos 12 caracteres"
    if len(clave) > 128: return "La contraseña no puede superar 128 caracteres"
    bajo = clave.lower()
    if usuario and usuario.lower() in bajo: return "La contraseña no puede contener el usuario"
    if email and email.split("@")[0].lower() in bajo and len(email.split("@")[0]) >= 4: return "La contraseña no puede contener su correo"
    if bajo in COMUNES: return "Contraseña demasiado común"
    if len(set(clave)) < 6: return "Contraseña demasiado repetitiva"
    clases = sum((any(c.islower() for c in clave), any(c.isupper() for c in clave), any(c.isdigit() for c in clave), any(not c.isalnum() for c in clave)))
    if clases < 3: return "Use al menos 3 de: minúsculas, mayúsculas, números y símbolos"
    return None


def clave_reutilizada(usuario, clave: str) -> bool:
    previas = db.session.execute(select(HistorialClave.password_hash).where(HistorialClave.usuario_id == usuario.id)
                                 .order_by(HistorialClave.id.desc()).limit(HISTORIAL_CLAVES)).scalars().all()
    return any(verificar_clave(clave, h)[0] for h in [usuario.password_hash, *previas] if h and not legacy.es_legacy(h))


def fijar_clave(usuario, clave: str):
    """Cambia la contraseña, la guarda en el historial e invalida TODAS las sesiones abiertas del usuario."""
    usuario.password_hash = hashear(clave)
    db.session.add(HistorialClave(usuario_id=usuario.id, password_hash=usuario.password_hash))
    usuario.sesion_version += 1
    usuario.intentos_fallidos, usuario.bloqueado_hasta = 0, None


# ---- RBAC: matriz de permisos centralizada. Un permiso solo vale si además el PLAN de la licencia trae su módulo ----
PERMISOS = {
    "Maestro": {"plataforma"},  # gestiona clientes y parámetros legales globales; NO ve libros de ningún cliente
    "Administrador": {"*"},
    "Contador": {"asientos.ver", "asientos.crear", "asientos.anular", "periodos.cerrar", "cuentas.editar", "reportes.ver",
                 "cartera.ver", "cartera.operar", "inventario.ver", "inventario.operar", "fiscal.ver", "fiscal.operar", "config.contable",
                 "facturacion.ver", "facturacion.emitir", "facturacion.anular", "facturacion.configurar",
                 "compras.ver", "compras.operar", "whatsapp.ver", "whatsapp.operar"},
    "Auxiliar": {"asientos.ver", "asientos.crear", "reportes.ver", "cartera.ver", "cartera.operar", "inventario.ver", "inventario.operar",
                 "fiscal.ver", "facturacion.ver", "facturacion.emitir", "compras.ver", "compras.operar", "whatsapp.ver", "whatsapp.operar"},
    "Auditor Financiero": {"asientos.ver", "reportes.ver", "cartera.ver", "inventario.ver", "fiscal.ver", "facturacion.ver",
                           "auditoria.ver", "auditoria.crear", "compras.ver"},  # solo lectura + hallazgos (segregación de funciones)
    "Cajero": {"facturacion.ver", "facturacion.emitir", "inventario.ver", "whatsapp.ver", "whatsapp.operar"},
}
MFA_OBLIGATORIO = set(PERMISOS)  # MFA para TODOS los roles (sistema expuesto en Internet)


def modulos_actuales() -> frozenset:
    return getattr(g, "modulos", frozenset())


def tiene_permiso(usuario, permiso: str) -> bool:
    p = PERMISOS.get(usuario.rol, set())
    if usuario.rol == "Maestro":
        return permiso in p
    if permiso == "plataforma":
        return False
    return ("*" in p or permiso in p) and permiso_habilitado(permiso, modulos_actuales())


def requiere(permiso: str):
    def deco(f):
        @wraps(f)
        def interior(*a, **kw):
            u = getattr(g, "usuario", None)
            if u is None: abort(401)
            if not tiene_permiso(u, permiso): abort(403)
            return f(*a, **kw)
        return interior
    return deco


def empresa_autorizada(empresa_id: int) -> int:
    """Aislamiento: la empresa debe ser de la organización del usuario Y (salvo Administrador) estar asignada a él.
    Se verifica en la BD en CADA petición; además PostgreSQL filtra por organización (RLS)."""
    u = getattr(g, "usuario", None)
    if u is None: abort(401)
    emp = db.session.get(Empresa, empresa_id)
    if emp is None or u.organizacion_id is None or emp.organizacion_id != u.organizacion_id:
        abort(404)  # 404 y no 403: no revela que exista
    if u.rol != "Administrador" and db.session.get(UsuarioEmpresa, (u.id, empresa_id)) is None:
        abort(404)
    return empresa_id
