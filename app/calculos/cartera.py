"""CxC / CxP: antigüedad de saldos, provisión de incobrables y plantillas de asientos automáticos."""
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

D2 = Decimal("0.01")


def _d(v): return Decimal(str(v or 0))
def _q(v): return _d(v).quantize(D2, ROUND_HALF_UP)


def dias_vencido(fecha_corte: date, vencimiento: date) -> int:
    return max((fecha_corte - vencimiento).days, 0)


def provision(docs, tramos, fecha_corte: date):
    """docs: [(id, deudor, numero, vencimiento(date), saldo)]; tramos: [(desde, hasta, pct)]."""
    detalle, total = [], Decimal(0)
    for did, deudor, num, venc, saldo in docs:
        dias = dias_vencido(fecha_corte, venc)
        pct = next((_d(p) for d, h, p in sorted(tramos) if d <= dias <= h), Decimal(0))
        prov = _q(_d(saldo) * pct / 100)
        total += prov
        detalle.append((did, deudor, num, dias, _q(saldo), pct, prov))
    return detalle, _q(total)


# ---- Plantillas de asientos (devuelven [(cuenta, debe, haber)]). El servicio decide si hay config completa. ----
def asiento_venta(cfg, monto, base, iva):
    monto, base, iva = _q(monto), _q(base), _q(iva)
    if iva and cfg.get("cuenta_iva_debito_fiscal"):
        if base + iva != monto:
            raise ValueError("base + IVA debe ser igual al monto total")
        return [(cfg["cuenta_cxc"], monto, 0), (cfg["cuenta_ventas"], 0, base), (cfg["cuenta_iva_debito_fiscal"], 0, iva)]
    return [(cfg["cuenta_cxc"], monto, 0), (cfg["cuenta_ventas"], 0, monto)]


def asiento_compra(cfg, monto, base, iva):
    monto, base, iva = _q(monto), _q(base), _q(iva)
    if iva and cfg.get("cuenta_iva_credito_fiscal"):
        if base + iva != monto:
            raise ValueError("base + IVA debe ser igual al monto total")
        return [(cfg["cuenta_compras_gastos"], base, 0), (cfg["cuenta_iva_credito_fiscal"], iva, 0), (cfg["cuenta_cxp"], 0, monto)]
    return [(cfg["cuenta_compras_gastos"], monto, 0), (cfg["cuenta_cxp"], 0, monto)]


def asiento_cobro(cfg, monto):  return [(cfg["cuenta_caja_banco"], _q(monto), 0), (cfg["cuenta_cxc"], 0, _q(monto))]
def asiento_pago(cfg, monto):   return [(cfg["cuenta_cxp"], _q(monto), 0), (cfg["cuenta_caja_banco"], 0, _q(monto))]
def asiento_entrada_inv(cfg, valor, credito=False):
    contra = cfg["cuenta_cxp"] if credito else cfg["cuenta_caja_banco"]
    return [(cfg["cuenta_inventario"], _q(valor), 0), (contra, 0, _q(valor))]
def asiento_costo_venta(cfg, costo): return [(cfg["cuenta_costo_venta"], _q(costo), 0), (cfg["cuenta_inventario"], 0, _q(costo))]
def asiento_provision_incobrables(cfg, monto):
    return [(cfg["cuenta_gasto_incobrables"], _q(monto), 0), (cfg["cuenta_provision_incobrables"], 0, _q(monto))]
def asiento_provision_islr(cfg, monto):
    return [(cfg["cuenta_gasto_islr"], _q(monto), 0), (cfg["cuenta_islr_por_pagar"], 0, _q(monto))]
