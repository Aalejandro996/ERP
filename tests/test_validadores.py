from decimal import Decimal
import pytest
from app.contabilidad.validadores import ErrorAsiento, validar_lineas, parsear_fecha


def lanza(f, *a):
    try: f(*a)
    except ErrorAsiento: return True
    return False


def test_asiento_cuadrado():
    r = validar_lineas([("1.1", "100.10", 0), ("4.1", 0, "100.10")])
    assert sum(l[1] for l in r) == sum(l[2] for l in r) == Decimal("100.10")


def test_sin_error_de_punto_flotante():
    # con float, 0.1+0.2 != 0.3: aquí debe cuadrar exacto
    assert validar_lineas([("1", "0.1", 0), ("1", "0.2", 0), ("2", 0, "0.3")])


def test_rechazos():
    assert lanza(validar_lineas, [("1", "100", 0), ("2", 0, "99.99")])      # descuadre
    assert lanza(validar_lineas, [("1", "100", 0)])                          # una línea
    assert lanza(validar_lineas, [("1", "5", "5"), ("2", 0, "0")])           # debe y haber
    assert lanza(validar_lineas, [("1", "-5", 0), ("2", 0, "-5")])           # negativos
    assert lanza(validar_lineas, [("1", 100.0, 0), ("2", 0, 100.0)])         # float
    assert lanza(validar_lineas, [("1", "abc", 0), ("2", 0, "1")])           # basura
    assert lanza(validar_lineas, [("1", "NaN", 0), ("2", 0, "NaN")])         # NaN
    assert lanza(parsear_fecha, "31/12/2026")
