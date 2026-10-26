"""Estados financieros a partir de saldos por cuenta. Lógica pura.

rows: iterable de dicts {codigo, nombre, tipo, categoria, monetaria, debe, haber} YA filtrados por empresa
(y por fecha si aplica) en la capa de servicio — aquí nunca se mezclan datos entre empresas.
"""
from decimal import Decimal

CATEGORIAS = ("Operacion", "Inversion", "Financiamiento", "Impuestos", "Discontinuadas")
FECHA_NIIF18 = "2027-01-01"
Z = Decimal(0)


def _s(r, deudor=True):
    d, h = Decimal(str(r["debe"] or 0)), Decimal(str(r["haber"] or 0))
    return d - h if deudor else h - d


def balance(rows):
    act, pas, pat, ing, gas = [], [], [], Z, Z
    for r in rows:
        t = r["tipo"]
        if t == "Activo" and _s(r): act.append((r["codigo"], r["nombre"], _s(r)))
        elif t == "Pasivo" and _s(r, False): pas.append((r["codigo"], r["nombre"], _s(r, False)))
        elif t == "Patrimonio" and _s(r, False): pat.append((r["codigo"], r["nombre"], _s(r, False)))
        elif t == "Ingreso": ing += _s(r, False)
        elif t == "Gasto": gas += _s(r)
    utilidad = ing - gas
    t_act = sum((x[2] for x in act), Z)
    t_pp = sum((x[2] for x in pas), Z) + sum((x[2] for x in pat), Z) + utilidad
    return {"activos": act, "pasivos": pas, "patrimonio": pat, "utilidad": utilidad,
            "total_activos": t_act, "total_pasivo_patrimonio": t_pp, "cuadra": t_act == t_pp}


def estado_resultados(rows, fecha_ref: str):
    if fecha_ref < FECHA_NIIF18:  # NIC 1
        ing = [(r["codigo"], r["nombre"], _s(r, False)) for r in rows if r["tipo"] == "Ingreso" and _s(r, False)]
        gas = [(r["codigo"], r["nombre"], _s(r)) for r in rows if r["tipo"] == "Gasto" and _s(r)]
        ti, tg = sum((x[2] for x in ing), Z), sum((x[2] for x in gas), Z)
        return {"normativa": "NIC 1", "ingresos": ing, "gastos": gas, "total_ingresos": ti,
                "total_gastos": tg, "utilidad_del_ejercicio": ti - tg}
    cats = {c: [] for c in CATEGORIAS}
    for r in rows:
        if r["tipo"] not in ("Ingreso", "Gasto"): continue
        cat = r.get("categoria") if r.get("categoria") in cats else "Operacion"
        m = _s(r, False) if r["tipo"] == "Ingreso" else -_s(r)
        if m: cats[cat].append((r["codigo"], r["nombre"], r["tipo"], m))
    tot = {c: sum((x[3] for x in v), Z) for c, v in cats.items()}
    antes = tot["Operacion"] + tot["Inversion"]
    return {"normativa": "NIIF 18", "categorias": cats, "resultado_operativo": tot["Operacion"],
            "resultado_antes_financiamiento_impuestos": antes,
            "resultado_del_periodo": antes + tot["Financiamiento"] + tot["Impuestos"] + tot["Discontinuadas"]}


def cambios_patrimonio(rows):
    det, ta, td = [], Z, Z
    for r in sorted((x for x in rows if x["tipo"] == "Patrimonio"), key=lambda x: x["codigo"]):
        a, d = Decimal(str(r["haber"] or 0)), Decimal(str(r["debe"] or 0))
        det.append((r["codigo"], r["nombre"], a, d, a - d)); ta += a; td += d
    return det, ta, td


def lineas_cierre(rows, cuenta_patrimonio):
    """Refundición: salda ingresos (debe) y gastos (haber) contra patrimonio. Vacío si no hay nada que cerrar."""
    lineas, util = [], Z
    for r in rows:
        if r["tipo"] == "Ingreso" and _s(r, False):
            lineas.append((r["codigo"], _s(r, False), Z)); util += _s(r, False)
        elif r["tipo"] == "Gasto" and _s(r):
            lineas.append((r["codigo"], Z, _s(r))); util -= _s(r)
    if not lineas: return []
    if util > 0: lineas.append((cuenta_patrimonio, Z, util))
    elif util < 0: lineas.append((cuenta_patrimonio, -util, Z))
    return lineas


def bases_inflacion(rows):
    """(activos no monetarios, patrimonio neto) para el reajuste por inflación."""
    anm = sum((_s(r) for r in rows if r["tipo"] == "Activo" and not r.get("monetaria")), Z)
    pat = sum((_s(r, False) for r in rows if r["tipo"] == "Patrimonio"), Z)
    return anm, pat


def flujo_efectivo(sumas_por_categoria: dict, saldo_inicial=Z):
    o, i, f = (Decimal(str(sumas_por_categoria.get(k, 0))) for k in ("Operacion", "Inversion", "Financiamiento"))
    neto = o + i + f
    return {"flujo_operativo": o, "flujo_inversion": i, "flujo_financiamiento": f,
            "flujo_neto_periodo": neto, "saldo_inicial": saldo_inicial, "saldo_final": saldo_inicial + neto}
