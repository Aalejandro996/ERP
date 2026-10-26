import hashlib

from flask import g, has_request_context, request
from sqlalchemy import insert, select

from .extensions import db
from .models import AuditLog, _ahora

GENESIS = "0" * 64
_ORG_ACTUAL = object()


def _org_actual():
    u = getattr(g, "usuario", None) if has_request_context() else None
    return u.organizacion_id if u is not None else None


def registrar(usuario: str, accion: str, detalles: str = "", org=_ORG_ACTUAL):
    """Añade un eslabón a la cadena de la organización (NULL = eventos de plataforma).
    Se serializa con un lock de aviso por organización para que la cadena no se bifurque."""
    org = _org_actual() if org is _ORG_ACTUAL else org
    db.session.execute(db.text("SELECT pg_advisory_xact_lock(42, :o)"), {"o": org or 0})
    # El eslabón anterior se lee con RLS fijado a ESA organización (p. ej. un login fallido aún no tiene sesión) y luego
    # se restaura el contexto. La política de audit_log permite insertar pero solo leer la cadena propia.
    previo = db.session.execute(db.text("SELECT current_setting('app.org_id', true)")).scalar() or ""
    db.session.execute(db.text("SELECT set_config('app.org_id', :v, true)"), {"v": str(org or "")})
    prev = db.session.execute(select(AuditLog.hash).where(AuditLog.organizacion_id.is_not_distinct_from(org))
                              .order_by(AuditLog.id.desc()).limit(1)).scalar() or GENESIS
    db.session.execute(db.text("SELECT set_config('app.org_id', :v, true)"), {"v": previo})
    ts = _ahora()
    h = hashlib.sha256("|".join([prev, ts.isoformat(), usuario, accion, detalles]).encode()).hexdigest()
    # INSERT sin RETURNING: con RLS, RETURNING exigiría poder LEER la fila (p. ej. el Maestro creando un cliente)
    db.session.execute(insert(AuditLog).values(organizacion_id=org, ts=ts, usuario=usuario, ip=request.remote_addr if has_request_context() else None,
                                               accion=accion, detalles=detalles, hash_prev=prev, hash=h))


def verificar_cadena(org=_ORG_ACTUAL) -> tuple[bool, int | None]:
    org = _org_actual() if org is _ORG_ACTUAL else org
    prev = GENESIS
    for r in db.session.execute(select(AuditLog).where(AuditLog.organizacion_id.is_not_distinct_from(org)).order_by(AuditLog.id)).scalars():
        esperado = hashlib.sha256("|".join([prev, r.ts.isoformat(), r.usuario, r.accion, r.detalles or ""]).encode()).hexdigest()
        if r.hash_prev != prev or r.hash != esperado:
            return False, r.id
        prev = r.hash
    return True, None
