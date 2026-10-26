"""Reglas de partida doble, sin dependencias (probables de forma aislada).

El dinero NUNCA es float: se usa Decimal con 2 decimales (el ERP original usaba REAL).
"""
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")
MAX_MONTO = Decimal("9999999999999.99")


class ErrorAsiento(ValueError):
    pass


def a_decimal(valor) -> Decimal:
    if isinstance(valor, float):
        raise ErrorAsiento("Los montos no pueden llegar como float; envíelos como texto o Decimal")
    try:
        d = Decimal(str(valor or "0")).quantize(CENT, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ErrorAsiento(f"Monto inválido: {valor!r}") from exc
    if not d.is_finite() or d < 0 or d > MAX_MONTO:
        raise ErrorAsiento(f"Monto fuera de rango: {valor!r}")
    return d


def validar_lineas(lineas) -> list[tuple[str, Decimal, Decimal]]:
    """lineas: iterable de (cuenta_codigo, debe, haber). Devuelve líneas normalizadas."""
    norm = []
    for cuenta, debe, haber in lineas:
        d, h = a_decimal(debe), a_decimal(haber)
        if not str(cuenta).strip():
            raise ErrorAsiento("Cada línea requiere una cuenta")
        if d > 0 and h > 0:
            raise ErrorAsiento(f"La cuenta {cuenta} no puede tener debe y haber a la vez")
        if d == 0 and h == 0:
            raise ErrorAsiento(f"La cuenta {cuenta} tiene monto cero")
        norm.append((str(cuenta).strip(), d, h))
    if len(norm) < 2:
        raise ErrorAsiento("Un asiento requiere al menos dos líneas")
    total_d, total_h = sum(l[1] for l in norm), sum(l[2] for l in norm)
    if total_d != total_h:
        raise ErrorAsiento(f"Asiento descuadrado: debe {total_d} ≠ haber {total_h}")
    return norm


def parsear_fecha(valor) -> date:
    if isinstance(valor, date):
        return valor
    try:
        return date.fromisoformat(str(valor))
    except ValueError as exc:
        raise ErrorAsiento(f"Fecha inválida: {valor!r} (use AAAA-MM-DD)") from exc
