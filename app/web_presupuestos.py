"""Facturación → Presupuestos: cotizaciones de la empresa a sus clientes, con el mismo modelo de documento (logo, RIF,
dirección fiscal). Se emiten a mano o desde el bot de WhatsApp («6» o «presupuesto»): en modo automático el bot envía el
presupuesto al instante; en modo manual queda en borrador para que el personal lo revise y lo envíe. Un presupuesto
aceptado se factura con un clic (misma factura de siempre: inventario, CxC, asiento y libro de ventas)."""
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from flask import Response, abort, current_app, flash, g, get_flashed_messages, redirect, request, url_for
from markupsafe import escape
from sqlalchemy import select

from . import plantilla_correo as pc
from . import servicios_compras as SC
from . import whatsapp as WA
from .auditoria import registrar
from .contabilidad.validadores import a_decimal
from .extensions import db, limiter
from .models import Empresa
from .models2 import Producto
from .models_compras import PresupuestoVenta
from .models_fact import ClienteFact, SerieFacturacion
from .security import tiene_permiso
from .tiempo import hoy_ve
from .web import F, bp, campo, intentar, pagina, ruta

C2 = Decimal("0.01")
N_LINEAS = 8
h = lambda v: campo("accion", "", "hidden", valor=v)


# ---------------------------------------------------------------- servicio
def crear(eid, autor, cliente: dict, lineas: list[dict], origen="manual", estado="Borrador", contacto_id=None, validez=15, nota=None) -> PresupuestoVenta:
    from .servicios_fact import alicuotas_vigentes
    if not (cliente.get("nombre") or "").strip(): raise ValueError("Indique el cliente")
    hoy = hoy_ve()
    try: al = alicuotas_vigentes(hoy)
    except ValueError: al = {"G": Decimal(16), "R": Decimal(8), "A": Decimal(15)}
    pct = lambda a: Decimal(0) if a == "E" else (Decimal(str(al["G"])) + Decimal(str(al["A"])) if a == "A" else Decimal(str(al[a])))
    exento = base = iva = Decimal(0); limpias = []
    for l in lineas:
        cod = (l.get("codigo") or "").strip() or None
        prod = db.session.get(Producto, (eid, cod)) if cod else None
        if cod and prod is None: raise ValueError(f"El producto {cod} no existe")
        desc = (l.get("descripcion") or "").strip() or (prod.nombre if prod else "")
        if not desc: continue
        cant = a_decimal(l.get("cantidad") or 1)
        precio = a_decimal(l["precio"]) if str(l.get("precio") or "").strip() else (prod.precio_venta if prod else Decimal(0))
        a = l.get("alicuota") or (prod.alicuota_iva if prod else "G")
        if cant <= 0 or precio < 0 or a not in ("G", "R", "A", "E"): raise ValueError(f"Línea inválida: {desc}")
        monto = (cant * precio).quantize(C2, ROUND_HALF_UP)
        if a == "E": exento += monto
        else: base += monto; iva += (monto * pct(a) / 100).quantize(C2, ROUND_HALF_UP)
        limpias.append(dict(codigo=cod, descripcion=desc[:300], cantidad=format(cant.normalize(), "f"), precio=str(precio), alicuota=a, monto=str(monto)))
    if not limpias: raise ValueError("Agregue al menos un producto o servicio")
    p = PresupuestoVenta(empresa_id=eid, numero=SC._siguiente(eid, PresupuestoVenta), fecha=hoy, valido_hasta=hoy + timedelta(days=max(1, min(int(validez or 15), 90))),
                         cliente_rif=(cliente.get("rif") or "").strip().upper() or None, cliente_nombre=cliente["nombre"].strip()[:200],
                         cliente_direccion=cliente.get("direccion"), cliente_telefono=cliente.get("telefono"), cliente_email=cliente.get("email"),
                         contacto_id=contacto_id, lineas=limpias, exento=exento, base=base, iva=iva, total=exento + base + iva,
                         nota=(nota or "").strip()[:1000] or None, estado=estado, origen=origen, creado_por=autor)
    db.session.add(p); db.session.flush()
    registrar(autor, "PRESUPUESTO_VENTA", f"empresa={eid} N°{p.numero} total={p.total} origen={origen}")
    return p


def como_dict(p: PresupuestoVenta) -> dict:
    """Mismo modelo de documento que la orden de compra (emisor con logo y RIF, cliente, líneas, totales)."""
    e = db.session.get(Empresa, p.empresa_id)
    d = {"numero": p.numero, "numero_txt": f"PR-{p.numero:06d}", "fecha": str(p.fecha), "fecha_entrega": None, "estado": p.estado, "tipo_destinatario": "cliente",
         "moneda": "VES", "tasa_bcv": "1", "condicion_pago": f"Válido hasta {p.valido_hasta:%d/%m/%Y}", "lugar_entrega": e.direccion, "observaciones": p.nota,
         "creado_por": p.creado_por, "exento": str(p.exento), "base": str(p.base), "iva": str(p.iva), "total": str(p.total), "total_bs": str(p.total),
         "emisor": SC.emisor_dict(e), "destinatario": {"nombre": p.cliente_nombre, "rif": p.cliente_rif or "—", "direccion": p.cliente_direccion,
                                                      "telefono": p.cliente_telefono, "email": p.cliente_email},
         "lineas": [dict(l, unidad=None) for l in p.lineas]}
    return d


def html(p, intro=None) -> str:
    cfg = SC.config_wa(p.empresa_id)
    return pc.orden_compra_html(como_dict(p), current_app.config["APP_BASE_URL"], intro, titulo="PRESUPUESTO", etiqueta="Cotización",
                                pie="Para aceptar responda ACEPTO por WhatsApp o escríbanos. " + ((cfg.instrucciones_pago or "") if cfg else ""),
                                aviso=f"Presupuesto válido hasta el {p.valido_hasta:%d/%m/%Y}; precios sujetos a disponibilidad de inventario. No es una factura.")


def enlace(p) -> str:
    from itsdangerous import URLSafeTimedSerializer
    org = db.session.get(Empresa, p.empresa_id).organizacion_id
    return current_app.config["APP_BASE_URL"].rstrip("/") + "/pv/" + URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="presupuesto").dumps([org, p.empresa_id, p.id])


def resumen_whatsapp(p) -> str:
    det = "\n".join(f"• {l['descripcion']} × {l['cantidad']} = Bs {pc._bs(l['monto'])}" for l in p.lineas[:15])
    return (f"📑 *Presupuesto N° {p.numero}*\n{det}\n*Total: Bs {pc._bs(p.total)}* (IVA incluido)\nVálido hasta {p.valido_hasta:%d/%m/%Y}.\n"
            f"Ver presupuesto: {enlace(p)}\nPara aceptarlo responda *ACEPTO {p.numero}*.")


def enviar_whatsapp(eid, p) -> None:
    tel = p.cliente_telefono
    if p.contacto_id:
        from .models_compras import WhatsappContacto
        c = db.session.get(WhatsappContacto, p.contacto_id); tel = c.telefono if c else tel
    if not tel: raise ValueError("El presupuesto no tiene WhatsApp del cliente")
    SC.enviar_wa(eid, tel, resumen_whatsapp(p))
    if p.estado == "Borrador": p.estado = "Enviado"


def facturar(eid, u, p, serie_id, dias, modulos):
    """Presupuesto aceptado → factura a crédito con la misma lógica de siempre (inventario, CxC, asiento, libro de ventas)."""
    from .servicios_fact import emitir_documento
    if p.estado in ("Facturado", "Anulado", "Rechazado"): raise ValueError(f"El presupuesto está {p.estado.lower()}")
    if not p.cliente_rif: raise ValueError("Para facturar indique el RIF del cliente")
    hoy = hoy_ve()
    d = emitir_documento(eid, u, dict(tipo="FAC", serie_id=serie_id, fecha=hoy.isoformat(), moneda="VES", condicion="Credito",
                                      vencimiento=(hoy + timedelta(days=int(dias or 0))).isoformat(), pagos=[],
                                      lineas=[dict(producto=(l.get("codigo") or "") if "inventario" in modulos else "", descripcion=l["descripcion"], cantidad=l["cantidad"],
                                                   precio=l["precio"], descuento_pct="0", alicuota=l["alicuota"]) for l in p.lineas],
                                      cliente=dict(rif=p.cliente_rif, nombre=p.cliente_nombre, direccion=p.cliente_direccion, telefono=p.cliente_telefono),
                                      observaciones=f"Según presupuesto N° {p.numero}"), modulos)
    p = db.session.get(PresupuestoVenta, p.id)
    p.estado, p.documento_venta_id = "Facturado", d.id
    db.session.commit()
    return d


# ---------------------------------------------------------------- pantallas
@ruta("/e/<int:eid>/presupuestos", "presupuestos.ver", ("GET", "POST"))
def presupuestos(eid):
    if request.method == "POST":
        if not tiene_permiso(g.usuario, "presupuestos.emitir"): return ("", 403)
        res = {}
        def alta():
            sel = F.get("cliente") or ""
            c = db.session.get(ClienteFact, int(sel)) if sel.isdigit() else None
            if c is not None and c.empresa_id != eid: raise ValueError("Cliente inválido")
            cliente = dict(rif=c.rif, nombre=c.razon_social, direccion=c.direccion, telefono=F.get("telefono") or c.telefono, email=c.email) if c else \
                dict(rif=F.get("rif"), nombre=F.get("nombre"), direccion=F.get("direccion"), telefono=F.get("telefono"), email=F.get("email"))
            lineas = [dict(codigo=F.get(f"l{i}_cod"), descripcion=F.get(f"l{i}_desc"), cantidad=F.get(f"l{i}_cant") or "1", precio=F.get(f"l{i}_precio"))
                      for i in range(1, N_LINEAS + 1) if F.get(f"l{i}_cod") or (F.get(f"l{i}_desc") or "").strip()]
            res["p"] = crear(eid, g.usuario.username, cliente, lineas, validez=F.get("validez") or 15, nota=F.get("nota")); db.session.commit()
            return res["p"]
        if intentar(alta, lambda p: f"Presupuesto N° {p.numero} creado"): return redirect(url_for("web.presupuesto_ver", eid=eid, pid=res["p"].id))
        return redirect(request.url)
    ps = db.session.execute(select(PresupuestoVenta).where(PresupuestoVenta.empresa_id == eid).order_by(PresupuestoVenta.id.desc()).limit(500)).scalars().all()
    secs = [dict(kpis=[("Por revisar (bot, manual)", sum(p.estado == "Borrador" for p in ps)), ("Enviados", sum(p.estado == "Enviado" for p in ps)),
                       ("Aceptados", sum(p.estado == "Aceptado" for p in ps)), ("Facturados", sum(p.estado == "Facturado" for p in ps))]),
            dict(titulo="Presupuestos", tabla=dict(cols=["N°", "Fecha", "Cliente", "Total Bs", "Válido hasta", "Origen", "Estado"], num=[3], filas=[
                dict(cells=[p.numero, p.fecha, p.cliente_nombre, p.total, p.valido_hasta, "WhatsApp" if p.origen == "whatsapp" else "Manual", p.estado],
                     url=url_for("web.presupuesto_ver", eid=eid, pid=p.id), clase="anulado" if p.estado in ("Anulado", "Rechazado") else "") for p in ps]))]
    return pagina("Presupuestos", eid, secs, acciones=[dict(url=url_for("web.presupuesto_nuevo", eid=eid), texto="+ Nuevo presupuesto", primaria=True)]
                  if tiene_permiso(g.usuario, "presupuestos.emitir") else None)


@ruta("/e/<int:eid>/presupuestos/nuevo", "presupuestos.emitir")
def presupuesto_nuevo(eid):
    """Mismo diseño por pasos del presupuesto de Gigadevs: cliente → productos (precio e IVA del inventario) → condiciones."""
    import json
    from flask import render_template
    from .menu import pestanas
    from .servicios_fact import alicuotas_vigentes
    try: al = alicuotas_vigentes(hoy_ve())
    except ValueError: al = {"G": 16, "R": 8, "A": 15}
    prods = db.session.execute(select(Producto).where(Producto.empresa_id == eid).order_by(Producto.codigo)).scalars().all()
    clis = db.session.execute(select(ClienteFact).where(ClienteFact.empresa_id == eid).order_by(ClienteFact.razon_social)).scalars().all()
    datos = {"productos": {p.codigo: [p.nombre, str(p.precio_venta), p.alicuota_iva] for p in prods}, "alicuotas": {k: str(v) for k, v in al.items()},
             "clientes": {str(c.id): [c.razon_social, c.rif, c.direccion or "", c.telefono or "", c.email or ""] for c in clis}}
    datos_js = json.dumps(datos).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return render_template("presupuesto_cliente.html", titulo="Nuevo presupuesto", empresa=db.session.get(Empresa, eid), productos=prods, clientes=clis,
                           n_lineas=N_LINEAS, datos_js=datos_js, pestanas=pestanas(eid))


@ruta("/e/<int:eid>/presupuestos/<int:pid>", "presupuestos.ver", ("GET", "POST"))
def presupuesto_ver(eid, pid):
    p = db.session.execute(select(PresupuestoVenta).where(PresupuestoVenta.id == pid, PresupuestoVenta.empresa_id == eid)).scalar_one_or_none()
    if p is None: abort(404)
    if request.method == "POST":
        if not tiene_permiso(g.usuario, "presupuestos.emitir"): return ("", 403)
        a = F.get("accion")
        def hacer():
            if a == "whatsapp":
                try: enviar_whatsapp(eid, p)
                except WA.ErrorWhatsapp as e:
                    db.session.commit(); raise ValueError(str(e)) from e
                db.session.commit(); return "Presupuesto enviado por WhatsApp"
            if a == "correo":
                from . import correo
                destino = (F.get("correo") or p.cliente_email or "").strip()
                if "@" not in destino: raise ValueError("Indique el correo del cliente")
                emp = db.session.get(Empresa, eid)
                intro = f"Estimado(a) {p.cliente_nombre}: {emp.razon_social} le envía el presupuesto N° {p.numero}."
                correo.enviar(destino, f"Presupuesto N° {p.numero} · {emp.razon_social}", intro + f"\n\nVer en línea: {enlace(p)}", html=html(p, intro))
                if p.estado == "Borrador": p.estado = "Enviado"
                db.session.commit(); return f"Presupuesto enviado a {destino}"
            if a in ("Aceptado", "Rechazado", "Anulado"):
                if p.estado in ("Facturado", "Anulado"): raise ValueError(f"El presupuesto está {p.estado.lower()}")
                p.estado = a; registrar(g.usuario.username, "PRESUPUESTO_" + a.upper(), f"empresa={eid} N°{p.numero}"); db.session.commit(); return f"Presupuesto {a.lower()}"
            if a == "facturar":
                if not tiene_permiso(g.usuario, "facturacion.emitir"): raise ValueError("Su rol no puede emitir facturas")
                d = facturar(eid, g.usuario, p, int(F["serie_id"]), F.get("dias") or 0, g.modulos)
                from .servicios_fact import numero_completo
                return f"Factura N° {numero_completo(d)} emitida desde el presupuesto"
            raise ValueError("Acción inválida")
        try:
            intentar(hacer, lambda m: m)
        except OSError as e:
            db.session.rollback(); flash(f"No se pudo enviar el correo: {e}")
        return redirect(request.url)
    from flask_wtf.csrf import generate_csrf
    st = "padding:10px 14px;border-radius:10px;font-weight:700;cursor:pointer"
    form = lambda accion, texto, estilo, extra="": (f'<form method="post" action="{url_for("web.presupuesto_ver", eid=eid, pid=pid)}"><input type="hidden" name="csrf_token" value="{generate_csrf()}">'
                                                   f'<input type="hidden" name="accion" value="{accion}">{extra}<button style="{st};{estilo}">{escape(texto)}</button></form>')
    barra = [f'<a href="{url_for("web.presupuestos", eid=eid)}" style="{st};border:1px solid #ccc;color:#191919;text-decoration:none;background:#fff">← Presupuestos</a>',
             f'<button type="button" data-imprimir style="{st};border:1px solid #ccc;background:#fff">Imprimir / PDF</button>']
    if tiene_permiso(g.usuario, "presupuestos.emitir") and p.estado not in ("Facturado", "Anulado"):
        verde = "border:0;background:#87e64b;color:#1a4200"
        barra += [form("whatsapp", "Enviar por WhatsApp", verde),
                  form("correo", "Enviar por correo", verde, f'<input name="correo" type="email" placeholder="{escape(p.cliente_email or "correo")}" style="padding:9px;border:1px solid #ccc;border-radius:10px;margin-right:4px">')]
        if p.estado in ("Borrador", "Enviado"): barra += [form("Aceptado", "Aceptado", "border:0;background:#191919;color:#fff"), form("Rechazado", "Rechazado", "border:1px solid #ccc;background:#fff")]
        if p.estado in ("Enviado", "Aceptado", "Borrador") and tiene_permiso(g.usuario, "facturacion.emitir") and "facturacion" in g.modulos:
            series = db.session.execute(select(SerieFacturacion).where(SerieFacturacion.empresa_id == eid, SerieFacturacion.activa.is_(True))).scalars().all()
            if series:
                opts = "".join(f'<option value="{x.id}">Serie {escape(x.serie or "—")}</option>' for x in series)
                barra.append(form("facturar", "Facturar (crédito)", "border:0;background:#191919;color:#fff",
                                  f'<select name="serie_id" style="padding:9px;border-radius:10px;margin-right:4px">{opts}</select>'
                                  f'<input name="dias" type="number" min="0" value="0" title="Días de crédito" style="width:70px;padding:9px;border:1px solid #ccc;border-radius:10px;margin-right:4px">'))
    acciones = (f'<div class="no-print" style="max-width:680px;margin:16px auto 0;font-family:sans-serif;display:flex;gap:8px;flex-wrap:wrap;align-items:center">{"".join(barra)}</div>'
                + "".join(f'<div class="no-print" role="alert" style="max-width:680px;margin:10px auto 0;padding:10px 14px;border-radius:10px;background:#eef9e6;font-family:sans-serif">{escape(m)}</div>'
                          for m in get_flashed_messages())
                + f'<script src="{url_for("static", filename="app.js")}" defer></script><style>@media print{{.no-print{{display:none!important}}}}</style>')
    g.csp_estilos_en_linea = g.csp_scripts_propios = True
    return Response(html(p).replace("</body>", acciones + "</body>", 1), mimetype="text/html")


@bp.route("/pv/<token>")
@limiter.limit("60/minute")
def presupuesto_publico(token):
    from itsdangerous import BadSignature, URLSafeTimedSerializer
    from . import rls
    try:
        org, eid, pid = URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="presupuesto").loads(token, max_age=90 * 86400)
    except BadSignature:
        abort(404)
    rls.fijar_org(db, int(org))
    p = db.session.execute(select(PresupuestoVenta).where(PresupuestoVenta.id == int(pid), PresupuestoVenta.empresa_id == int(eid))).scalar_one_or_none()
    emp = db.session.get(Empresa, int(eid))
    if p is None or emp is None or emp.organizacion_id != int(org): abort(404)
    g.csp_estilos_en_linea = True
    return Response(html(p), mimetype="text/html")
