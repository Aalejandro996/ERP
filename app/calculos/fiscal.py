"""Cálculos fiscales de Venezuela (ISLR, retenciones, ajuste por inflación). Lógica pura con Decimal.

ADVERTENCIAS heredadas del sistema original (se mantienen): el ajuste por inflación es simplificado
(no pondera por fecha de cada movimiento); los Sujetos Pasivos Especiales no pueden aplicarlo desde 2015;
la estructura del TXT de retenciones IVA debe verificarse contra la versión vigente del SENIAT.
"""
from decimal import ROUND_HALF_UP, Decimal

D2 = Decimal("0.01")


def q2(v) -> Decimal:
    return Decimal(str(v)).quantize(D2, ROUND_HALF_UP)


class ErrorFiscal(ValueError):
    pass


def calcular_tarifa_islr(enriquecimiento_bs, valor_ut, tramos):
    """tramos: [(desde_ut, hasta_ut, porcentaje, sustraendo_ut)] — Tarifa N° 2, Art. 52 LISLR."""
    valor_ut, enr = Decimal(str(valor_ut)), Decimal(str(enriquecimiento_bs))
    if valor_ut <= 0:
        raise ErrorFiscal("No hay un valor de Unidad Tributaria configurado")
    enr_ut = enr / valor_ut
    for desde, hasta, pct, sust in sorted(tramos, key=lambda t: t[0]):
        if Decimal(str(desde)) <= enr_ut <= Decimal(str(hasta)):
            islr_ut = enr_ut * Decimal(str(pct)) / 100 - Decimal(str(sust))
            return {"enriquecimiento_neto_bs": q2(enr), "valor_ut": valor_ut, "enriquecimiento_neto_ut": q2(enr_ut),
                    "porcentaje_aplicado": Decimal(str(pct)), "sustraendo_ut": Decimal(str(sust)),
                    "islr_ut": q2(islr_ut), "islr_bs": q2(max(islr_ut, 0) * valor_ut)}
    raise ErrorFiscal("El enriquecimiento no cae en ningún tramo de la tarifa configurada")


def calcular_retencion_islr(monto_pagado, tipo_beneficiario, pct_juridica, pct_natural, sustraendo_ut, minimo_ut, valor_ut):
    if tipo_beneficiario not in ("Juridica", "Natural"):
        raise ErrorFiscal("Tipo de beneficiario inválido")
    monto, ut = Decimal(str(monto_pagado)), Decimal(str(valor_ut))
    if monto < Decimal(str(minimo_ut)) * ut:
        return {"monto_retenido": Decimal("0.00"), "motivo": f"Pago por debajo del mínimo sujeto a retención ({minimo_ut} UT)"}
    pct = Decimal(str(pct_juridica if tipo_beneficiario == "Juridica" else pct_natural))
    sust_bs = Decimal(str(sustraendo_ut)) * ut
    return {"porcentaje_aplicado": pct, "sustraendo_bs": q2(sust_bs), "monto_pagado": q2(monto),
            "monto_retenido": q2(max(monto * pct / 100 - sust_bs, 0))}


def reajuste_inflacion(inpc_inicio, inpc_cierre, activos_no_monetarios, patrimonio_neto, es_spe=False):
    if es_spe:
        raise ErrorFiscal("Sujeto Pasivo Especial: desde 2015 no puede aplicar el Ajuste por Inflación Fiscal (LISLR)")
    ini, fin = Decimal(str(inpc_inicio or 0)), Decimal(str(inpc_cierre or 0))
    if ini <= 0 or fin <= 0:
        raise ErrorFiscal("No hay índices INPC configurados para el ejercicio")
    var = (fin - ini) / ini
    aj_act = q2(Decimal(str(activos_no_monetarios)) * var)
    aj_pat = q2(Decimal(str(patrimonio_neto)) * var)
    return {"inpc_inicio": ini, "inpc_cierre": fin, "variacion_inpc_pct": q2(var * 100),
            "ajuste_activos_no_monetarios": aj_act, "ajuste_patrimonio": aj_pat,
            "reajuste_regular_por_inflacion": q2(aj_act - aj_pat)}


def islr_completo(utilidad_contable, sumas, restas, reajuste, valor_ut, tramos, rebajas):
    """Utilidad ± conciliación ± reajuste = enriquecimiento gravable → tarifa → (−) rebajas = ISLR neto."""
    enr = q2(Decimal(str(utilidad_contable)) + Decimal(str(sumas)) - Decimal(str(restas)) + Decimal(str(reajuste)))
    base = {"utilidad_contable": q2(utilidad_contable), "total_sumas": q2(sumas), "total_restas": q2(restas),
            "ajuste_inflacion": q2(reajuste), "enriquecimiento_neto_gravable": enr}
    if enr <= 0:
        return {**base, "islr_determinado_bs": Decimal("0.00"), "total_rebajas": Decimal("0.00"),
                "islr_neto_a_pagar": Decimal("0.00"), "nota": "Pérdida fiscal: no se genera ISLR"}
    tarifa = calcular_tarifa_islr(enr, valor_ut, tramos)
    reb = q2(sum((Decimal(str(r)) for r in rebajas), Decimal(0)))
    return {**base, "calculo_tarifa": tarifa, "islr_determinado_bs": tarifa["islr_bs"], "total_rebajas": reb,
            "islr_neto_a_pagar": q2(max(tarifa["islr_bs"] - reb, 0))}


def linea_txt_retencion_iva(f: dict) -> str:
    """16 columnas separadas por TAB (Instructivo SENIAT 3.0.0). Dinero con 2 decimales; nunca float."""
    orden = ["rif_contribuyente", "periodo_impositivo", "fecha_documento", "tipo_operacion", "tipo_documento",
             "rif_proveedor", "numero_documento", "numero_control", "monto_documento", "base_imponible",
             "monto_iva", "numero_documento_afectado", "numero_comprobante", "monto_exento", "alicuota", "numero_expediente"]
    dinero = {"monto_documento", "base_imponible", "monto_iva", "monto_exento", "alicuota"}
    out = []
    for k in orden:
        v = f.get(k, "")
        s = str(q2(v)) if k in dinero else str(v if v is not None else "")
        if "\t" in s or "\n" in s:
            raise ErrorFiscal(f"Carácter no permitido en el campo {k}")
        out.append(s)
    return "\t".join(out)
