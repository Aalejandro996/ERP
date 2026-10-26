"""Operaciones de negocio. Regla de oro: documento + asiento + auditoría = UNA transacción (todo o nada).
El sistema original guardaba el documento y luego intentaba el asiento: un fallo dejaba datos a medias."""
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select

from .auditoria import registrar, verificar_cadena
from .calculos import cartera, estados, fiscal, inventario
from .calculos.inventario import Lote
from .contabilidad.service import crear_asiento, saldos
from .contabilidad.validadores import ErrorAsiento, a_decimal, parsear_fecha
from .extensions import db
from .tiempo import hoy_ve
from .models import Asiento, Empresa, Movimiento, PeriodoCerrado
from .models2 import (CobroCxC, ConfigAsientos, DocumentoCxC, DocumentoCxP, Hallazgo, IndiceINPC, LotePeps,
                      MovInventario, PagoCxP, PartidaConciliacion, Producto, RebajaISLR, TarifaISLR, TramoProvision,
                      UnidadTributaria, ConceptoRetencionISLR, ExtractoBancario)


def _cfg(empresa_id, *claves):
    """Devuelve el dict de cuentas si la config automática está activa y completa para `claves`; si no, None."""
    c = db.session.get(ConfigAsientos, empresa_id)
    if not c or not c.generar_automatico:
        return None
    d = c.como_dict()
    return d if all(d.get(f"cuenta_{k}") for k in claves) else None


def _d(v): return a_decimal(v)


# ============================ CxC / CxP ============================
def _alta_doc(Modelo, tipo, empresa_id, u, datos, claves, plantilla, desc):
    f, v = parsear_fecha(datos["fecha_emision"]), parsear_fecha(datos["fecha_vencimiento"])
    if v < f: raise ErrorAsiento("El vencimiento no puede ser anterior a la emisión")
    monto = _d(datos["monto"]); iva = _d(datos.get("monto_iva") or 0)
    base = _d(datos["base_imponible"]) if datos.get("base_imponible") not in (None, "") else monto - iva
    if monto <= 0: raise ErrorAsiento("El monto debe ser mayor que cero")
    doc = Modelo(empresa_id=empresa_id, numero_documento=datos["numero_documento"].strip(), fecha_emision=f,
                 fecha_vencimiento=v, monto=monto, saldo_pendiente=monto, **datos["tercero"])  # (CxP: incluye los datos fiscales)
    db.session.add(doc); db.session.flush()
    cfg = _cfg(empresa_id, *claves)
    msg = "Documento registrado (sin asiento automático: configure las cuentas)."
    if cfg:
        doc.asiento_id = crear_asiento(empresa_id, u, f, f"{desc} - Doc. {doc.numero_documento}", plantilla(cfg, monto, base, iva), commit=False)
        msg = f"Documento registrado y asiento N°{doc.asiento_id} generado."
    registrar(u.username, f"{tipo}_REGISTRADO", f"empresa={empresa_id} doc={doc.numero_documento} monto={monto}")
    db.session.commit()
    return doc, msg


def registrar_cxc(empresa_id, u, d):
    d["tercero"] = dict(deudor_nombre=d["deudor_nombre"].strip(), deudor_rif=(d.get("deudor_rif") or "").strip().upper())
    return _alta_doc(DocumentoCxC, "CXC", empresa_id, u, d, ("cxc", "ventas"), cartera.asiento_venta, "Venta a crédito")


def registrar_cxp(empresa_id, u, d):
    """Además de la deuda, guarda los datos fiscales de la factura del proveedor: con ellos se genera el libro de compras."""
    monto, iva, exento = _d(d["monto"]), _d(d.get("monto_iva") or 0), _d(d.get("exento") or 0)
    base = _d(d["base_imponible"]) if d.get("base_imponible") not in (None, "") else monto - iva - exento
    if base + iva + exento != monto: raise ErrorAsiento(f"Base ({base}) + IVA ({iva}) + exento ({exento}) debe ser igual al total ({monto})")
    alic = _d(d.get("alicuota") or 0)
    if iva and not alic and base: alic = (iva * 100 / base).quantize(Decimal("0.01"))
    if (d.get("tipo_documento") or "FAC") not in ("FAC", "NC", "ND"): raise ErrorAsiento("Tipo de documento inválido")
    d["base_imponible"] = base + exento  # el asiento de compra registra exento + base contra la cuenta de compras/gastos
    d["tercero"] = dict(proveedor_nombre=d["proveedor_nombre"].strip(), proveedor_rif=(d.get("proveedor_rif") or "").strip().upper(),
                        numero_control=(d.get("numero_control") or "").strip()[:30] or None, tipo_documento=d.get("tipo_documento") or "FAC",
                        documento_afectado=(d.get("documento_afectado") or "").strip()[:30] or None, exento=exento, base_imponible=base,
                        alicuota=alic, monto_iva=iva, es_importacion=d.get("importacion") == "1")
    return _alta_doc(DocumentoCxP, "CXP", empresa_id, u, d, ("cxp", "compras_gastos"), cartera.asiento_compra, "Compra a crédito")


def _movimiento_doc(Modelo, Mov, empresa_id, u, doc_id, fecha, monto, ref, claves, plantilla, desc, tipo):
    doc = db.session.execute(select(Modelo).where(Modelo.id == doc_id, Modelo.empresa_id == empresa_id).with_for_update()).scalar_one_or_none()
    if doc is None: raise ErrorAsiento("Documento no encontrado")
    f, m = parsear_fecha(fecha), _d(monto)
    if m <= 0: raise ErrorAsiento("El monto debe ser mayor que cero")
    if m > doc.saldo_pendiente: raise ErrorAsiento(f"El monto excede el saldo pendiente ({doc.saldo_pendiente})")
    doc.saldo_pendiente -= m
    doc.estado = "Cobrado" if tipo == "COBRO" and doc.saldo_pendiente == 0 else "Pagado" if doc.saldo_pendiente == 0 else "Pendiente"
    mov = Mov(documento_id=doc.id, fecha=f, monto=m, referencia=(ref or "")[:200]); db.session.add(mov)
    cfg = _cfg(empresa_id, *claves)
    if cfg:
        mov.asiento_id = crear_asiento(empresa_id, u, f, f"{desc} Doc.N°{doc.id} - {ref}", plantilla(cfg, m), commit=False)
    registrar(u.username, f"{tipo}_REGISTRADO", f"empresa={empresa_id} doc={doc.id} monto={m}")
    db.session.commit()
    return mov


def registrar_cobro(empresa_id, u, doc_id, fecha, monto, ref):
    return _movimiento_doc(DocumentoCxC, CobroCxC, empresa_id, u, doc_id, fecha, monto, ref, ("cxc", "caja_banco"), cartera.asiento_cobro, "Cobro CxC", "COBRO")


def registrar_pago(empresa_id, u, doc_id, fecha, monto, ref):
    return _movimiento_doc(DocumentoCxP, PagoCxP, empresa_id, u, doc_id, fecha, monto, ref, ("cxp", "caja_banco"), cartera.asiento_pago, "Pago CxP", "PAGO")


def aging_y_provision(empresa_id, corte):
    docs = [(x.id, x.deudor_nombre, x.numero_documento, x.fecha_vencimiento, x.saldo_pendiente) for x in
            db.session.execute(select(DocumentoCxC).where(DocumentoCxC.empresa_id == empresa_id, DocumentoCxC.saldo_pendiente > 0)).scalars()]
    org = db.session.get(Empresa, empresa_id).organizacion_id
    tramos = [(t.dias_desde, t.dias_hasta, t.porcentaje) for t in db.session.execute(select(TramoProvision).where(TramoProvision.organizacion_id == org)).scalars()]
    return cartera.provision(docs, tramos, corte)


def aplicar_provision_incobrables(empresa_id, u, fecha, corte=None):
    _, total = aging_y_provision(empresa_id, corte or parsear_fecha(fecha))
    cfg = _cfg(empresa_id, "gasto_incobrables", "provision_incobrables")
    if total <= 0: raise ErrorAsiento("No hay provisión que registrar")
    if not cfg: raise ErrorAsiento("Configure las cuentas de gasto y provisión de incobrables")
    aid = crear_asiento(empresa_id, u, fecha, "Provisión de cuentas incobrables", cartera.asiento_provision_incobrables(cfg, total))
    return aid, total


def libro_compras(empresa_id, periodo):
    """Libro de compras del mes (Art. 75 RLIVA) generado desde las cuentas por pagar + retenciones de IVA practicadas."""
    from .models2 import RetencionIVA
    anio, mes = int(periodo[:4]), int(periodo[5:7])
    ini, fin = date(anio, mes, 1), date(anio + (mes == 12), mes % 12 + 1, 1)
    docs = db.session.execute(select(DocumentoCxP).where(DocumentoCxP.empresa_id == empresa_id, DocumentoCxP.fecha_emision >= ini,
                                                         DocumentoCxP.fecha_emision < fin).order_by(DocumentoCxP.fecha_emision, DocumentoCxP.id)).scalars().all()
    ret = {}
    for r in db.session.execute(select(RetencionIVA).where(RetencionIVA.empresa_id == empresa_id, RetencionIVA.tipo_operacion == "C")).scalars():
        x = ret.setdefault((r.rif_proveedor.upper(), r.numero_documento), [Decimal(0), []])
        x[0] += r.monto_iva; x[1].append(r.numero_comprobante or "")
    filas, tot = [], {k: Decimal(0) for k in ("total", "exento", "base", "iva", "retenido")}
    for n, d in enumerate(docs, 1):
        s = -1 if d.tipo_documento == "NC" else 1
        retenido, comps = ret.get(((d.proveedor_rif or "").upper(), d.numero_documento), [Decimal(0), []])
        v = dict(total=s * d.monto, exento=s * d.exento, base=s * d.base_imponible, iva=s * d.monto_iva, retenido=retenido)
        for k in tot: tot[k] += v[k]
        filas.append(dict(n=n, fecha=d.fecha_emision, rif=d.proveedor_rif or "", nombre=d.proveedor_nombre, tipo_compra="Importación" if d.es_importacion else "Nacional",
                          tipo=d.tipo_documento, numero=d.numero_documento, control=d.numero_control or "", afectado=d.documento_afectado or "",
                          alicuota=d.alicuota, comprobante=", ".join(c for c in comps if c), **v))
    return filas, tot


# ============================ Inventario ============================
def alta_producto(empresa_id, u, codigo, nombre, metodo, precio_venta=0, alicuota="G"):
    if metodo not in ("Promedio", "PEPS"): raise ErrorAsiento("Método de costeo inválido")
    if alicuota not in ("G", "R", "A", "E"): raise ErrorAsiento("Alícuota de IVA inválida")
    if db.session.get(Producto, (empresa_id, codigo)): raise ErrorAsiento("El código de producto ya existe")
    db.session.add(Producto(empresa_id=empresa_id, codigo=codigo.strip(), nombre=nombre.strip(), metodo_costeo=metodo,
                            precio_venta=Decimal(str(a_decimal(precio_venta))), alicuota_iva=alicuota))
    registrar(u.username, "PRODUCTO_CREADO", f"empresa={empresa_id} codigo={codigo}"); db.session.commit()


def _producto(empresa_id, codigo):
    p = db.session.execute(select(Producto).where(Producto.empresa_id == empresa_id, Producto.codigo == codigo).with_for_update()).scalar_one_or_none()
    if p is None: raise ErrorAsiento("Producto no encontrado")
    return p


def entrada_inventario(empresa_id, u, codigo, cantidad, costo, fecha, ref, credito=False, commit=True, contabilizar=True):
    p, f = _producto(empresa_id, codigo), parsear_fecha(fecha)
    try:
        cant = inventario.entrada_promedio(p.cantidad_actual, p.costo_promedio_actual, cantidad, costo)
    except inventario.ErrorInventario as e:
        raise ErrorAsiento(str(e)) from e
    c, cu = Decimal(str(cantidad)), Decimal(str(costo))
    if p.metodo_costeo == "PEPS":
        db.session.add(LotePeps(empresa_id=empresa_id, producto_codigo=codigo, fecha=f, cantidad_restante=c, costo_unitario=cu))
        p.cantidad_actual = p.cantidad_actual + c
    else:
        p.cantidad_actual, p.costo_promedio_actual = cant
    db.session.add(MovInventario(empresa_id=empresa_id, producto_codigo=codigo, tipo="Entrada", cantidad=c, costo_unitario=cu, fecha=f, referencia=ref))
    cfg = _cfg(empresa_id, "inventario", "cxp" if credito else "caja_banco") if contabilizar else None
    aid = crear_asiento(empresa_id, u, f, f"Entrada Inventario {codigo} - {ref}", cartera.asiento_entrada_inv(cfg, c * cu, credito), commit=False) if cfg else None
    registrar(u.username, "INV_ENTRADA", f"empresa={empresa_id} {codigo} x{c}")
    if commit: db.session.commit()
    return aid


def salida_inventario(empresa_id, u, codigo, cantidad, fecha, ref, commit=True, contabilizar=True):
    p, f = _producto(empresa_id, codigo), parsear_fecha(fecha)
    c = Decimal(str(cantidad))
    try:
        if p.metodo_costeo == "PEPS":
            lotes = list(db.session.execute(select(LotePeps).where(LotePeps.empresa_id == empresa_id, LotePeps.producto_codigo == codigo,
                                                                      LotePeps.cantidad_restante > 0).with_for_update()).scalars())
            total, cu, consumos = inventario.salida_peps([Lote(str(l.fecha), l.cantidad_restante, l.costo_unitario, l.id) for l in lotes], c)
            por_id = {l.id: l for l in lotes}
            for lote, usa in consumos: por_id[lote.id].cantidad_restante -= usa
        else:
            total, cu = inventario.salida_promedio(p.cantidad_actual, p.costo_promedio_actual, c)
    except inventario.ErrorInventario as e:
        raise ErrorAsiento(str(e)) from e
    p.cantidad_actual -= c
    db.session.add(MovInventario(empresa_id=empresa_id, producto_codigo=codigo, tipo="Salida", cantidad=c, costo_unitario=cu, fecha=f, referencia=ref))
    cfg = _cfg(empresa_id, "inventario", "costo_venta") if contabilizar else None
    aid = crear_asiento(empresa_id, u, f, f"Costo de Venta {codigo} - {ref}", cartera.asiento_costo_venta(cfg, total), commit=False) if cfg and total > 0 else None
    registrar(u.username, "INV_SALIDA", f"empresa={empresa_id} {codigo} x{c} costo={total}")
    if commit: db.session.commit()
    return total, aid


def valor_inventario(empresa_id):
    tot, det = Decimal(0), []
    for p in db.session.execute(select(Producto).where(Producto.empresa_id == empresa_id).order_by(Producto.codigo)).scalars():
        if p.metodo_costeo == "PEPS":
            v = inventario.valor_peps([Lote("", l.cantidad_restante, l.costo_unitario) for l in db.session.execute(
                select(LotePeps).where(LotePeps.empresa_id == empresa_id, LotePeps.producto_codigo == p.codigo)).scalars()])
        else:
            v = (p.cantidad_actual * p.costo_promedio_actual).quantize(Decimal("0.01"))
        det.append((p.codigo, p.nombre, p.metodo_costeo, p.cantidad_actual, v)); tot += v
    return tot, det


# ============================ Cierre / reportes ============================
def ejecutar_cierre(empresa_id, u, fecha, cuenta_patrimonio):
    f = parsear_fecha(fecha)
    lineas = estados.lineas_cierre(saldos(empresa_id, hasta=f), cuenta_patrimonio)
    if not lineas: raise ErrorAsiento("No hay saldos de Ingresos/Gastos para cerrar")
    return crear_asiento(empresa_id, u, f, f"Cierre de Ejercicio y Refundición a {f}", lineas)


def cerrar_periodo(empresa_id, u, mes_anio):
    if len(mes_anio) != 7 or mes_anio[4] != "-": raise ErrorAsiento("Use el formato AAAA-MM")
    if db.session.execute(select(PeriodoCerrado.id).where(PeriodoCerrado.empresa_id == empresa_id, PeriodoCerrado.mes_anio == mes_anio)).first():
        raise ErrorAsiento("El período ya está cerrado")
    db.session.add(PeriodoCerrado(empresa_id=empresa_id, mes_anio=mes_anio, cerrado_por=u.id))
    registrar(u.username, "PERIODO_CERRADO", f"empresa={empresa_id} {mes_anio}"); db.session.commit()


def flujo_efectivo(empresa_id, desde, hasta):
    q = select(ExtractoBancario.categoria_flujo, func.coalesce(func.sum(ExtractoBancario.monto), 0)).where(
        ExtractoBancario.empresa_id == empresa_id, ExtractoBancario.fecha >= desde, ExtractoBancario.fecha <= hasta).group_by(ExtractoBancario.categoria_flujo)
    return estados.flujo_efectivo(dict(db.session.execute(q).all()))


# ============================ Fiscal ============================
def ut_vigente():
    r = db.session.execute(select(UnidadTributaria).order_by(UnidadTributaria.fecha_vigencia.desc()).limit(1)).scalar_one_or_none()
    if r is None: raise fiscal.ErrorFiscal("No hay Unidad Tributaria configurada (Administración → Fiscal)")
    return r.valor_bs


def _tramos():
    return [(t.desde_ut, t.hasta_ut, t.porcentaje, t.sustraendo_ut) for t in db.session.execute(select(TarifaISLR)).scalars()]


def _inpc(fecha):
    r = db.session.execute(select(IndiceINPC.valor_indice).where(IndiceINPC.fecha <= fecha).order_by(IndiceINPC.fecha.desc()).limit(1)).scalar()
    return r or db.session.execute(select(IndiceINPC.valor_indice).order_by(IndiceINPC.fecha).limit(1)).scalar()


def calcular_islr(empresa_id, ejercicio, inicio, cierre, incluir_inflacion=True):
    ini, fin = parsear_fecha(inicio), parsear_fecha(cierre)
    rows = saldos(empresa_id, hasta=fin)
    utilidad = estados.balance(rows)["utilidad"]
    parts = db.session.execute(select(PartidaConciliacion).where(PartidaConciliacion.empresa_id == empresa_id, PartidaConciliacion.ejercicio_fiscal == ejercicio)).scalars().all()
    sumas, restas = (sum((p.monto for p in parts if p.tipo == t), Decimal(0)) for t in ("Suma", "Resta"))
    reaj, nota = Decimal(0), None
    if incluir_inflacion:
        try:
            anm, pat = estados.bases_inflacion(rows)
            es_spe = bool(db.session.get(Empresa, empresa_id).es_sujeto_pasivo_especial)
            reaj = fiscal.reajuste_inflacion(_inpc(ini), _inpc(fin), anm, pat, es_spe)["reajuste_regular_por_inflacion"]
        except fiscal.ErrorFiscal as e:
            nota = str(e)
    rebajas = [r.monto_bs for r in db.session.execute(select(RebajaISLR).where(RebajaISLR.empresa_id == empresa_id, RebajaISLR.ejercicio_fiscal == ejercicio)).scalars()]
    res = fiscal.islr_completo(utilidad, sumas, restas, reaj, ut_vigente(), _tramos(), rebajas)
    res["nota_inflacion"] = nota
    return res


def retencion_islr_calculada(concepto_id, monto, tipo):
    c = db.session.get(ConceptoRetencionISLR, concepto_id)
    if not c: raise fiscal.ErrorFiscal("Concepto de retención no encontrado")
    return c, fiscal.calcular_retencion_islr(monto, tipo, c.porcentaje_juridica, c.porcentaje_natural, c.sustraendo_ut, c.monto_minimo_ut, ut_vigente())


# ============================ Auditoría ============================
def verificar_integridad(empresa_id):
    h = []
    for aid, fecha, concepto, d, hh in db.session.execute(
            select(Asiento.id, Asiento.fecha, Asiento.concepto, func.sum(Movimiento.debe), func.sum(Movimiento.haber))
            .join(Movimiento, Movimiento.asiento_id == Asiento.id).where(Asiento.empresa_id == empresa_id)
            .group_by(Asiento.id).having(func.sum(Movimiento.debe) != func.sum(Movimiento.haber))):
        h.append(("Crítica", f"Asiento descuadrado N° {aid}", f"{fecha} '{concepto}': Debe={d} Haber={hh}", "Cédula Analítica - Asientos"))
    futuros = db.session.execute(select(func.count()).select_from(Asiento).where(Asiento.empresa_id == empresa_id, Asiento.fecha > hoy_ve())).scalar()
    if futuros:
        h.append(("Media", "Asientos con fecha futura", f"{futuros} asiento(s) posteriores a hoy", "Cédula Sumaria"))
    ok, rota = verificar_cadena()
    if not ok:
        h.append(("Crítica", "Bitácora de auditoría alterada", f"La cadena de hashes se rompe en el registro {rota}", "Bitácora"))
    from .servicios_fact import verificar_cadena_documentos
    ok, rota = verificar_cadena_documentos(empresa_id)
    if not ok:
        h.append(("Crítica", "Documentos de venta alterados", f"La cadena de facturación se rompe en el documento {rota}", "Cédula de ingresos"))
    return h


def crear_hallazgo(empresa_id, u, titulo, descripcion, cedula, cuenta, severidad):
    if severidad not in ("Crítica", "Alta", "Media", "Baja"): raise ErrorAsiento("Severidad inválida")
    db.session.add(Hallazgo(empresa_id=empresa_id, titulo=titulo.strip(), descripcion=descripcion, cedula_ref=cedula,
                            cuenta_codigo=cuenta, severidad=severidad, auditor=u.username))
    registrar(u.username, "HALLAZGO_CREADO", f"empresa={empresa_id} {titulo[:80]}"); db.session.commit()


def cerrar_hallazgo(empresa_id, u, hid, comentario):
    h = db.session.execute(select(Hallazgo).where(Hallazgo.id == hid, Hallazgo.empresa_id == empresa_id)).scalar_one_or_none()
    if h is None or h.estado == "Cerrado": raise ErrorAsiento("Hallazgo no disponible")
    h.estado, h.comentario_cierre, h.cerrado_en = "Cerrado", comentario, datetime.now(timezone.utc)
    registrar(u.username, "HALLAZGO_CERRADO", f"empresa={empresa_id} id={hid}"); db.session.commit()
