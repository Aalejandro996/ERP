"""Módulo fiscal Venezuela: ISLR, conciliación, retenciones IVA/ISLR (TXT SENIAT) y libros de compra/venta."""
import re
from decimal import Decimal

from flask import Response, flash, g, redirect, request, url_for
from sqlalchemy import select

from . import servicios as S
from .auditoria import registrar
from .calculos import fiscal as calc
from .calculos.fiscal import ErrorFiscal
from .contabilidad.validadores import ErrorAsiento, a_decimal, parsear_fecha
from .extensions import db
from .models import Empresa
from .models2 import (ConceptoRetencionISLR, DocumentoFiscal, PartidaConciliacion, RebajaISLR, RetencionIVA, RetencionISLR)
from .security import tiene_permiso
from .web import F, campo, hoy, intentar, pagina, ruta

ADVERTENCIA_INFL = ("Ajuste por inflación SIMPLIFICADO (no pondera por fecha de cada movimiento). Un contador debe revisarlo antes de declarar. "
                    "Los Sujetos Pasivos Especiales no pueden aplicarlo desde 2015.")
h = lambda v: campo("accion", "", "hidden", valor=v)


@ruta("/e/<int:eid>/fiscal/islr", "fiscal.ver", ("GET", "POST"))
def fiscal_islr(eid):
    res = None
    if request.method == "POST":
        if not tiene_permiso(g.usuario, "fiscal.operar"): return ("", 403)
        a = F["accion"]
        def spe():
            db.session.get(Empresa, eid).es_sujeto_pasivo_especial = F["spe"] == "1"
            registrar(g.usuario.username, "SPE_MODIFICADO", f"empresa={eid} spe={F['spe']}"); db.session.commit()
        if a == "calcular":
            try:
                res = S.calcular_islr(eid, F["ejercicio"], F["inicio"], F["cierre"], F.get("inflacion") == "1")
                registrar(g.usuario.username, "ISLR_CALCULADO", f"empresa={eid} ejercicio={F['ejercicio']}"); db.session.commit()
            except (ErrorFiscal, ErrorAsiento, ValueError) as e:
                flash(str(e))
        elif a == "partida":
            def alta():
                db.session.add(PartidaConciliacion(empresa_id=eid, ejercicio_fiscal=F["ejercicio"], concepto=F["concepto"], categoria=F.get("categoria"), tipo=F["tipo"], monto=a_decimal(F["monto"]))); db.session.commit()
            intentar(alta, "Partida registrada")
        elif a == "rebaja":
            def alta():
                db.session.add(RebajaISLR(empresa_id=eid, ejercicio_fiscal=F["ejercicio"], concepto=F["concepto"], monto_bs=a_decimal(F["monto"]))); db.session.commit()
            intentar(alta, "Rebaja registrada")
        elif a == "spe": intentar(spe, "Condición actualizada")
        if res is None: return redirect(request.url)
    secs = []
    if res:
        pares = [(k.replace("_", " ").capitalize(), v) for k, v in res.items() if k not in ("calculo_tarifa", "nota_inflacion", "nota") and not isinstance(v, dict)]
        secs.append(dict(titulo="Resultado de la determinación", pares=pares, nota=" · ".join(x for x in (res.get("nota"), res.get("nota_inflacion")) if x)))
        if res.get("calculo_tarifa"): secs.append(dict(titulo="Detalle de la tarifa N° 2 (Art. 52 LISLR)", pares=[(k.replace("_", " ").capitalize(), v) for k, v in res["calculo_tarifa"].items()]))
    emp = db.session.get(Empresa, eid)
    secs.append(dict(titulo="Determinar ISLR de persona jurídica", nota=ADVERTENCIA_INFL, form=dict(action=request.path, boton="Calcular", campos=[
        h("calcular"), campo("ejercicio", "Ejercicio (AAAA)", valor=hoy()[:4]), campo("inicio", "Inicio", "date", valor=f"{hoy()[:4]}-01-01"), campo("cierre", "Cierre", "date", valor=f"{hoy()[:4]}-12-31"),
        campo("inflacion", "¿Incluir ajuste por inflación?", "select", opciones=[("1", "Sí"), ("0", "No")])])))
    if tiene_permiso(g.usuario, "fiscal.operar"):
        secs += [dict(titulo="Partida de conciliación fiscal", form=dict(action=request.path, boton="Agregar", campos=[h("partida"), campo("ejercicio", "Ejercicio"), campo("concepto", "Concepto"), campo("categoria", "Categoría", req=False), campo("tipo", "Tipo", "select", opciones=[("Suma", "Suma (gasto no deducible…)"), ("Resta", "Resta (ingreso no gravable…)")]), campo("monto", "Monto", "number")])),
                 dict(titulo="Rebaja de impuesto", form=dict(action=request.path, boton="Agregar", campos=[h("rebaja"), campo("ejercicio", "Ejercicio"), campo("concepto", "Concepto"), campo("monto", "Monto", "number")])),
                 dict(titulo=f"Sujeto Pasivo Especial: {'SÍ' if emp.es_sujeto_pasivo_especial else 'NO'}", form=dict(action=request.path, boton="Actualizar", campos=[h("spe"), campo("spe", "Condición", "select", opciones=[("0", "No"), ("1", "Sí")])]))]
    parts = db.session.execute(select(PartidaConciliacion).where(PartidaConciliacion.empresa_id == eid).order_by(PartidaConciliacion.ejercicio_fiscal.desc())).scalars()
    secs.append(dict(titulo="Partidas de conciliación", tabla=dict(cols=["Ejercicio", "Concepto", "Tipo", "Monto"], num=[3], filas=[dict(cells=[p.ejercicio_fiscal, p.concepto, p.tipo, p.monto]) for p in parts])))
    return pagina("Fiscal — ISLR", eid, secs)


@ruta("/e/<int:eid>/fiscal/retenciones", "fiscal.ver", ("GET", "POST"))
def fiscal_retenciones(eid):
    if request.method == "POST":
        if not tiene_permiso(g.usuario, "fiscal.operar"): return ("", 403)
        if F["accion"] == "iva":
            def alta():
                base, iva, exento = a_decimal(F["base_imponible"]), a_decimal(F["monto_iva"]), a_decimal(F.get("monto_exento") or 0)
                if not re.fullmatch(r"\d{6}", F["periodo"]): raise ErrorFiscal("El período debe ser AAAAMM")
                db.session.add(RetencionIVA(empresa_id=eid, rif_contribuyente=F["rif_contribuyente"].upper(), periodo_impositivo=F["periodo"], fecha_documento=parsear_fecha(F["fecha"]),
                                            tipo_operacion=F["tipo_operacion"], tipo_documento=F["tipo_documento"], rif_proveedor=F["rif_proveedor"].upper(), numero_documento=F["numero_documento"],
                                            numero_control=F["numero_control"], monto_documento=base + iva + exento, base_imponible=base, monto_iva=iva,
                                            numero_comprobante=F.get("numero_comprobante"), monto_exento=exento, alicuota=a_decimal(F["alicuota"])))
                registrar(g.usuario.username, "RETENCION_IVA", f"empresa={eid} periodo={F['periodo']}"); db.session.commit()
            intentar(alta, "Retención de IVA registrada")
        else:
            def alta():
                c, r = S.retencion_islr_calculada(int(F["concepto_id"]), F["monto"], F["tipo"])
                db.session.add(RetencionISLR(empresa_id=eid, fecha=parsear_fecha(F["fecha"]), concepto=c.concepto, codigo_seniat=c.codigo_seniat, beneficiario_nombre=F["nombre"],
                                             beneficiario_rif=F["rif"].upper(), tipo_beneficiario=F["tipo"], monto_pagado=a_decimal(F["monto"]),
                                             porcentaje_aplicado=r.get("porcentaje_aplicado", 0), sustraendo_bs=r.get("sustraendo_bs", 0), monto_retenido=r["monto_retenido"]))
                registrar(g.usuario.username, "RETENCION_ISLR", f"empresa={eid} retenido={r['monto_retenido']}"); db.session.commit()
                return r
            intentar(alta, lambda r: f"Retención ISLR calculada: {r['monto_retenido']}" + (f" ({r['motivo']})" if r.get("motivo") else ""))
        return redirect(request.url)
    ivas = db.session.execute(select(RetencionIVA).where(RetencionIVA.empresa_id == eid).order_by(RetencionIVA.id.desc()).limit(200)).scalars()
    islrs = db.session.execute(select(RetencionISLR).where(RetencionISLR.empresa_id == eid).order_by(RetencionISLR.id.desc()).limit(200)).scalars()
    conceptos = [(c.id, f"{c.concepto} ({c.codigo_seniat})") for c in db.session.execute(select(ConceptoRetencionISLR).order_by(ConceptoRetencionISLR.id)).scalars()]
    return pagina("Fiscal — Retenciones", eid, [
        dict(titulo="Descargar TXT de retenciones de IVA (SENIAT, 16 columnas)", nota="Verifique la estructura contra el instructivo vigente del SENIAT antes de declarar.",
             form=dict(action=url_for("web.txt_iva", eid=eid), metodo="get", boton="Descargar", campos=[campo("periodo", "Período AAAAMM")])),
        dict(titulo="Retenciones de IVA", tabla=dict(cols=["Período", "Fecha", "Proveedor", "Documento", "Base", "IVA", "Alícuota"], num=[4, 5, 6], filas=[dict(cells=[r.periodo_impositivo, r.fecha_documento, r.rif_proveedor, r.numero_documento, r.base_imponible, r.monto_iva, r.alicuota]) for r in ivas])),
        dict(titulo="Nueva retención de IVA", form=dict(action=request.path, boton="Registrar", campos=[h("iva"), campo("rif_contribuyente", "RIF contribuyente"), campo("periodo", "Período AAAAMM"), campo("fecha", "Fecha", "date", valor=hoy()),
             campo("tipo_operacion", "Tipo operación", "select", opciones=[("C", "Compra"), ("V", "Venta")]), campo("tipo_documento", "Tipo documento", valor="01"), campo("rif_proveedor", "RIF proveedor"), campo("numero_documento", "N° documento"),
             campo("numero_control", "N° control"), campo("base_imponible", "Base imponible", "number"), campo("monto_iva", "Monto IVA", "number"), campo("monto_exento", "Exento", "number", req=False), campo("alicuota", "Alícuota %", "number"), campo("numero_comprobante", "N° comprobante", req=False)])),
        dict(titulo="Retenciones de ISLR", tabla=dict(cols=["Fecha", "Concepto", "Beneficiario", "RIF", "Pagado", "%", "Retenido"], num=[4, 5, 6], filas=[dict(cells=[r.fecha, r.concepto, r.beneficiario_nombre, r.beneficiario_rif, r.monto_pagado, r.porcentaje_aplicado, r.monto_retenido]) for r in islrs])),
        dict(titulo="Nueva retención de ISLR", form=dict(action=request.path, boton="Calcular y registrar", campos=[h("islr"), campo("fecha", "Fecha", "date", valor=hoy()), campo("concepto_id", "Concepto", "select", opciones=conceptos),
             campo("tipo", "Beneficiario", "select", opciones=[("Juridica", "Persona jurídica"), ("Natural", "Persona natural")]), campo("nombre", "Nombre"), campo("rif", "RIF"), campo("monto", "Monto pagado", "number")]))])


@ruta("/e/<int:eid>/fiscal/retenciones-iva.txt", "fiscal.ver")
def txt_iva(eid):
    periodo = request.args.get("periodo", "")
    if not re.fullmatch(r"\d{6}", periodo): return ("Período inválido (AAAAMM)", 400)
    filas = db.session.execute(select(RetencionIVA).where(RetencionIVA.empresa_id == eid, RetencionIVA.periodo_impositivo == periodo).order_by(RetencionIVA.id)).scalars().all()
    if not filas: return ("No hay retenciones de IVA para ese período", 404)
    cols = ["rif_contribuyente", "periodo_impositivo", "fecha_documento", "tipo_operacion", "tipo_documento", "rif_proveedor", "numero_documento", "numero_control", "monto_documento", "base_imponible", "monto_iva", "numero_documento_afectado", "numero_comprobante", "monto_exento", "alicuota", "numero_expediente"]
    try: cuerpo = "\n".join(calc.linea_txt_retencion_iva({c: getattr(r, c) for c in cols}) for r in filas) + "\n"
    except ErrorFiscal as e: return (str(e), 422)
    registrar(g.usuario.username, "TXT_IVA_DESCARGADO", f"empresa={eid} periodo={periodo}"); db.session.commit()
    return Response(cuerpo, mimetype="text/plain", headers={"Content-Disposition": f'attachment; filename="retenciones_iva_{periodo}.txt"'})


@ruta("/e/<int:eid>/fiscal/libros", "fiscal.ver", ("GET", "POST"))
def fiscal_libros(eid):
    if request.method == "POST":
        if not tiene_permiso(g.usuario, "fiscal.operar"): return ("", 403)
        def alta():
            base, pct = a_decimal(F["base"]), a_decimal(F["pct_iva"])
            iva = (base * pct / 100).quantize(Decimal("0.01"))
            db.session.add(DocumentoFiscal(empresa_id=eid, tipo_documento=F["tipo"], rif_tercero=F["rif"].upper(), razon_social_tercero=F["razon"], numero_factura=F["factura"], numero_control=F.get("control"),
                                           fecha=parsear_fecha(F["fecha"]), base_imponible=base, porcentaje_iva=pct, monto_iva=iva, total_operacion=base + iva,
                                           retencion_iva=a_decimal(F.get("ret_iva") or 0), retencion_islr=a_decimal(F.get("ret_islr") or 0)))
            db.session.commit()
        intentar(alta, "Documento fiscal registrado"); return redirect(request.url)
    sec = []
    for tipo in ("Compra", "Venta"):
        docs = db.session.execute(select(DocumentoFiscal).where(DocumentoFiscal.empresa_id == eid, DocumentoFiscal.tipo_documento == tipo).order_by(DocumentoFiscal.fecha.desc())).scalars().all()
        tot = sum((d.total_operacion for d in docs), Decimal(0)); iva = sum((d.monto_iva for d in docs), Decimal(0))
        sec.append(dict(titulo=f"Libro de {tipo.lower()}s — total {tot} · IVA {iva}", tabla=dict(cols=["Fecha", "RIF", "Razón social", "Factura", "Base", "% IVA", "IVA", "Total"], num=[4, 5, 6, 7], filas=[dict(cells=[d.fecha, d.rif_tercero, d.razon_social_tercero, d.numero_factura, d.base_imponible, d.porcentaje_iva, d.monto_iva, d.total_operacion]) for d in docs])))
    sec.append(dict(titulo="Registrar documento", form=dict(action=request.path, boton="Registrar", campos=[campo("tipo", "Libro", "select", opciones=[("Compra", "Compras"), ("Venta", "Ventas")]), campo("fecha", "Fecha", "date", valor=hoy()), campo("rif", "RIF"), campo("razon", "Razón social"),
         campo("factura", "N° factura"), campo("control", "N° control", req=False), campo("base", "Base imponible", "number"), campo("pct_iva", "% IVA", "number"), campo("ret_iva", "Ret. IVA", "number", req=False), campo("ret_islr", "Ret. ISLR", "number", req=False)])))
    return pagina("Libros de compras y ventas", eid, sec)
