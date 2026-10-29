"""Facturación de GIGADEVS por la venta de licencias de ERP (emisor: Gigadevs Corp).

Mismas reglas que la facturación del ERP (Prov. SNAT/2011/00071): numeración consecutiva y única, número de control del
rango asignado por la imprenta autorizada, datos del emisor y del adquirente, base, alícuota e IVA en bolívares, y la
operación en divisas con su equivalente a la tasa BCV. Las facturas son de solo inserción (se anulan, no se borran) y van
encadenadas por SHA-256. Este módulo SOLO emite facturas: no lleva contabilidad.

- Venta de licencia: los precios del catálogo INCLUYEN el IVA; la factura descompone base + IVA sobre el monto cobrado.
- Factura manual («construir la factura»): líneas con precio SIN IVA, como en el ERP.
"""
import hashlib
import time
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import (Column, Float, Integer, MetaData, Numeric, String, Table, Text, insert, select, update)

from app.calculos import facturacion as calc

D2 = Decimal("0.01")
md = MetaData()
config = Table(
    "lic_facturacion_config", md,
    Column("id", Integer, primary_key=True),
    Column("razon_social", Text, nullable=False, default="Gigadevs Corp"), Column("rif", Text, nullable=False, default=""),
    Column("domicilio", Text, nullable=False, default=""), Column("telefono", Text), Column("correo", Text),
    Column("contribuyente_especial", Integer, nullable=False, default=0),
    Column("iva_pct", Numeric(6, 2), nullable=False, default=16), Column("serie", String(3), nullable=False, default=""),
    Column("siguiente", Integer, nullable=False, default=1), Column("control_prefijo", String(4), nullable=False, default="00"),
    Column("control_siguiente", Integer, nullable=False, default=1), Column("control_hasta", Integer, nullable=False, default=0),
    Column("imprenta_razon_social", Text, nullable=False, default=""), Column("imprenta_rif", Text, nullable=False, default=""),
    Column("imprenta_providencia", Text, nullable=False, default=""), Column("fecha_asignacion", Text, nullable=False, default=""))
facturas = Table(
    "lic_facturas", md,
    Column("id", Integer, primary_key=True), Column("numero", Integer, nullable=False), Column("serie", String(3), nullable=False),
    Column("numero_control", String(20), nullable=False, unique=True), Column("fecha", String(10), nullable=False),
    Column("emitida_en", Float, nullable=False), Column("orden_ref", String(32), unique=True), Column("licencia_id", Integer),
    Column("cliente_nombre", Text, nullable=False), Column("cliente_rif", Text, nullable=False), Column("cliente_direccion", Text),
    Column("cliente_correo", Text), Column("moneda", String(4), nullable=False), Column("tasa_bcv", Numeric(18, 6), nullable=False),
    Column("lineas", Text, nullable=False),               # JSON: [{descripcion, cantidad, precio, alicuota, base_bs, iva_bs}]
    Column("exento", Numeric(18, 2), nullable=False), Column("base", Numeric(18, 2), nullable=False), Column("iva_pct", Numeric(6, 2), nullable=False),
    Column("iva", Numeric(18, 2), nullable=False), Column("total", Numeric(18, 2), nullable=False), Column("total_divisa", Numeric(18, 4)),
    Column("forma_pago", Text), Column("emisor", Text, nullable=False),   # JSON: copia fija de los datos del emisor y la imprenta
    Column("estado", String(10), nullable=False, default="Emitida"), Column("motivo_anulacion", Text), Column("anulada_en", Float),
    Column("correo_enviado_en", Float), Column("hash_prev", String(64), nullable=False), Column("hash", String(64), nullable=False))

CAMPOS_CONFIG = ("razon_social", "rif", "domicilio", "telefono", "correo", "contribuyente_especial", "iva_pct", "serie", "control_prefijo",
                 "imprenta_razon_social", "imprenta_rif", "imprenta_providencia", "fecha_asignacion")


class ErrorFacturaPlataforma(ValueError):
    pass


def q2(v): return Decimal(v).quantize(D2, ROUND_HALF_UP)


def instalar(eng):
    md.create_all(eng)
    with eng.begin() as c:
        if c.execute(select(config.c.id).where(config.c.id == 1)).first() is None:
            c.execute(insert(config).values(id=1))


def leer_config(con) -> dict:
    return dict(con.execute(select(config).where(config.c.id == 1)).first()._mapping)


def guardar_config(eng, datos: dict):
    import re
    cambios = {k: (str(v).strip()[:300] if v is not None else None) for k, v in datos.items() if k in CAMPOS_CONFIG}
    if "rif" in cambios and cambios["rif"] and not re.fullmatch(r"[VEJPGC]-\d{8}-\d", cambios["rif"].upper()): raise ErrorFacturaPlataforma("RIF del emisor inválido (J-12345678-9)")
    if "imprenta_rif" in cambios and cambios["imprenta_rif"] and not re.fullmatch(r"[VEJPGC]-\d{8}-\d", cambios["imprenta_rif"].upper()): raise ErrorFacturaPlataforma("RIF de la imprenta inválido")
    if "iva_pct" in cambios:
        p = Decimal(cambios["iva_pct"]); cambios["iva_pct"] = p
        if not (0 <= p < 100): raise ErrorFacturaPlataforma("Alícuota de IVA inválida")
    if "contribuyente_especial" in cambios: cambios["contribuyente_especial"] = 1 if cambios["contribuyente_especial"] in ("1", "True", "true") else 0
    if "serie" in cambios and not re.fullmatch(r"[A-Z0-9]{0,3}", cambios["serie"].upper()): raise ErrorFacturaPlataforma("Serie inválida")
    for k in ("rif", "imprenta_rif", "serie"):
        if cambios.get(k): cambios[k] = cambios[k].upper()
    with eng.begin() as c:
        c.execute(update(config).where(config.c.id == 1).values(**cambios))


def nuevo_rango(eng, desde, hasta, fecha, providencia=None):
    desde, hasta = int(desde), int(hasta)
    with eng.begin() as c:
        cfg = c.execute(select(config).where(config.c.id == 1).with_for_update()).first()
        if desde < 1 or hasta < desde: raise ErrorFacturaPlataforma("Rango de control inválido")
        if desde < cfg.control_siguiente and cfg.control_hasta: raise ErrorFacturaPlataforma("El rango nuevo no puede reutilizar números de control ya emitidos")
        valores = dict(control_siguiente=desde, control_hasta=hasta, fecha_asignacion=str(fecha)[:10])
        if providencia: valores["imprenta_providencia"] = str(providencia)[:300]
        c.execute(update(config).where(config.c.id == 1).values(**valores))


def _hash(prev, d):
    return hashlib.sha256("|".join([prev, d["serie"], str(d["numero"]), d["numero_control"], d["fecha"], d["cliente_rif"],
                                    str(q2(d["total"]))]).encode()).hexdigest()


def numero_completo(f) -> str:
    return f"{f['serie']}{int(f['numero']):08d}"


def emitir(con, *, cliente_nombre, cliente_rif, cliente_direccion, cliente_correo, moneda, tasa_bcv, lineas=None, monto_con_iva=None,
           concepto=None, forma_pago=None, orden_ref=None, licencia_id=None, fecha=None, reloj=time.time) -> dict:
    """Emite UNA factura dentro de la transacción `con`. Venta de licencia: `monto_con_iva` + `concepto`.
    Factura manual: `lineas` [{descripcion, cantidad, precio, descuento_pct, alicuota}] con precios SIN IVA."""
    import json
    import re
    cfg = dict(con.execute(select(config).where(config.c.id == 1).with_for_update()).first()._mapping)
    faltan = [n for k, n in (("rif", "RIF del emisor"), ("domicilio", "domicilio fiscal"), ("imprenta_razon_social", "imprenta"),
                             ("imprenta_rif", "RIF de la imprenta"), ("imprenta_providencia", "providencia de la imprenta")) if not cfg.get(k)]
    if faltan: raise ErrorFacturaPlataforma("Configure los datos de facturación de Gigadevs: " + ", ".join(faltan))
    if not cfg["control_hasta"] or cfg["control_siguiente"] > cfg["control_hasta"]:
        raise ErrorFacturaPlataforma("No hay números de control disponibles: registre un nuevo rango asignado por la imprenta")
    if not re.fullmatch(r"[VEJPGC]-\d{8}-\d", (cliente_rif or "").upper()): raise ErrorFacturaPlataforma(f"RIF/cédula del cliente inválido: {cliente_rif!r}")
    if moneda not in ("VES", "USD", "EUR", "USDT"): raise ErrorFacturaPlataforma("Moneda inválida")
    tasa = Decimal(1) if moneda == "VES" else Decimal(str(tasa_bcv or 0))
    if tasa <= 0: raise ErrorFacturaPlataforma("Falta la tasa BCV del día")
    iva_pct = Decimal(cfg["iva_pct"])
    if monto_con_iva is not None:  # venta de licencia: el precio cobrado ya incluye el IVA
        total_doc = Decimal(str(monto_con_iva))
        total_bs = q2(total_doc * tasa)
        base_bs = q2(total_bs * 100 / (100 + iva_pct))
        lin = [{"descripcion": concepto, "cantidad": "1", "precio": str(q2(total_doc / (1 + iva_pct / 100))), "alicuota": "G",
                "base_bs": str(base_bs), "iva_bs": str(total_bs - base_bs)}]
        exento, base, iva, total, total_divisa = Decimal(0), base_bs, total_bs - base_bs, total_bs, (total_doc if moneda != "VES" else None)
    else:
        r = calc.calcular(lineas or [], {"G": iva_pct, "R": iva_pct, "A": 0}, moneda, tasa)
        if r["base_r"] or r["base_a"]: raise ErrorFacturaPlataforma("Las facturas de Gigadevs usan solo la alícuota general o exento")
        lin = [{"descripcion": l["descripcion"].strip()[:300], "cantidad": str(l["cantidad"]), "precio": str(l["precio"]), "alicuota": l["alicuota"],
                "base_bs": str(l["base_bs"]), "iva_bs": str(l["iva_bs"])} for l in r["lineas"]]
        exento, base, iva, total, total_divisa = r["exento"], r["base_g"], r["iva_g"], r["total"], r["total_divisa"]
    numero, control = cfg["siguiente"], calc.control_fmt(cfg["control_prefijo"], cfg["control_siguiente"])
    con.execute(update(config).where(config.c.id == 1).values(siguiente=numero + 1, control_siguiente=cfg["control_siguiente"] + 1))
    prev = con.execute(select(facturas.c.hash).order_by(facturas.c.id.desc()).limit(1)).scalar() or "0" * 64
    from app.tiempo import hoy_ve
    emisor = {k: (str(cfg[k]) if cfg[k] is not None else "") for k in ("razon_social", "rif", "domicilio", "telefono", "correo", "contribuyente_especial",
                                                                        "imprenta_razon_social", "imprenta_rif", "imprenta_providencia", "fecha_asignacion",
                                                                        "control_prefijo", "control_hasta")}
    fila = dict(numero=numero, serie=cfg["serie"] or "", numero_control=control, fecha=str(fecha or hoy_ve()), emitida_en=reloj(), orden_ref=orden_ref,
                licencia_id=licencia_id, cliente_nombre=str(cliente_nombre)[:200], cliente_rif=cliente_rif.upper(), cliente_direccion=(cliente_direccion or "")[:300],
                cliente_correo=cliente_correo, moneda=moneda, tasa_bcv=tasa, lineas=json.dumps(lin), exento=exento, base=base, iva_pct=iva_pct, iva=iva,
                total=total, total_divisa=total_divisa, forma_pago=(forma_pago or "")[:120], emisor=json.dumps(emisor), estado="Emitida", hash_prev=prev)
    fila["hash"] = _hash(prev, fila)
    fid = con.execute(insert(facturas).values(**fila).returning(facturas.c.id)).scalar()
    return {**fila, "id": fid}


def como_dict(fila) -> dict:
    import json
    d = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in dict(fila._mapping if hasattr(fila, "_mapping") else fila).items()}
    d["lineas"] = json.loads(d["lineas"]) if isinstance(d["lineas"], str) else d["lineas"]
    d["emisor"] = json.loads(d["emisor"]) if isinstance(d["emisor"], str) else d["emisor"]
    d["numero_completo"] = numero_completo(d)
    return d


def verificar_cadena(con):
    prev = "0" * 64
    for f in con.execute(select(facturas).order_by(facturas.c.id)):
        d = dict(f._mapping)
        if d["hash_prev"] != prev or d["hash"] != _hash(prev, d): return False, d["id"]
        prev = d["hash"]
    return True, None
