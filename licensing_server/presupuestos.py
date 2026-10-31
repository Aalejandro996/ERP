"""Presupuestos de GIGADEVS para sus clientes (licencias de Contax, combos, módulos y servicios a la medida).

Un presupuesto NO es una factura: no consume numeración fiscal. Lleva su propio consecutivo (P-000001), vigencia y estado.
Las líneas del catálogo se cotizan al PRECIO VIGENTE (mensual o anual, según elija el cliente). Como los precios del
catálogo incluyen IVA, la línea guarda su precio sin IVA para que el total del presupuesto sea exactamente el del catálogo.
Las líneas de servicios (implementación, integraciones, soporte, cambios) se cargan sin IVA."""
import json
import time
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import Column, Float, Integer, MetaData, Numeric, String, Table, Text, func, insert, select, update

D2 = Decimal("0.01")
ESTADOS = ("Emitido", "Enviado", "Aceptado", "Rechazado", "Anulado")
md = MetaData()
presupuestos = Table(
    "lic_presupuestos", md,
    Column("id", Integer, primary_key=True), Column("numero", Integer, nullable=False, unique=True),
    Column("fecha", String(10), nullable=False), Column("valido_hasta", String(10), nullable=False),
    Column("organizacion_ref", Text), Column("cliente_nombre", Text, nullable=False), Column("cliente_rif", Text),
    Column("cliente_correo", Text), Column("cliente_direccion", Text), Column("cliente_telefono", Text), Column("atencion", Text),
    Column("titulo", Text), Column("periodo", String(10), nullable=False, default="anual"),
    Column("lineas", Text, nullable=False),            # JSON [{descripcion, detalle, cantidad, precio, alicuota, monto}] — USD sin IVA
    Column("subtotal", Numeric(14, 2), nullable=False), Column("iva_pct", Numeric(6, 2), nullable=False),
    Column("iva", Numeric(14, 2), nullable=False), Column("total", Numeric(14, 2), nullable=False),
    Column("condiciones", Text), Column("estado", String(10), nullable=False, default="Emitido"),
    Column("creado_por", Text), Column("creado_en", Float, nullable=False), Column("correo_enviado_en", Float))


def instalar(eng):
    md.create_all(eng)


def q2(v): return Decimal(str(v)).quantize(D2, ROUND_HALF_UP)


def sin_iva(precio_con_iva, iva_pct) -> Decimal:
    return q2(Decimal(str(precio_con_iva)) * 100 / (100 + Decimal(str(iva_pct))))


def catalogo(st) -> dict:
    """Precios VIGENTES para cotizar: planes, combos y módulos, en mensual y anual. Anual sin precio propio = 10 meses."""
    from .server import DESCUENTO_ANUAL_PCT
    pr = st.precios()
    por = {(p["plan"], p["meses"]): p for p in pr}
    planes = {}
    for (plan, meses), p in por.items():
        x = planes.setdefault(plan, {"plan": plan})
        if meses == 1: x["mensual"] = p["monto_usd"]
        if meses == 12: x["anual"] = p["monto_usd"]
        x.setdefault("topes", f"{p['max_usuarios']} usuarios, {p['max_empresas']} empresas, {p['max_equipos']} equipos")
    for x in planes.values():
        if "mensual" in x and "anual" not in x: x["anual"] = str(q2(Decimal(x["mensual"]) * 10))
    mods = {m: {"modulo": m, "mensual": v, "anual": str(q2(Decimal(v) * 12 * (100 - DESCUENTO_ANUAL_PCT) / 100).quantize(Decimal("1")))}
            for m, v in st.precios_modulos().items()}
    return {"planes": sorted(planes.values(), key=lambda x: x["plan"]), "combos": st.combos(solo_activos=True), "modulos": list(mods.values())}


def crear(con, d: dict, iva_pct, reloj=time.time) -> int:
    nombre = str(d.get("cliente_nombre") or "").strip()[:200]
    if not nombre: raise ValueError("Indique el cliente")
    lineas = []
    for l in d.get("lineas") or []:
        desc = str(l.get("descripcion") or "").strip()[:200]
        if not desc: continue
        cant, precio = Decimal(str(l.get("cantidad") or 1)), q2(l.get("precio") or 0)
        if cant <= 0 or precio < 0: raise ValueError(f"Cantidad o precio inválidos en «{desc}»")
        a = "E" if l.get("alicuota") == "E" else "G"
        lineas.append(dict(descripcion=desc, detalle=str(l.get("detalle") or "").strip()[:400], cantidad=format(cant.normalize(), "f"),
                           precio=str(precio), alicuota=a, monto=str(q2(cant * precio))))
    if not lineas: raise ValueError("El presupuesto necesita al menos una línea")
    iva_pct = Decimal(str(iva_pct))
    gravado = sum((Decimal(l["monto"]) for l in lineas if l["alicuota"] == "G"), Decimal(0))
    subtotal = sum((Decimal(l["monto"]) for l in lineas), Decimal(0))
    iva = q2(gravado * iva_pct / 100)
    hoy = date.fromtimestamp(reloj())
    dias = max(1, min(int(d.get("validez_dias") or 15), 90))
    numero = (con.execute(select(func.max(presupuestos.c.numero))).scalar() or 0) + 1
    periodo = d.get("periodo") if d.get("periodo") in ("mensual", "anual", "unico") else "anual"
    return con.execute(insert(presupuestos).values(
        numero=numero, fecha=hoy.isoformat(), valido_hasta=(hoy + timedelta(days=dias)).isoformat(), organizacion_ref=d.get("organizacion_ref"),
        cliente_nombre=nombre, **{k: (str(d.get(k) or "").strip()[:300] or None) for k in ("cliente_rif", "cliente_correo", "cliente_direccion", "cliente_telefono", "atencion", "titulo")},
        periodo=periodo, lineas=json.dumps(lineas, ensure_ascii=False), subtotal=q2(subtotal), iva_pct=iva_pct, iva=iva, total=q2(subtotal + iva),
        condiciones=str(d.get("condiciones") or "").strip()[:3000] or None, estado="Emitido", creado_por=str(d.get("autor") or "")[:80], creado_en=reloj())
    ).inserted_primary_key[0]


def como_dict(fila) -> dict:
    d = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in dict(fila._mapping).items()}
    d["lineas"] = json.loads(d["lineas"])
    d["numero_txt"] = f"P-{int(d['numero']):06d}"
    return d


def cambiar_estado(con, pid, estado):
    if estado not in ESTADOS: raise ValueError("Estado inválido")
    if not con.execute(update(presupuestos).where(presupuestos.c.id == pid).values(estado=estado)).rowcount: raise LookupError("Presupuesto inexistente")
