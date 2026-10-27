"""Operaciones del módulo de facturación. Igual que el resto del sistema: documento + inventario + asiento + CxC +
bitácora en UNA transacción. La integración con otros módulos depende del PLAN de la licencia:
  - inventario: descarga existencias y guarda el costo de venta;  - contabilidad: asiento automático y libro fiscal;
  - administrativo: las ventas a crédito generan su documento de CxC.
"""
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select

from . import servicios as S
from .auditoria import registrar
from .calculos import facturacion as F
from .calculos.facturacion import ErrorFactura
from .contabilidad import service as contab
from .contabilidad.validadores import parsear_fecha
from .extensions import db
from .tiempo import hoy_ve
from .models import Empresa, PeriodoCerrado
from .models2 import AlicuotaImpuesto, CobroCxC, DocumentoCxC, DocumentoFiscal, Producto
from .models_fact import DocumentoVenta, LineaDocumentoVenta, PagoDocumentoVenta, SerieFacturacion

GENESIS = "0" * 64


def alicuotas_vigentes(fecha) -> dict:
    out = {}
    for cod in ("G", "R", "A", "IGTF"):
        v = db.session.execute(select(AlicuotaImpuesto.porcentaje).where(AlicuotaImpuesto.codigo == cod, AlicuotaImpuesto.fecha_vigencia <= fecha)
                               .order_by(AlicuotaImpuesto.fecha_vigencia.desc()).limit(1)).scalar()
        if v is not None: out[cod] = v
    faltan = {"G", "R", "A"} - set(out)
    if faltan:
        raise ErrorFactura(f"Faltan alícuotas de IVA vigentes ({', '.join(sorted(faltan))}). El administrador de la plataforma debe cargarlas.")
    return out


def tasa_del_dia(fecha, moneda="USD"):
    """Tasa BCV vigente para la moneda; si falta la de hoy se consulta DolarAPI automáticamente (app/tasas.py)."""
    from . import tasas
    return tasas.tasa_vigente(fecha, moneda)


def _periodo_cerrado(eid, f):
    return db.session.execute(select(PeriodoCerrado.id).where(PeriodoCerrado.empresa_id == eid, PeriodoCerrado.mes_anio == f.strftime("%Y-%m"))).first() is not None


def _doc(eid, did, bloquear=True) -> DocumentoVenta:
    q = select(DocumentoVenta).where(DocumentoVenta.id == did, DocumentoVenta.empresa_id == eid)
    d = db.session.execute(q.with_for_update() if bloquear else q).scalar_one_or_none()
    if d is None: raise ErrorFactura("Documento no encontrado")
    return d


def numero_completo(d) -> str:
    return f"{d.serie}{F.numero_fmt(d.numero)}" if d.serie else F.numero_fmt(d.numero)


def emitir_documento(eid, u, datos, modulos) -> DocumentoVenta:
    tipo = datos["tipo"]
    if tipo not in ("FAC", "NC", "ND"): raise ErrorFactura("Tipo de documento inválido")
    f = parsear_fecha(datos["fecha"])
    if f > hoy_ve(): raise ErrorFactura("No se pueden emitir documentos con fecha futura")
    if _periodo_cerrado(eid, f): raise ErrorFactura("El período de esa fecha está cerrado")
    serie = db.session.execute(select(SerieFacturacion).where(SerieFacturacion.id == int(datos["serie_id"]), SerieFacturacion.empresa_id == eid,
                                                              SerieFacturacion.activa.is_(True)).with_for_update()).scalar_one_or_none()
    if serie is None: raise ErrorFactura("Serie de facturación no disponible")
    if serie.control_siguiente > serie.control_hasta:
        raise ErrorFactura("Se agotó el rango de números de control asignado por la imprenta. Registre un nuevo rango.")
    emp = db.session.get(Empresa, eid)
    afectado = None
    if tipo in ("NC", "ND"):
        afectado = _doc(eid, int(datos.get("afectado_id") or 0))
        if afectado.tipo != "FAC" or afectado.estado != "Emitido": raise ErrorFactura("La nota debe referirse a una factura emitida (no anulada)")
        if not (datos.get("motivo") or "").strip(): raise ErrorFactura("Indique el motivo de la nota")
        cliente = dict(rif=afectado.cliente_rif, nombre=afectado.cliente_nombre, direccion=afectado.cliente_direccion, telefono=afectado.cliente_telefono)
        moneda, condicion = afectado.moneda, afectado.condicion
    else:
        cliente = datos["cliente"]
        if not cliente.get("rif") or not cliente.get("nombre"): raise ErrorFactura("Indique RIF/cédula y nombre o razón social del adquirente")
        moneda, condicion = datos.get("moneda", "VES"), datos.get("condicion", "Contado")
    if condicion not in ("Contado", "Credito"): raise ErrorFactura("Condición de pago inválida")
    tasa = datos.get("tasa") or (tasa_del_dia(f, moneda) if moneda != "VES" else 1)
    if moneda != "VES" and not tasa: raise ErrorFactura("Indique la tasa BCV del día para operaciones en divisas")
    alic = alicuotas_vigentes(f)
    calc = F.calcular(datos["lineas"], alic, moneda, tasa)
    if tipo == "NC":
        acumulado = db.session.execute(select(func.coalesce(func.sum(DocumentoVenta.total), 0)).where(
            DocumentoVenta.documento_afectado_id == afectado.id, DocumentoVenta.tipo == "NC", DocumentoVenta.estado == "Emitido")).scalar()
        if acumulado + calc["total"] > afectado.total:
            raise ErrorFactura(f"Las notas de crédito superarían el total de la factura (disponible Bs {afectado.total - acumulado})")
    aplica_igtf = bool(emp.es_sujeto_pasivo_especial) and tipo != "NC"
    pagos, igtf_base, igtf = [], Decimal(0), Decimal(0)
    if tipo != "NC" and condicion == "Contado":
        if aplica_igtf and "IGTF" not in alic: raise ErrorFactura("Falta la alícuota de IGTF vigente (la carga el administrador de la plataforma)")
        pagos, igtf_base, igtf = F.aplicar_pagos(calc["total"], datos.get("pagos") or [], calc["tasa_bcv"], alic.get("IGTF", 0), aplica_igtf)
    elif tipo == "FAC" and condicion == "Credito":
        venc = parsear_fecha(datos.get("vencimiento") or f)
        if venc < f: raise ErrorFactura("El vencimiento no puede ser anterior a la emisión")
    # ---- numeración: consecutiva por serie (fila bloqueada) y número de control del rango de la imprenta
    campo_num = {"FAC": "siguiente_fac", "NC": "siguiente_nc", "ND": "siguiente_nd"}[tipo]
    numero, control = getattr(serie, campo_num), F.control_fmt(serie.control_prefijo, serie.control_siguiente)
    setattr(serie, campo_num, numero + 1); serie.control_siguiente += 1
    db.session.execute(db.text("SELECT pg_advisory_xact_lock(43, :e)"), {"e": eid})
    prev = db.session.execute(select(DocumentoVenta.hash).where(DocumentoVenta.empresa_id == eid).order_by(DocumentoVenta.id.desc()).limit(1)).scalar() or GENESIS
    total_pagar = calc["total"] + igtf
    d = DocumentoVenta(
        empresa_id=eid, serie_id=serie.id, tipo=tipo, serie=serie.serie, numero=numero, numero_control=control, fecha_emision=f,
        cliente_rif=cliente["rif"], cliente_nombre=cliente["nombre"].strip(), cliente_direccion=cliente.get("direccion"), cliente_telefono=cliente.get("telefono"),
        moneda=moneda, tasa_bcv=calc["tasa_bcv"], condicion=condicion, fecha_vencimiento=parsear_fecha(datos["vencimiento"]) if tipo == "FAC" and condicion == "Credito" else None,
        exento=calc["exento"], base_g=calc["base_g"], iva_g=calc["iva_g"], base_r=calc["base_r"], iva_r=calc["iva_r"], base_a=calc["base_a"], iva_a=calc["iva_a"],
        pct_g=calc["pct_g"], pct_r=calc["pct_r"], pct_a=calc["pct_a"], descuento=calc["descuento"], total=calc["total"], total_divisa=calc["total_divisa"],
        igtf_pct=alic.get("IGTF", 0) if aplica_igtf else 0, igtf_base=igtf_base, igtf=igtf, total_a_pagar=total_pagar,
        documento_afectado_id=afectado.id if afectado else None, motivo=(datos.get("motivo") or "").strip()[:500] or None,
        observaciones=(datos.get("observaciones") or "").strip()[:500] or None, creado_por=u.id, hash_prev=prev,
        saldo_pendiente=total_pagar if (tipo != "NC" and condicion == "Credito") else 0)
    d.hash = F.hash_documento(prev, dict(tipo=tipo, serie=d.serie, numero=numero, numero_control=control, fecha_emision=f, cliente_rif=d.cliente_rif, total=d.total, igtf=igtf))
    # El documento se inserta UNA vez y ya completo (el trigger impide modificarlo después): se reserva su id,
    # se hacen inventario, asiento y CxC, y al final se insertan documento, líneas y pagos.
    d.id = db.session.execute(db.text("SELECT nextval(pg_get_serial_sequence('documentos_venta', 'id'))")).scalar()
    ref = f"{tipo} {numero_completo(d)}"
    usa_inv, usa_cont = "inventario" in modulos, "contabilidad" in modulos
    lineas = []
    for l in calc["lineas"]:
        cod = (l.get("producto") or "").strip() or None if usa_inv else None
        linea = LineaDocumentoVenta(documento_id=d.id, empresa_id=eid, producto_codigo=cod, descripcion=l["descripcion"].strip()[:300], cantidad=l["cantidad"],
                                    precio_unitario=l["precio"], descuento_pct=l["descuento_pct"], alicuota=l["alicuota"], base_bs=l["base_bs"], iva_bs=l["iva_bs"])
        if cod:
            if db.session.get(Producto, (eid, cod)) is None: raise ErrorFactura(f"Producto {cod} no existe en el inventario")
            if tipo == "NC":
                if datos.get("reintegrar"):
                    costo = db.session.execute(select(LineaDocumentoVenta.costo_unitario).where(LineaDocumentoVenta.documento_id == afectado.id,
                                                                                               LineaDocumentoVenta.producto_codigo == cod).limit(1)).scalar()
                    costo = costo if costo is not None else db.session.get(Producto, (eid, cod)).costo_promedio_actual
                    S.entrada_inventario(eid, u, cod, l["cantidad"], costo, f, f"Devolución {ref}", commit=False, contabilizar=False)
                    linea.costo_unitario = costo
                    cfg_inv = S._cfg(eid, "inventario", "costo_venta") if usa_cont else None
                    valor = F.q2(Decimal(costo) * l["cantidad"])
                    if cfg_inv and valor > 0:  # devolución: el costo de venta se revierte contra el inventario
                        contab.crear_asiento(eid, u, f, f"Costo de Venta {cod} - {ref}", [(cfg_inv["cuenta_inventario"], valor, 0), (cfg_inv["cuenta_costo_venta"], 0, valor)], commit=False)
            else:
                total_costo, _ = S.salida_inventario(eid, u, cod, l["cantidad"], f, ref, commit=False, contabilizar=usa_cont)
                linea.costo_unitario = (total_costo / l["cantidad"]).quantize(Decimal("0.0001"))
        lineas.append(linea)
    # ---- contabilidad (asiento automático + libro fiscal)
    if usa_cont:
        iva = d.iva_g + d.iva_r + d.iva_a
        base = d.exento + d.base_g + d.base_r + d.base_a
        cfg = S._cfg(eid, *F.claves_asiento(condicion, iva, igtf))
        if cfg:
            d.asiento_id = contab.crear_asiento(eid, u, f, f"{ref} — {d.cliente_nombre}", F.asiento_documento(cfg, tipo, condicion, base, iva, igtf), commit=False)
        s = F.signo(tipo)
        db.session.add(DocumentoFiscal(empresa_id=eid, tipo_documento="Venta", rif_tercero=d.cliente_rif, razon_social_tercero=d.cliente_nombre,
                                       numero_factura=f"{tipo}-{numero_completo(d)}", numero_control=control, fecha=f, base_imponible=s * base,
                                       porcentaje_iva=d.pct_g, monto_iva=s * iva, total_operacion=s * d.total))
    # ---- cartera
    if tipo != "NC" and condicion == "Credito" and "administrativo" in modulos:
        cxc = DocumentoCxC(empresa_id=eid, numero_documento=ref, fecha_emision=f, fecha_vencimiento=d.fecha_vencimiento or f, monto=total_pagar,
                           saldo_pendiente=total_pagar, deudor_nombre=d.cliente_nombre, deudor_rif=d.cliente_rif, asiento_id=d.asiento_id)
        db.session.add(cxc); db.session.flush(); d.cxc_id = cxc.id
    db.session.add(d); db.session.flush()
    db.session.add_all(lineas)
    for p in pagos:
        db.session.add(PagoDocumentoVenta(documento_id=d.id, empresa_id=eid, fecha=f, **p))
    if tipo == "NC" and condicion == "Credito" and afectado.saldo_pendiente > 0:
        rebaja = min(afectado.saldo_pendiente, d.total)
        afectado.saldo_pendiente -= rebaja
        if afectado.cxc_id:
            c = db.session.execute(select(DocumentoCxC).where(DocumentoCxC.id == afectado.cxc_id).with_for_update()).scalar_one()
            c.saldo_pendiente -= min(c.saldo_pendiente, rebaja)
            c.estado = "Cobrado" if c.saldo_pendiente == 0 else c.estado
    registrar(u.username, f"{tipo}_EMITIDA", f"empresa={eid} {ref} control={control} total={d.total} igtf={igtf}")
    db.session.commit()
    return d


def registrar_cobro_doc(eid, u, did, fecha, forma, moneda, monto, tasa, referencia, modulos):
    """Cobro de una factura a crédito (también actualiza la CxC enlazada). IGTF si el pago es en divisas y la empresa es SPE."""
    d = _doc(eid, did)
    if d.estado != "Emitido" or d.tipo == "NC" or d.saldo_pendiente <= 0: raise ErrorFactura("El documento no tiene saldo por cobrar")
    f = parsear_fecha(fecha)
    if forma not in F.FORMAS_PAGO or moneda not in F.MONEDAS: raise ErrorFactura("Forma de pago o moneda inválida")
    m = F.q2(F._dec(monto, "Monto"))
    if m <= 0: raise ErrorFactura("El monto debe ser mayor que cero")
    t = F._dec(tasa or tasa_del_dia(f, moneda) or 0, "Tasa") if moneda != "VES" else Decimal(1)
    if t <= 0: raise ErrorFactura("Indique la tasa BCV del día")
    mbs = F.q2(m * t)
    emp = db.session.get(Empresa, eid)
    igtf = F.igtf_de_pago(mbs, moneda, alicuotas_vigentes(f).get("IGTF", 0), emp.es_sujeto_pasivo_especial)
    if mbs > d.saldo_pendiente: raise ErrorFactura(f"El cobro excede el saldo pendiente (Bs {d.saldo_pendiente})")
    d.saldo_pendiente -= mbs
    db.session.add(PagoDocumentoVenta(documento_id=d.id, empresa_id=eid, fecha=f, forma=forma, moneda=moneda, monto=m, monto_bs=mbs, igtf_bs=igtf, referencia=(referencia or "")[:120]))
    aid = None
    if "contabilidad" in modulos:
        cfg = S._cfg(eid, "caja_banco", "cxc", *(["igtf_por_pagar"] if igtf else []))
        if cfg:
            lineas = [(cfg["cuenta_caja_banco"], mbs + igtf, 0), (cfg["cuenta_cxc"], 0, mbs)] + ([(cfg["cuenta_igtf_por_pagar"], 0, igtf)] if igtf else [])
            aid = contab.crear_asiento(eid, u, f, f"Cobro {d.tipo} {numero_completo(d)} — {referencia or forma}", lineas, commit=False)
    if d.cxc_id:
        c = db.session.execute(select(DocumentoCxC).where(DocumentoCxC.id == d.cxc_id).with_for_update()).scalar_one()
        c.saldo_pendiente -= min(c.saldo_pendiente, mbs)
        c.estado = "Cobrado" if c.saldo_pendiente == 0 else "Pendiente"
        db.session.add(CobroCxC(documento_id=c.id, fecha=f, monto=mbs, referencia=(referencia or forma)[:200], asiento_id=aid))
    registrar(u.username, "COBRO_FACTURA", f"empresa={eid} doc={d.id} bs={mbs} igtf={igtf}")
    db.session.commit()
    return mbs, igtf


def registrar_retencion_iva(eid, u, did, comprobante, monto, modulos):
    """Retención de IVA practicada por un cliente contribuyente especial (Prov. SNAT/2015/0049)."""
    d = _doc(eid, did)
    if d.estado != "Emitido" or d.tipo == "NC": raise ErrorFactura("Documento no válido para retención")
    m = F.q2(F._dec(monto, "Monto retenido"))
    iva = d.iva_g + d.iva_r + d.iva_a
    if m <= 0 or d.iva_retenido + m > iva: raise ErrorFactura(f"La retención no puede superar el IVA del documento (Bs {iva})")
    if not (comprobante or "").strip(): raise ErrorFactura("Indique el N° de comprobante de retención")
    d.iva_retenido += m; d.comprobante_retencion = comprobante.strip()[:40]
    nota = ""
    if d.condicion == "Credito" and d.saldo_pendiente > 0:
        rebaja = min(d.saldo_pendiente, m); d.saldo_pendiente -= rebaja
        if d.cxc_id:
            c = db.session.execute(select(DocumentoCxC).where(DocumentoCxC.id == d.cxc_id).with_for_update()).scalar_one()
            c.saldo_pendiente -= min(c.saldo_pendiente, rebaja); c.estado = "Cobrado" if c.saldo_pendiente == 0 else c.estado
        if "contabilidad" in modulos:
            cfg = S._cfg(eid, "iva_retenido_por_clientes", "cxc")
            if cfg:
                contab.crear_asiento(eid, u, hoy_ve(), f"Retención IVA comprobante {comprobante} — {d.tipo} {numero_completo(d)}",
                                     [(cfg["cuenta_iva_retenido_por_clientes"], rebaja, 0), (cfg["cuenta_cxc"], 0, rebaja)], commit=False)
            else: nota = " (sin asiento: configure la cuenta de IVA retenido por clientes)"
    registrar(u.username, "RETENCION_IVA_RECIBIDA", f"empresa={eid} doc={d.id} comprobante={comprobante} monto={m}")
    db.session.commit()
    return nota


def anular_documento(eid, u, did, motivo, modulos):
    """Solo el mismo día de emisión, sin cobros posteriores ni notas asociadas; si no, corresponde una nota de crédito.
    El documento NO se borra: queda "Anulado" con su número y control (el correlativo no se reutiliza)."""
    d = _doc(eid, did)
    if d.estado != "Emitido": raise ErrorFactura("El documento ya está anulado")
    if d.fecha_emision != hoy_ve(): raise ErrorFactura("Solo puede anularse el mismo día de su emisión; después emita una nota de crédito")
    if not (motivo or "").strip(): raise ErrorFactura("Indique el motivo de la anulación")
    if d.condicion == "Credito" and d.tipo != "NC" and d.saldo_pendiente != d.total_a_pagar: raise ErrorFactura("Tiene cobros registrados: emita una nota de crédito")
    if d.iva_retenido: raise ErrorFactura("Tiene retención de IVA registrada: emita una nota de crédito")
    if db.session.execute(select(DocumentoVenta.id).where(DocumentoVenta.documento_afectado_id == d.id, DocumentoVenta.estado == "Emitido")).first():
        raise ErrorFactura("Tiene notas de crédito/débito vigentes: anúlelas primero")
    hoy = hoy_ve()
    ref = f"Anulación {d.tipo} {numero_completo(d)}"
    if d.asiento_id and "contabilidad" in modulos:
        contab.anular_asiento(eid, u, d.asiento_id, hoy, motivo.strip()[:200])
    if "inventario" in modulos:
        for l in db.session.execute(select(LineaDocumentoVenta).where(LineaDocumentoVenta.documento_id == d.id, LineaDocumentoVenta.producto_codigo.is_not(None),
                                                                      LineaDocumentoVenta.costo_unitario.is_not(None))).scalars():
            if d.tipo == "NC": S.salida_inventario(eid, u, l.producto_codigo, l.cantidad, hoy, ref, commit=False, contabilizar=False)
            else: S.entrada_inventario(eid, u, l.producto_codigo, l.cantidad, l.costo_unitario, hoy, ref, commit=False, contabilizar=False)
    # El costo de venta (o su reverso en una devolución) tiene su propio asiento: se revierte también
    if "contabilidad" in modulos:
        from .models import Asiento
        for a in db.session.execute(select(Asiento.id).where(Asiento.empresa_id == eid, Asiento.concepto.like(f"Costo de Venta % - {d.tipo} {numero_completo(d)}"))).scalars():
            contab.anular_asiento(eid, u, a, hoy, f"{ref}: costo de venta")
    if d.cxc_id:
        c = db.session.execute(select(DocumentoCxC).where(DocumentoCxC.id == d.cxc_id).with_for_update()).scalar_one()
        c.saldo_pendiente, c.estado = 0, "Anulado"
    if d.tipo == "NC" and d.condicion == "Credito":
        a = _doc(eid, d.documento_afectado_id)
        a.saldo_pendiente = min(a.total_a_pagar, a.saldo_pendiente + d.total)
    if "contabilidad" in modulos:
        s = -F.signo(d.tipo)
        iva, base = d.iva_g + d.iva_r + d.iva_a, d.exento + d.base_g + d.base_r + d.base_a
        db.session.add(DocumentoFiscal(empresa_id=eid, tipo_documento="Venta", rif_tercero=d.cliente_rif, razon_social_tercero=f"ANULADA — {d.cliente_nombre}",
                                       numero_factura=f"{d.tipo}-{numero_completo(d)}", numero_control=d.numero_control, fecha=hoy, base_imponible=s * base,
                                       porcentaje_iva=d.pct_g, monto_iva=s * iva, total_operacion=s * d.total))
    d.estado, d.saldo_pendiente, d.anulado_en, d.anulado_por, d.motivo_anulacion = "Anulado", 0, datetime.now(timezone.utc), u.id, motivo.strip()[:500]
    registrar(u.username, f"{d.tipo}_ANULADA", f"empresa={eid} doc={d.id} control={d.numero_control} motivo={motivo[:80]}")
    db.session.commit()


def verificar_cadena_documentos(eid):
    prev = GENESIS
    for d in db.session.execute(select(DocumentoVenta).where(DocumentoVenta.empresa_id == eid).order_by(DocumentoVenta.id)).scalars():
        esperado = F.hash_documento(prev, dict(tipo=d.tipo, serie=d.serie, numero=d.numero, numero_control=d.numero_control, fecha_emision=d.fecha_emision,
                                               cliente_rif=d.cliente_rif, total=d.total, igtf=d.igtf))
        if d.hash_prev != prev or d.hash != esperado:
            return False, d.id
        prev = d.hash
    return True, None


def libro_ventas(eid, periodo):
    """Libro de ventas del mes (Art. 72 RLIVA). Los documentos anulados se listan con montos en cero."""
    anio, mes = int(periodo[:4]), int(periodo[5:7])
    ini = date(anio, mes, 1)
    fin = date(anio + (mes == 12), mes % 12 + 1, 1)
    docs = db.session.execute(select(DocumentoVenta).where(DocumentoVenta.empresa_id == eid, DocumentoVenta.fecha_emision >= ini, DocumentoVenta.fecha_emision < fin)
                              .order_by(DocumentoVenta.fecha_emision, DocumentoVenta.id)).scalars().all()
    afect = {d.id: d for d in db.session.execute(select(DocumentoVenta).where(DocumentoVenta.id.in_({x.documento_afectado_id for x in docs if x.documento_afectado_id}))).scalars()} if docs else {}
    igtf_cobros = dict(db.session.execute(select(PagoDocumentoVenta.documento_id, func.sum(PagoDocumentoVenta.igtf_bs)).where(
        PagoDocumentoVenta.documento_id.in_([d.id for d in docs])).group_by(PagoDocumentoVenta.documento_id)).all()) if docs else {}
    filas, tot = [], {k: Decimal(0) for k in ("total", "exento", "base_g", "iva_g", "base_r", "iva_r", "base_a", "iva_a", "iva_retenido", "igtf")}
    for n, d in enumerate(docs, 1):
        s = 0 if d.estado == "Anulado" else F.signo(d.tipo)
        v = {k: s * getattr(d, k) for k in tot if k != "igtf"}
        v["igtf"] = (igtf_cobros.get(d.id) or Decimal(0)) if s else Decimal(0)
        for k in tot: tot[k] += v[k]
        a = afect.get(d.documento_afectado_id)
        filas.append(dict(n=n, fecha=d.fecha_emision, rif=d.cliente_rif, nombre=d.cliente_nombre + (" (ANULADA)" if d.estado == "Anulado" else ""),
                          tipo=d.tipo, factura=numero_completo(d) if d.tipo == "FAC" else "", nc=numero_completo(d) if d.tipo == "NC" else "",
                          nd=numero_completo(d) if d.tipo == "ND" else "", control=d.numero_control, afectada=numero_completo(a) if a else "",
                          comprobante=d.comprobante_retencion or "", **v))
    return filas, tot
