"""Reglas de los asientos automáticos de Gigadevs (sin base de datos)."""
from decimal import Decimal

from app.contabilidad_gigadevs import PLAN, asientos_de, cuenta_cobro


def _f(**k):
    return {"id": 1, "numero": 1, "fecha": "2026-11-05", "cliente_nombre": "X", "cliente_rif": "J-1", "moneda": "VES", "total": "116",
            "iva": "16", "estado": "Emitida", "forma_pago": "Pago Móvil", **k}


def test_plan_tiene_las_cuentas_que_usan_los_asientos():
    codigos = {c for c, _, _ in PLAN}
    assert {"1.1.2.01", "4.1.1.01", "2.1.2.01", "1.1.1.02", "1.1.1.03", "1.1.1.05"} <= codigos and len(PLAN) == 88


def test_venta_cobro_y_anulacion_cuadran():
    for f in (_f(), _f(estado="Anulada", anulada_en=1_793_000_000.0), _f(iva="0")):
        for _o, _fe, _c, lineas in asientos_de(f):
            assert sum(Decimal(str(d)) for _, d, _ in lineas) == sum(Decimal(str(h)) for _, _, h in lineas)
    assert [o for o, *_ in asientos_de(_f(estado="Anulada"))] == [("fx", 1), ("fx_cobro", 1), ("fx_anulada", 1), ("fx_cobro_anulado", 1)]
    assert [o for o, *_ in asientos_de(_f(forma_pago=""))] == [("fx", 1)]          # sin pago: queda por cobrar


def test_cuenta_de_cobro_segun_la_forma_de_pago():
    assert cuenta_cobro(_f()) == "1.1.1.03"
    assert cuenta_cobro(_f(moneda="USD", forma_pago="Zelle")) == "1.1.1.05"
    assert cuenta_cobro(_f(moneda="USD", forma_pago="Efectivo")) == "1.1.1.02"
