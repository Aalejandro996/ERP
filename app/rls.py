"""Aislamiento entre clientes a nivel de PostgreSQL (Row-Level Security, migración 002).

Cada transacción fija `app.org_id` (SET LOCAL) con la organización del usuario autenticado. Las políticas RLS solo
dejan ver/insertar filas de esa organización: aunque un error de programación olvidara un filtro, la base de datos no
devuelve datos de otro cliente. Sin usuario (o Maestro) el valor queda vacío y no se ve ningún dato de negocio.
Nota: RLS no aplica al dueño de las tablas; en producción la app debe conectar con el rol limitado `erp_app`."""
from flask import g, has_request_context
from sqlalchemy import event, text
from sqlalchemy.orm import Session

_SQL = text("SELECT set_config('app.org_id', :v, true)")


def _valor() -> str:
    if not has_request_context():
        return ""
    org = getattr(g, "org_rls", None)
    return str(int(org)) if org else ""


def instalar():
    @event.listens_for(Session, "after_begin")
    def _fijar(session, transaction, connection):  # noqa: ARG001
        if connection.dialect.name == "postgresql":
            connection.execute(_SQL, {"v": _valor()})


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
    db.session.execute(_SQL, {"v": _valor()})
