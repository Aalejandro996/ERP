"""Aislamiento entre clientes a nivel de PostgreSQL (Row-Level Security, migración 002).

Cada transacción fija `app.org_id` (SET LOCAL) con la organización del usuario autenticado. Las políticas RLS solo
dejan ver/insertar filas de esa organización: aunque un error de programación olvidara un filtro, la base de datos no
devuelve datos de otro cliente. Sin usuario (o Maestro) el valor queda vacío y no se ve ningún dato de negocio.
Nota: RLS no aplica al dueño de las tablas; en producción la app debe conectar con el rol limitado `erp_app`."""
from flask import g, has_request_context
from sqlalchemy import event, text
from sqlalchemy.orm import Session

_SQL = text("SELECT set_config('app.org_id', :v, true), set_config('app.usuario_id', :u, true), set_config('app.maestro', :m, true)")


def _valor() -> str:
    if not has_request_context():
        return ""
    org = getattr(g, "org_rls", None)
    return str(int(org)) if org else ""


def _parametros() -> dict:
    """org (datos de negocio), usuario (su propia cuenta) y maestro (tablas de acceso de todas las cuentas; migración 015)."""
    if not has_request_context(): return {"v": "", "u": "", "m": ""}
    uid = getattr(g, "usuario_rls", None)
    return {"v": _valor(), "u": str(int(uid)) if uid else "", "m": "1" if getattr(g, "maestro_rls", False) else ""}


def instalar():
    @event.listens_for(Session, "after_begin")
    def _fijar(session, transaction, connection):  # noqa: ARG001
        if connection.dialect.name == "postgresql":
            connection.execute(_SQL, _parametros())


def rol_limitado(engine, rol):
    """En plataformas donde la app solo tiene el usuario dueño (Render), cada conexión hace SET ROLE a un rol sin
    privilegios de dueño: así RLS aplica igual que con un usuario aparte. El dueño debe ser miembro del rol
    (lo prepara scripts/inicializar.py)."""
    import re
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", rol):
        raise RuntimeError("DB_ROL_APLICACION inválido")

    @event.listens_for(engine, "connect")
    def _set_role(dbapi_con, _registro):
        cur = dbapi_con.cursor()
        cur.execute(f"SET ROLE {rol}")  # nombre validado arriba (no admite parámetros)
        cur.close()
        dbapi_con.commit()


def fijar_org(db, org_id):
    """Llamar en cuanto se conoce al usuario: aplica a la transacción en curso y a las siguientes de la petición."""
    g.org_rls = org_id
    db.session.execute(_SQL, _parametros())


def ver_usuario(db, usuario_id, maestro=False):
    """La persona que usa (o intenta usar) la cuenta: puede ver su propia fila de usuario, sus enlaces y sus códigos."""
    g.usuario_rls, g.maestro_rls = usuario_id, bool(maestro)
    db.session.execute(_SQL, _parametros())


def de_usuario(db, u):
    """Contexto completo de una persona autenticada: su organización, su cuenta y, si es Maestro, las cuentas de todos."""
    g.org_rls, g.usuario_rls, g.maestro_rls = u.organizacion_id, u.id, (u.rol == "Maestro")
    db.session.execute(_SQL, _parametros())


def _buscar(db, funcion, *args):
    if db.engine.dialect.name != "postgresql": return None
    marcadores = ", ".join(f":a{i}" for i in range(len(args)))
    return db.session.execute(text(f"SELECT {funcion}({marcadores})"), {f"a{i}": a for i, a in enumerate(args)}).scalar()


def usuario_por_nombre(db, valor): return _buscar(db, "acceso_usuario_por_nombre", valor)
def usuario_por_correo(db, valor): return _buscar(db, "acceso_usuario_por_correo", valor)
def usuario_por_token(db, token_hash, proposito): return _buscar(db, "acceso_usuario_por_token", token_hash, proposito)


def cuentas_con(db, nombre, correo) -> list[tuple[int, int | None]]:
    """(usuario_id, organizacion_id) de las cuentas con ese usuario o correo, en TODA la plataforma (sin ver sus datos)."""
    if db.engine.dialect.name != "postgresql": return []
    return [tuple(r) for r in db.session.execute(text("SELECT usuario_id, organizacion_id FROM acceso_organizacion_de(:n, :c)"),
                                                 {"n": nombre or "", "c": correo or ""})]
