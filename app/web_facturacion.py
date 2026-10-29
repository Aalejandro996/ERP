"""Facturación (Venezuela): emisión de facturas, notas de crédito/débito, cobros, retenciones recibidas, series y rangos
de control, catálogo de clientes y libro de ventas."""
import csv
import io
import json
from datetime import date

from flask import Response, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import select

from . import servicios_fact as SF
from .auditoria import registrar
from .calculos import facturacion as calc
from .contabilidad.validadores import parsear_fecha
from .extensions import db
from .tiempo import hoy_ve
from .models import Empresa
from .models2 import Producto
from .models_fact import NOMBRE_DOC, ClienteFact, DocumentoVenta, LineaDocumentoVenta, PagoDocumentoVenta, SerieFacturacion
from .security import tiene_permiso
from .web import F, campo, hoy, intentar, pagina, ruta, validar_rif

h = lambda v: campo("accion", "", "hidden", valor=v)
N_LINEAS, N_PAGOS = 10, 3
ALIC_TXT = {"G": "General", "R": "Reducida", "A": "Lujo", "E": "Exento (E)"}


def _modulos(): return g.modulos


@ruta("/e/<int:eid>/facturacion", "facturacion.ver")
def facturas(eid):
    periodo = request.args.get("periodo") or hoy()[:7]
    try: anio, mes = int(periodo[:4]), int(periodo[5:7]); ini = date(anio, mes, 1); fin = date(anio + (mes == 12), mes % 12 + 1, 1)
    except (ValueError, IndexError): flash("Período inválido"); return redirect(url_for("web.facturas", eid=eid))
    docs = db.session.execute(select(DocumentoVenta).where(DocumentoVenta.empresa_id == eid, DocumentoVenta.fecha_emision >= ini, DocumentoVenta.fecha_emision < fin)
                              .order_by(DocumentoVenta.id.desc())).scalars().all()
    emitidos = [d for d in docs if d.estado == "Emitido"]
    ventas = sum((calc.signo(d.tipo) * d.total for d in emitidos), 0)
    por_cobrar = sum((d.saldo_pendiente for d in emitidos), 0)
    puede_emitir = tiene_permiso(g.usuario, "facturacion.emitir")
    acciones = [dict(url=url_for("web.factura_nueva", eid=eid), texto="Nueva factura", primaria=True)] if puede_emitir else []
    acciones += [dict(url=url_for("web.libro_ventas", eid=eid, periodo=periodo), texto="Libro de ventas")]
    if tiene_permiso(g.usuario, "facturacion.configurar"):
        acciones += [dict(url=url_for("web.series", eid=eid), texto="Series y control"), dict(url=url_for("web.clientes_fact", eid=eid), texto="Clientes")]
    filas = []
    for d in docs:
        filas.append(dict(cells=[NOMBRE_DOC[d.tipo], SF.numero_completo(d), d.numero_control, d.fecha_emision, d.cliente_nombre, d.condicion,
                                 d.total, d.saldo_pendiente, d.estado], url=url_for("web.factura_ver", eid=eid, did=d.id), clase="anulado" if d.estado == "Anulado" else ""))
    return pagina("Facturación", eid, [
        dict(kpis=[("Ventas netas del mes (Bs)", f"{ventas:,.2f}"), ("Documentos emitidos", len(emitidos)), ("Por cobrar (Bs)", f"{por_cobrar:,.2f}"),
                   ("Anulados", len(docs) - len(emitidos))]),
        dict(titulo="Período", form=dict(action=request.path, metodo="get", boton="Ver", campos=[campo("periodo", "Mes", "month", valor=periodo)])),
        dict(titulo="Documentos", tabla=dict(cols=["Tipo", "Número", "Control", "Fecha", "Cliente", "Condición", "Total Bs", "Saldo Bs", "Estado"], num=[6, 7], filas=filas))],
        acciones=acciones)


def _leer_lineas():
    lineas = []
    for i in range(1, N_LINEAS + 1):
        desc = (F.get(f"l{i}_desc") or "").strip()
        if not desc and not F.get(f"l{i}_precio"): continue
        lineas.append(dict(producto=F.get(f"l{i}_prod") or "", descripcion=desc, cantidad=F.get(f"l{i}_cant") or "1", precio=F.get(f"l{i}_precio") or "0",
                           descuento_pct=F.get(f"l{i}_dpct") or "0", alicuota=F.get(f"l{i}_alic") or "G"))
    return lineas


def _leer_pagos():
    return [dict(forma=F.get(f"p{i}_forma"), moneda=F.get(f"p{i}_moneda"), monto=F.get(f"p{i}_monto"), referencia=F.get(f"p{i}_ref"))
            for i in range(1, N_PAGOS + 1) if (F.get(f"p{i}_monto") or "").strip()]


@ruta("/e/<int:eid>/facturacion/nueva", "facturacion.emitir", ("GET", "POST"))
def factura_nueva(eid):
    tipo = request.values.get("tipo", "FAC")
    if tipo not in ("FAC", "NC", "ND"): abort(400)
    afectado = None
    if tipo != "FAC":
        afectado = db.session.execute(select(DocumentoVenta).where(DocumentoVenta.id == int(request.values.get("afectado") or 0), DocumentoVenta.empresa_id == eid)).scalar_one_or_none()
        if afectado is None: abort(404)
    if request.method == "POST":
        try: rif = validar_rif(F.get("cliente_rif"), obligatorio=tipo == "FAC") if tipo == "FAC" else ""
        except ValueError as e: flash(str(e)); rif = None
        if rif is not None:
            datos = dict(tipo=tipo, serie_id=F.get("serie_id") or 0, fecha=F.get("fecha") or hoy(), moneda=F.get("moneda", "VES"), tasa=F.get("tasa") or None,
                         condicion=F.get("condicion", "Contado"), vencimiento=F.get("vencimiento") or None, lineas=_leer_lineas(), pagos=_leer_pagos(),
                         cliente=dict(rif=rif, nombre=F.get("cliente_nombre", ""), direccion=F.get("cliente_direccion"), telefono=F.get("cliente_telefono")),
                         afectado_id=afectado.id if afectado else None, motivo=F.get("motivo"), observaciones=F.get("observaciones"), reintegrar=F.get("reintegrar") == "1")
            if F.get("guardar_cliente") == "1" and tipo == "FAC" and rif:
                if not db.session.execute(select(ClienteFact.id).where(ClienteFact.empresa_id == eid, ClienteFact.rif == rif)).first():
                    db.session.add(ClienteFact(empresa_id=eid, rif=rif, razon_social=datos["cliente"]["nombre"].strip(), direccion=datos["cliente"]["direccion"], telefono=datos["cliente"]["telefono"]))
            res = {}
            if intentar(lambda: res.setdefault("d", SF.emitir_documento(eid, g.usuario, datos, _modulos())), lambda d: f"{NOMBRE_DOC[tipo]} N° {SF.numero_completo(d)} emitida — control {d.numero_control}"):
                return redirect(url_for("web.factura_ver", eid=eid, did=res["d"].id))
    series = db.session.execute(select(SerieFacturacion).where(SerieFacturacion.empresa_id == eid, SerieFacturacion.activa.is_(True)).order_by(SerieFacturacion.serie)).scalars().all()
    if not series:
        flash("Configure primero una serie y el rango de números de control asignado por la imprenta.")
        return redirect(url_for("web.series", eid=eid) if tiene_permiso(g.usuario, "facturacion.configurar") else url_for("web.facturas", eid=eid))
    clientes = db.session.execute(select(ClienteFact).where(ClienteFact.empresa_id == eid).order_by(ClienteFact.razon_social)).scalars().all()
    productos = db.session.execute(select(Producto).where(Producto.empresa_id == eid).order_by(Producto.nombre)).scalars().all() if "inventario" in g.modulos else []
    lineas_af = db.session.execute(select(LineaDocumentoVenta).where(LineaDocumentoVenta.documento_id == afectado.id)).scalars().all() if afectado else []
    try: alic = SF.alicuotas_vigentes(hoy_ve())
    except calc.ErrorFactura as e: flash(str(e)); alic = {}
    emp = db.session.get(Empresa, eid)
    datos_js = json.dumps({"clientes": {c.rif: [c.razon_social, c.direccion or "", c.telefono or ""] for c in clientes},
                           "productos": {p.codigo: [p.nombre, str(p.precio_venta), p.alicuota_iva, str(p.cantidad_actual)] for p in productos},
                           "alicuotas": {k: str(v) for k, v in alic.items()}, "igtf": bool(emp.es_sujeto_pasivo_especial),
                           "tasas": {m: str(SF.tasa_del_dia(hoy_ve(), m) or "") for m in emp.divisas()}})
    datos_js = datos_js.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")  # un nombre con "</script>" no rompe el bloque
    return render_template("factura_nueva.html", titulo=f"Nueva {NOMBRE_DOC[tipo].lower()}", empresa=emp, tipo=tipo, afectado=afectado, lineas_af=lineas_af,
                           series=series, clientes=clientes, productos=productos, datos_js=datos_js, n_lineas=N_LINEAS, n_pagos=N_PAGOS, hoy=hoy(),
                           tasa=SF.tasa_del_dia(hoy_ve()), formas=calc.FORMAS_PAGO, monedas=["VES", *emp.divisas()], alic_txt=ALIC_TXT, form=request.form)


@ruta("/e/<int:eid>/facturacion/<int:did>", "facturacion.ver")
def factura_ver(eid, did):
    d = db.session.execute(select(DocumentoVenta).where(DocumentoVenta.id == did, DocumentoVenta.empresa_id == eid)).scalar_one_or_none()
    if d is None: abort(404)
    emp = db.session.get(Empresa, eid)
    serie = db.session.get(SerieFacturacion, d.serie_id)
    lineas = db.session.execute(select(LineaDocumentoVenta).where(LineaDocumentoVenta.documento_id == d.id).order_by(LineaDocumentoVenta.id)).scalars().all()
    pagos = db.session.execute(select(PagoDocumentoVenta).where(PagoDocumentoVenta.documento_id == d.id).order_by(PagoDocumentoVenta.id)).scalars().all()
    afectado = db.session.get(DocumentoVenta, d.documento_afectado_id) if d.documento_afectado_id else None
    notas = db.session.execute(select(DocumentoVenta).where(DocumentoVenta.documento_afectado_id == d.id).order_by(DocumentoVenta.id)).scalars().all()
    return render_template("factura.html", titulo=f"{NOMBRE_DOC[d.tipo]} {SF.numero_completo(d)}", empresa=emp, d=d, serie=serie, lineas=lineas, pagos=pagos,
                           afectado=afectado, notas=notas, nombre_doc=NOMBRE_DOC, num=SF.numero_completo, formas=calc.FORMAS_PAGO, monedas=["VES", *emp.divisas()],
                           hoy=hoy(), puede_emitir=tiene_permiso(g.usuario, "facturacion.emitir"), puede_anular=tiene_permiso(g.usuario, "facturacion.anular"),
                           es_hoy=d.fecha_emision == hoy_ve())


@ruta("/e/<int:eid>/facturacion/<int:did>/cobro", "facturacion.emitir", ("POST",))
def factura_cobro(eid, did):
    intentar(lambda: SF.registrar_cobro_doc(eid, g.usuario, did, F["fecha"], F["forma"], F["moneda"], F["monto"], F.get("tasa"), F.get("ref"), _modulos()),
             lambda r: f"Cobro registrado: Bs {r[0]}" + (f" + IGTF Bs {r[1]}" if r[1] else ""))
    return redirect(url_for("web.factura_ver", eid=eid, did=did))


@ruta("/e/<int:eid>/facturacion/<int:did>/retencion", "facturacion.emitir", ("POST",))
def factura_retencion(eid, did):
    intentar(lambda: SF.registrar_retencion_iva(eid, g.usuario, did, F["comprobante"], F["monto"], _modulos()), lambda n: "Retención de IVA registrada" + n)
    return redirect(url_for("web.factura_ver", eid=eid, did=did))


@ruta("/e/<int:eid>/facturacion/<int:did>/anular", "facturacion.anular", ("POST",))
def factura_anular(eid, did):
    intentar(lambda: SF.anular_documento(eid, g.usuario, did, F.get("motivo", ""), _modulos()), "Documento anulado (conserva su número y control)")
    return redirect(url_for("web.factura_ver", eid=eid, did=did))


@ruta("/e/<int:eid>/facturacion/series", "facturacion.configurar", ("GET", "POST"))
def series(eid):
    if request.method == "POST":
        a = F["accion"]
        def guardar():
            if a == "monedas": desde = hasta = 1
            else: desde, hasta = int(F["desde"]), int(F["hasta"])
            if desde < 1 or hasta < desde: raise ValueError("Rango de control inválido")
            if a == "serie":
                serie = (F.get("serie") or "").strip().upper()[:3]
                if not serie.isalnum() and serie: raise ValueError("La serie debe ser alfanumérica")
                imp_rif = validar_rif(F["imprenta_rif"])
                db.session.add(SerieFacturacion(empresa_id=eid, serie=serie, control_prefijo=(F.get("prefijo") or "00").strip()[:4], control_siguiente=desde,
                                                control_hasta=hasta, imprenta_razon_social=F["imprenta"].strip(), imprenta_rif=imp_rif,
                                                imprenta_providencia=F["providencia"].strip(), fecha_asignacion=parsear_fecha(F["fecha"])))
                registrar(g.usuario.username, "SERIE_CREADA", f"empresa={eid} serie={serie} control={desde}-{hasta}")
            elif a == "monedas":
                elegidas = [m for m in ("USD", "EUR", "USDT") if m in F.getlist("monedas")]
                emp = db.session.get(Empresa, eid); emp.monedas = ",".join(elegidas)
                registrar(g.usuario.username, "MONEDAS_EMPRESA", f"empresa={eid} divisas={emp.monedas or 'ninguna'}")
                db.session.commit(); return
            elif a == "rango":
                s = db.session.execute(select(SerieFacturacion).where(SerieFacturacion.id == int(F["sid"]), SerieFacturacion.empresa_id == eid).with_for_update()).scalar_one_or_none()
                if s is None: raise ValueError("Serie no encontrada")
                if desde < s.control_siguiente: raise ValueError("El nuevo rango no puede reutilizar números de control ya emitidos")
                s.control_siguiente, s.control_hasta, s.fecha_asignacion = desde, hasta, parsear_fecha(F["fecha"])
                s.imprenta_providencia = F.get("providencia") or s.imprenta_providencia
                registrar(g.usuario.username, "RANGO_CONTROL", f"empresa={eid} serie={s.serie} control={desde}-{hasta}")
            else: raise ValueError("Acción inválida")
            db.session.commit()
        intentar(guardar, "Guardado"); return redirect(request.url)
    ss = db.session.execute(select(SerieFacturacion).where(SerieFacturacion.empresa_id == eid).order_by(SerieFacturacion.serie)).scalars().all()
    return pagina("Series y números de control", eid, [
        dict(nota="Prov. SNAT/2011/00071: numeración consecutiva y única por serie, y número de control asignado por una imprenta autorizada. "
                  "Si factura con imprenta digital (Prov. SNAT/2024/000102), registre aquí el rango que ella le asigne.",
             tabla=dict(cols=["Serie", "Próx. factura", "Próx. NC", "Próx. ND", "Próx. control", "Control hasta", "Disponibles", "Imprenta", "Providencia"], num=[1, 2, 3, 6], filas=[
                 dict(cells=[s.serie or "(sin serie)", s.siguiente_fac, s.siguiente_nc, s.siguiente_nd, calc.control_fmt(s.control_prefijo, s.control_siguiente),
                             calc.control_fmt(s.control_prefijo, s.control_hasta), max(s.control_hasta - s.control_siguiente + 1, 0), f"{s.imprenta_razon_social} ({s.imprenta_rif})", s.imprenta_providencia]) for s in ss])),
        dict(titulo="Monedas con las que factura la empresa", nota="El bolívar siempre está disponible. Las divisas marcadas aparecen al facturar y al cobrar; "
                                                                  "USDT se convierte con la tasa BCV del dólar y, como pago en divisas, genera IGTF si la empresa es contribuyente especial.",
             form=dict(action=request.path, boton="Guardar monedas", campos=[campo("accion", "", "hidden", valor="monedas"),
                       campo("monedas", "Divisas", "checks", req=False, opciones=[("USD", "Dólares (USD)"), ("EUR", "Euros (EUR)"), ("USDT", "USDT")],
                             valor=db.session.get(Empresa, eid).divisas())])),
        dict(titulo="Nueva serie", form=dict(action=request.path, boton="Crear serie", campos=[
            h("serie"), campo("serie", "Serie (vacío = sin serie)", req=False), campo("prefijo", "Prefijo de control", valor="00"), campo("desde", "Control desde", "number"), campo("hasta", "Control hasta", "number"),
            campo("imprenta", "Imprenta autorizada (razón social)"), campo("imprenta_rif", "RIF de la imprenta"), campo("providencia", "Providencia de la imprenta (N° y fecha)"),
            campo("fecha", "Fecha de asignación del rango", "date", valor=hoy())])),
        dict(titulo="Nuevo rango de control para una serie", form=dict(action=request.path, boton="Registrar rango", campos=[
            h("rango"), campo("sid", "Serie", "select", opciones=[(s.id, s.serie or "(sin serie)") for s in ss]), campo("desde", "Control desde", "number"), campo("hasta", "Control hasta", "number"),
            campo("providencia", "Providencia (si cambió)", req=False), campo("fecha", "Fecha de asignación", "date", valor=hoy())]))])


@ruta("/e/<int:eid>/facturacion/clientes", "facturacion.ver", ("GET", "POST"))
def clientes_fact(eid):
    if request.method == "POST":
        if not tiene_permiso(g.usuario, "facturacion.emitir"): abort(403)
        def alta():
            rif = validar_rif(F["rif"])
            if db.session.execute(select(ClienteFact.id).where(ClienteFact.empresa_id == eid, ClienteFact.rif == rif)).first(): raise ValueError("El cliente ya existe")
            db.session.add(ClienteFact(empresa_id=eid, rif=rif, razon_social=F["razon"].strip(), direccion=F.get("direccion"), telefono=F.get("telefono"),
                                       email=F.get("email"), es_contribuyente_especial=F.get("especial") == "1")); db.session.commit()
        intentar(alta, "Cliente registrado"); return redirect(request.url)
    cs = db.session.execute(select(ClienteFact).where(ClienteFact.empresa_id == eid).order_by(ClienteFact.razon_social)).scalars()
    return pagina("Clientes", eid, [
        dict(tabla=dict(cols=["RIF", "Razón social", "Dirección", "Teléfono", "Correo", "Contrib. especial"], filas=[dict(cells=[c.rif, c.razon_social, c.direccion or "", c.telefono or "", c.email or "", "Sí" if c.es_contribuyente_especial else "No"]) for c in cs])),
        dict(titulo="Nuevo cliente", form=dict(action=request.path, boton="Registrar", campos=[campo("rif", "RIF / Cédula (V-12345678-9)"), campo("razon", "Nombre o razón social"),
             campo("direccion", "Domicilio fiscal", req=False), campo("telefono", "Teléfono", req=False), campo("email", "Correo", "email", req=False),
             campo("especial", "¿Contribuyente especial (agente de retención)?", "select", opciones=[("0", "No"), ("1", "Sí")])]))],
        acciones=[dict(url=url_for("web.facturas", eid=eid), texto="Volver a facturación")])


@ruta("/e/<int:eid>/facturacion/libro-ventas", "facturacion.ver")
def libro_ventas(eid):
    periodo = request.args.get("periodo") or hoy()[:7]
    if len(periodo) != 7 or periodo[4] != "-": flash("Período inválido"); periodo = hoy()[:7]
    filas, tot = SF.libro_ventas(eid, periodo)
    cols = ["N°", "Fecha", "RIF", "Nombre o razón social", "N° factura", "N° control", "N° NC", "N° ND", "Fact. afectada", "Total con IVA", "Exento",
            "Base G", "IVA G", "Base R", "IVA R", "Base A", "IVA A", "IVA retenido", "Comprobante", "IGTF"]
    claves = ["n", "fecha", "rif", "nombre", "factura", "control", "nc", "nd", "afectada", "total", "exento", "base_g", "iva_g", "base_r", "iva_r", "base_a", "iva_a", "iva_retenido", "comprobante", "igtf"]
    if request.args.get("formato") == "csv":
        buf = io.StringIO(); w = csv.writer(buf, delimiter=";")
        w.writerow(cols)
        for f in filas: w.writerow([str(f[k]) for k in claves])
        w.writerow(["", "", "", "TOTALES", "", "", "", "", "", *[str(tot[k]) for k in ("total", "exento", "base_g", "iva_g", "base_r", "iva_r", "base_a", "iva_a", "iva_retenido")], "", str(tot["igtf"])])
        registrar(g.usuario.username, "LIBRO_VENTAS_EXPORTADO", f"empresa={eid} periodo={periodo}"); db.session.commit()
        return Response("﻿" + buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f'attachment; filename="libro_ventas_{periodo}.csv"'})
    num = list(range(9, 18)) + [19]
    total_fila = dict(cells=["", "", "", "TOTALES", "", "", "", "", "", *[tot[k] for k in ("total", "exento", "base_g", "iva_g", "base_r", "iva_r", "base_a", "iva_a", "iva_retenido")], "", tot["igtf"]], clase="total")
    return pagina("Libro de ventas", eid, [
        dict(titulo="Período", form=dict(action=request.path, metodo="get", boton="Ver", campos=[campo("periodo", "Mes", "month", valor=periodo)])),
        dict(titulo=f"Libro de ventas {periodo}", nota="Art. 72 del Reglamento de la Ley de IVA. Base G/R/A = alícuota general, reducida y general + adicional (lujo). Los documentos anulados figuran con montos en cero.",
             tabla=dict(cols=cols, num=num, filas=[dict(cells=[f[k] for k in claves]) for f in filas] + [total_fila], ancha=True)),
        dict(pares=[("Débito fiscal del período (Bs)", tot["iva_g"] + tot["iva_r"] + tot["iva_a"]), ("IVA retenido por clientes (Bs)", tot["iva_retenido"]), ("IGTF percibido (Bs)", tot["igtf"])])],
        acciones=[dict(url=url_for("web.libro_ventas", eid=eid, periodo=periodo, formato="csv"), texto="Exportar CSV", primaria=True), dict(url=url_for("web.facturas", eid=eid, periodo=periodo), texto="Volver")])
