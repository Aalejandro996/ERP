"""Cálculo de documentos de venta (Venezuela). Lógica pura con Decimal: se prueba sin base de datos.

- Alícuotas por línea: G (general), R (reducida), A (bienes de lujo: general + adicional), E (exento, se marca "(E)").
- La operación puede pactarse en divisas: cada importe se convierte a Bs con la tasa BCV del día (moneda funcional).
- IGTF: lo percibe el Sujeto Pasivo Especial sobre los pagos recibidos en divisas; se cobra APARTE del total.
"""
import hashlib
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

D2, D4 = Decimal("0.01"), Decimal("0.0001")
ALICUOTAS = ("G", "R", "A", "E")
FORMAS_PAGO = ("Efectivo", "Transferencia", "Pago móvil", "Punto de venta", "Depósito", "Zelle", "Otro")
MONEDAS = ("VES", "USD", "EUR")


class ErrorFactura(ValueError):
    pass


def _dec(v, nombre="valor") -> Decimal:
    if isinstance(v, float):
        raise ErrorFactura(f"{nombre}: los montos no pueden llegar como float")
    try:
        d = Decimal(str(v if v not in (None, "") else "0").replace(",", "."))
    except InvalidOperation as exc:
        raise ErrorFactura(f"{nombre} inválido: {v!r}") from exc
    if not d.is_finite():
        raise ErrorFactura(f"{nombre} inválido: {v!r}")
    return d


def q2(v) -> Decimal: return Decimal(v).quantize(D2, ROUND_HALF_UP)


def pct_linea(alicuota, pct) -> Decimal:
    """pct: {"G": 16, "R": 8, "A": 15} (A es el ADICIONAL de lujo, que se suma a la general)."""
    if alicuota == "E": return Decimal(0)
    if alicuota == "A": return _dec(pct["G"]) + _dec(pct["A"])
    return _dec(pct[alicuota])


def calcular(lineas, pct, moneda="VES", tasa=1):
    """lineas: [{cantidad, precio, descuento_pct, alicuota, ...}] en la moneda del documento.
    Devuelve totales en Bs y las líneas con base_bs / iva_bs calculados."""
    if moneda not in MONEDAS: raise ErrorFactura("Moneda no soportada")
    tasa = _dec(tasa, "Tasa BCV")
    if moneda == "VES": tasa = Decimal(1)
    if tasa <= 0: raise ErrorFactura("La tasa de cambio debe ser mayor que cero")
    if not lineas: raise ErrorFactura("El documento debe tener al menos una línea")
    for k in ("G", "R", "A"):
        if k not in pct or _dec(pct[k]) < 0: raise ErrorFactura(f"Alícuota {k} no configurada")
    bases = {k: Decimal(0) for k in ALICUOTAS}
    descuento, salida = Decimal(0), []
    for i, l in enumerate(lineas, 1):
        cant, precio, desc = _dec(l.get("cantidad"), f"Cantidad (línea {i})"), _dec(l.get("precio"), f"Precio (línea {i})"), _dec(l.get("descuento_pct"), f"Descuento (línea {i})")
        alic = l.get("alicuota")
        if alic not in ALICUOTAS: raise ErrorFactura(f"Alícuota inválida en la línea {i}")
        if cant <= 0: raise ErrorFactura(f"La cantidad de la línea {i} debe ser mayor que cero")
        if precio < 0: raise ErrorFactura(f"El precio de la línea {i} no puede ser negativo")
        if not (0 <= desc <= 100): raise ErrorFactura(f"El descuento de la línea {i} debe estar entre 0 y 100 %")
        if not (l.get("descripcion") or "").strip(): raise ErrorFactura(f"La línea {i} requiere descripción")
        bruto = cant * precio
        neto = q2(bruto * (100 - desc) / 100)
        base_bs = q2(neto * tasa)
        descuento += q2((bruto - neto) * tasa)
        bases[alic] += base_bs
        salida.append({**l, "cantidad": cant, "precio": precio, "descuento_pct": desc,
                       "base_bs": base_bs, "iva_bs": q2(base_bs * pct_linea(alic, pct) / 100)})
    pg, pr, pa = _dec(pct["G"]), _dec(pct["R"]), _dec(pct["A"])
    iva_g, iva_r, iva_a = q2(bases["G"] * pg / 100), q2(bases["R"] * pr / 100), q2(bases["A"] * (pg + pa) / 100)
    total = bases["E"] + bases["G"] + bases["R"] + bases["A"] + iva_g + iva_r + iva_a
    return {"lineas": salida, "moneda": moneda, "tasa_bcv": tasa,
            "exento": bases["E"], "base_g": bases["G"], "iva_g": iva_g, "base_r": bases["R"], "iva_r": iva_r,
            "base_a": bases["A"], "iva_a": iva_a, "pct_g": pg, "pct_r": pr, "pct_a": pa,
            "descuento": descuento, "total": q2(total),
            "total_divisa": q2(total / tasa) if moneda != "VES" else None}


def aplicar_pagos(total_bs, pagos, tasa, igtf_pct=0, aplica_igtf=False):
    """pagos: [{forma, moneda, monto, referencia}] — monto APLICADO al documento en la moneda del pago.
    Valida que cubran exactamente el total (tolerancia de 1 céntimo por pago en divisas, absorbida por el último)
    y calcula el IGTF sobre los pagos en divisas. Devuelve (pagos_normalizados, igtf_base, igtf_total)."""
    tasa, total_bs, igtf_pct = _dec(tasa), _dec(total_bs), _dec(igtf_pct)
    norm, suma, divisas = [], Decimal(0), []
    for i, p in enumerate(pagos, 1):
        if p.get("forma") not in FORMAS_PAGO: raise ErrorFactura(f"Forma de pago inválida (pago {i})")
        mon = p.get("moneda")
        if mon not in MONEDAS: raise ErrorFactura(f"Moneda inválida (pago {i})")
        m = q2(_dec(p.get("monto"), f"Monto (pago {i})"))
        if m <= 0: raise ErrorFactura(f"El monto del pago {i} debe ser mayor que cero")
        mbs = m if mon == "VES" else q2(m * tasa)
        x = {"forma": p["forma"], "moneda": mon, "monto": m, "monto_bs": mbs, "igtf_bs": Decimal(0), "referencia": (p.get("referencia") or "")[:120]}
        norm.append(x); suma += mbs
        if mon != "VES": divisas.append(x)
    if not norm: raise ErrorFactura("Registre al menos un pago (o emita a crédito)")
    dif = total_bs - suma
    if dif != 0:
        if divisas and abs(dif) <= D2 * len(divisas):
            divisas[-1]["monto_bs"] += dif   # redondeo de conversión
        else:
            raise ErrorFactura(f"Los pagos (Bs {q2(suma)}) no cubren exactamente el total (Bs {q2(total_bs)})")
    base = sum((x["monto_bs"] for x in divisas), Decimal(0)) if aplica_igtf else Decimal(0)
    for x in divisas:
        if aplica_igtf: x["igtf_bs"] = q2(x["monto_bs"] * igtf_pct / 100)
    return norm, q2(base), q2(sum((x["igtf_bs"] for x in divisas), Decimal(0)))


def igtf_de_pago(monto_bs, moneda, igtf_pct, aplica_igtf) -> Decimal:
    return q2(_dec(monto_bs) * _dec(igtf_pct) / 100) if aplica_igtf and moneda != "VES" else Decimal("0.00")


def numero_fmt(n) -> str: return f"{int(n):08d}"
def control_fmt(prefijo, n) -> str: return f"{prefijo}-{int(n):08d}"


def hash_documento(prev: str, d: dict) -> str:
    campos = [prev, d["tipo"], d["serie"], str(d["numero"]), d["numero_control"], str(d["fecha_emision"]), d["cliente_rif"],
              str(q2(d["total"])), str(q2(d["igtf"]))]
    return hashlib.sha256("|".join(campos).encode()).hexdigest()


def signo(tipo) -> int:
    """Las notas de crédito restan en el libro de ventas y en los asientos."""
    return -1 if tipo == "NC" else 1


def claves_asiento(condicion, iva, igtf):
    """Cuentas de ConfigAsientos necesarias para contabilizar el documento."""
    c = ["ventas", "caja_banco" if condicion == "Contado" else "cxc"]
    if iva: c.append("iva_debito_fiscal")
    if igtf: c.append("igtf_por_pagar")
    return c


def asiento_documento(cfg, tipo, condicion, base, iva, igtf=0):
    """FAC/ND: Caja (o CxC) al debe; Ventas, IVA débito fiscal e IGTF por pagar al haber. NC: lo inverso."""
    base, iva, igtf = q2(_dec(base)), q2(_dec(iva)), q2(_dec(igtf))
    contra = cfg["cuenta_caja_banco"] if condicion == "Contado" else cfg["cuenta_cxc"]
    lineas = [(contra, base + iva + igtf, 0), (cfg["cuenta_ventas"], 0, base)]
    if iva: lineas.append((cfg["cuenta_iva_debito_fiscal"], 0, iva))
    if igtf: lineas.append((cfg["cuenta_igtf_por_pagar"], 0, igtf))
    lineas = [l for l in lineas if l[1] or l[2]]
    return [(c, h, d) for c, d, h in lineas] if tipo == "NC" else lineas
