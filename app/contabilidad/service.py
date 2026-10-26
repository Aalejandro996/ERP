from sqlalchemy import select

from ..auditoria import registrar
from ..extensions import db
from ..models import Asiento, Cuenta, Movimiento, PeriodoCerrado
from .validadores import ErrorAsiento, parsear_fecha, validar_lineas


def _periodo_cerrado(empresa_id, fecha) -> bool:
    return db.session.execute(select(PeriodoCerrado.id).where(
        PeriodoCerrado.empresa_id == empresa_id, PeriodoCerrado.mes_anio == fecha.strftime("%Y-%m"))
    ).first() is not None


def crear_asiento(empresa_id, usuario, fecha, concepto, lineas, revierte_a=None, commit=True) -> int:
    """Todo o nada en UNA transacción; el cuadre se valida aquí Y lo refuerza un trigger en la BD."""
    f = parsear_fecha(fecha)
    norm = validar_lineas(lineas)
    concepto = (concepto or "").strip()
    if not concepto or len(concepto) > 500:
        raise ErrorAsiento("Concepto obligatorio (máx. 500 caracteres)")
    if _periodo_cerrado(empresa_id, f):
        raise ErrorAsiento("El período contable de esa fecha está cerrado")
    codigos = {c for c, _, _ in norm}
    validas = set(db.session.execute(select(Cuenta.codigo).where(
        Cuenta.empresa_id == empresa_id, Cuenta.codigo.in_(codigos))).scalars())
    if codigos - validas:
        raise ErrorAsiento(f"Cuentas inexistentes para esta empresa: {sorted(codigos - validas)}")
    a = Asiento(empresa_id=empresa_id, fecha=f, concepto=concepto, creado_por=usuario.id, revierte_a=revierte_a)
    db.session.add(a); db.session.flush()
    db.session.add_all(Movimiento(asiento_id=a.id, empresa_id=empresa_id, cuenta_codigo=c, debe=d, haber=h)
                       for c, d, h in norm)
    registrar(usuario.username, "ASIENTO_CREADO", f"empresa={empresa_id} asiento={a.id} fecha={f}")
    if commit:
        db.session.commit()
    return a.id


def anular_asiento(empresa_id, usuario, asiento_id, fecha_contraasiento, motivo) -> int:
    """La contabilidad no se borra: se emite un contra-asiento con debe/haber invertidos."""
    orig = db.session.execute(select(Asiento).where(Asiento.id == asiento_id, Asiento.empresa_id == empresa_id)).scalar_one_or_none()
    if orig is None:
        raise ErrorAsiento("Asiento no encontrado")
    if db.session.execute(select(Asiento.id).where(Asiento.revierte_a == asiento_id)).first():
        raise ErrorAsiento("El asiento ya fue anulado")
    lineas = [(m.cuenta_codigo, m.haber, m.debe) for m in orig.movimientos]
    return crear_asiento(empresa_id, usuario, fecha_contraasiento, f"ANULACIÓN asiento {orig.id}: {motivo}", lineas, revierte_a=orig.id)


def saldos(empresa_id, hasta=None, desde=None):
    """Saldos por cuenta de UNA empresa (el sistema original sumaba movimientos de otras empresas con el mismo código)."""
    from sqlalchemy import func
    mv = select(Movimiento.cuenta_codigo.label("c"), func.sum(Movimiento.debe).label("d"), func.sum(Movimiento.haber).label("h")) \
        .join(Asiento, Asiento.id == Movimiento.asiento_id) \
        .where(Movimiento.empresa_id == empresa_id, Asiento.empresa_id == empresa_id)
    if hasta: mv = mv.where(Asiento.fecha <= hasta)
    if desde: mv = mv.where(Asiento.fecha >= desde)
    mv = mv.group_by(Movimiento.cuenta_codigo).subquery()
    q = select(Cuenta.codigo, Cuenta.nombre, Cuenta.tipo, Cuenta.categoria_niif18, Cuenta.es_partida_monetaria,
               func.coalesce(mv.c.d, 0), func.coalesce(mv.c.h, 0)) \
        .outerjoin(mv, mv.c.c == Cuenta.codigo).where(Cuenta.empresa_id == empresa_id).order_by(Cuenta.codigo)
    return [dict(codigo=r[0], nombre=r[1], tipo=r[2], categoria=r[3], monetaria=r[4], debe=r[5], haber=r[6])
            for r in db.session.execute(q)]
