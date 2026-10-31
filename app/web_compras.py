"""Administración → Órdenes de compra, Proveedores, Pedidos por WhatsApp y Bot de WhatsApp (+ webhook público de Meta)."""
import json
import secrets

from flask import Response, abort, current_app, flash, g, get_flashed_messages, redirect, request, url_for
from markupsafe import escape
from sqlalchemy import select

from . import plantilla_correo as pc
from . import servicios_compras as SC
from . import whatsapp as WA
from .auditoria import registrar
from .extensions import csrf, db, limiter
from .models import Empresa
from .models2 import Producto
from .models_compras import ESTADOS_PEDIDO, OrdenCompra, Pedido, Proveedor, WhatsappConfig, WhatsappContacto, WhatsappMensaje
from .models_fact import ClienteFact, SerieFacturacion
from .security import tiene_permiso
from .tiempo import hoy_ve
from .web import F, bp, campo, hoy, intentar, pagina, ruta, validar_rif

h = lambda v: campo("accion", "", "hidden", valor=v)
N_LINEAS = 8


# ====================================================================== proveedores
@ruta("/e/<int:eid>/compras/proveedores", "compras.ver", ("GET", "POST"))
def proveedores(eid):
    if request.method == "POST":
        if not tiene_permiso(g.usuario, "compras.operar"): return ("", 403)
        def guardar():
            rif = validar_rif(F["rif"])
            p = db.session.execute(select(Proveedor).where(Proveedor.empresa_id == eid, Proveedor.rif == rif)).scalar_one_or_none() or Proveedor(empresa_id=eid, rif=rif)
            p.razon_social = F["razon_social"].strip()[:200]
            for k in ("direccion", "telefono", "email", "contacto"): setattr(p, k, (F.get(k) or "").strip()[:300] or None)
            p.whatsapp = WA.telefono(F["whatsapp"]) if F.get("whatsapp") else None
            db.session.add(p); registrar(g.usuario.username, "PROVEEDOR_GUARDADO", f"empresa={eid} {rif}"); db.session.commit()
        intentar(guardar, "Proveedor guardado"); return redirect(request.url)
    ps = db.session.execute(select(Proveedor).where(Proveedor.empresa_id == eid).order_by(Proveedor.razon_social)).scalars().all()
    secs = [dict(tabla=dict(cols=["RIF", "Razón social", "Teléfono", "WhatsApp", "Correo", "Contacto"], filas=[
        dict(cells=[p.rif, p.razon_social, p.telefono or "", ("+" + p.whatsapp) if p.whatsapp else "—", p.email or "", p.contacto or ""],
             url=url_for("web.ordenes_compra", eid=eid, proveedor=p.id)) for p in ps]))]
    if tiene_permiso(g.usuario, "compras.operar"):
        secs.append(dict(titulo="Nuevo proveedor (o actualizar por RIF)", nota="Con su WhatsApp, el proveedor puede ver, confirmar o rechazar sus órdenes de compra desde el chat del bot.",
                         form=dict(action=request.path, boton="Guardar", campos=[campo("rif", "RIF"), campo("razon_social", "Razón social"), campo("direccion", "Domicilio fiscal", req=False),
                                   campo("telefono", "Teléfono", req=False), campo("whatsapp", "WhatsApp (58412…)", req=False), campo("email", "Correo", "email", req=False),
                                   campo("contacto", "Persona de contacto", req=False)])))
    return pagina("Proveedores", eid, secs)


# ====================================================================== órdenes de compra
def _destinatario(eid, sel):
    """«P:id» proveedor, «C:id» cliente de facturación o vacío (datos escritos a mano)."""
    if sel.startswith("P:"):
        p = db.session.get(Proveedor, int(sel[2:]))
        if p is None or p.empresa_id != eid: raise ValueError("Proveedor inexistente")
        return dict(tipo_destinatario="proveedor", dest_rif=p.rif, dest_nombre=p.razon_social, dest_direccion=p.direccion, dest_telefono=p.telefono,
                    dest_email=p.email, dest_whatsapp=p.whatsapp)
    if sel.startswith("C:"):
        c = db.session.get(ClienteFact, int(sel[2:]))
        if c is None or c.empresa_id != eid: raise ValueError("Cliente inexistente")
        return dict(tipo_destinatario="cliente", dest_rif=c.rif, dest_nombre=c.razon_social, dest_direccion=c.direccion, dest_telefono=c.telefono,
                    dest_email=c.email, dest_whatsapp=F.get("dest_whatsapp") or None)
    return dict(tipo_destinatario=F.get("tipo_destinatario") or "proveedor", dest_rif=validar_rif(F.get("dest_rif")), dest_nombre=F.get("dest_nombre"),
                dest_direccion=F.get("dest_direccion"), dest_telefono=F.get("dest_telefono"), dest_email=F.get("dest_email"), dest_whatsapp=F.get("dest_whatsapp"))


@ruta("/e/<int:eid>/compras/ordenes", "compras.ver", ("GET", "POST"))
def ordenes_compra(eid):
    if request.method == "POST":
        if not tiene_permiso(g.usuario, "compras.operar"): return ("", 403)
        def crear():
            lineas = [dict(codigo=F.get(f"l{i}_cod"), descripcion=(F.get(f"l{i}_desc") or "").strip() or _nombre_producto(eid, F.get(f"l{i}_cod")),
                           cantidad=F.get(f"l{i}_cant") or "1", precio=F.get(f"l{i}_precio") or "0", alicuota=F.get(f"l{i}_alic") or "G", unidad=F.get(f"l{i}_und"))
                      for i in range(1, N_LINEAS + 1) if (F.get(f"l{i}_desc") or "").strip() or F.get(f"l{i}_cod")]
            datos = dict(_destinatario(eid, F.get("destinatario") or ""), **{k: F.get(k) for k in ("fecha", "fecha_entrega", "moneda", "tasa", "condicion_pago", "lugar_entrega", "observaciones")})
            oc = SC.crear_orden_compra(eid, g.usuario, datos, lineas); db.session.commit()
            return oc
        r = {}
        if intentar(lambda: r.setdefault("oc", crear()), lambda oc: f"Orden {SC.numero_oc(oc.numero)} emitida"):
            return redirect(url_for("web.orden_compra_ver", eid=eid, oid=r["oc"].id))
        return redirect(request.url)
    q = select(OrdenCompra).where(OrdenCompra.empresa_id == eid).order_by(OrdenCompra.id.desc())
    prov_sel = request.args.get("proveedor", "")
    if prov_sel.isdigit():
        p = db.session.get(Proveedor, int(prov_sel))
        if p and p.empresa_id == eid: q = q.where(OrdenCompra.dest_rif == p.rif)
    ocs = db.session.execute(q.limit(500)).scalars().all()
    secs = [dict(kpis=[("Órdenes", len(ocs)), ("Por confirmar", sum(o.estado in ("Emitida", "Enviada") for o in ocs)),
                       ("Confirmadas", sum(o.estado == "Confirmada" for o in ocs)), ("Recibidas", sum(o.estado == "Recibida" for o in ocs))]),
            dict(titulo="Órdenes de compra", tabla=dict(cols=["N°", "Fecha", "Destinatario", "RIF", "Moneda", "Total", "Total Bs", "Estado", "Origen"], num=[5, 6], filas=[
                dict(cells=[SC.numero_oc(o.numero), o.fecha, o.dest_nombre, o.dest_rif, o.moneda, o.total, o.total_bs, o.estado, o.origen],
                     url=url_for("web.orden_compra_ver", eid=eid, oid=o.id), clase="anulado" if o.estado == "Anulada" else "") for o in ocs]))]
    if tiene_permiso(g.usuario, "compras.operar"):
        emp = db.session.get(Empresa, eid)
        dests = [("", "— Escribir los datos abajo —")]
        dests += [(f"P:{p.id}", f"Proveedor · {p.razon_social} ({p.rif})") for p in db.session.execute(select(Proveedor).where(Proveedor.empresa_id == eid).order_by(Proveedor.razon_social)).scalars()]
        dests += [(f"C:{c.id}", f"Cliente · {c.razon_social} ({c.rif})") for c in db.session.execute(select(ClienteFact).where(ClienteFact.empresa_id == eid).order_by(ClienteFact.razon_social)).scalars()]
        prods = [("", "—")] + [(p.codigo, f"{p.codigo} {p.nombre}") for p in db.session.execute(select(Producto).where(Producto.empresa_id == eid).order_by(Producto.codigo)).scalars()]
        monedas = [("VES", "Bolívares")] + [(m, m) for m in emp.divisas()]
        alic = [("G", "IVA general"), ("R", "IVA reducida"), ("A", "IVA lujo"), ("E", "Exento")]
        lineas = sum([[campo(f"l{i}_cod", f"Producto {i} (inventario)", "select", req=False, opciones=prods), campo(f"l{i}_desc", "Descripción", req=False),
                       campo(f"l{i}_cant", "Cant.", "number", req=False), campo(f"l{i}_und", "Unidad", req=False), campo(f"l{i}_precio", "Precio unit. sin IVA", "number", req=False),
                       campo(f"l{i}_alic", "IVA", "select", opciones=alic)] for i in range(1, N_LINEAS + 1)], [])
        secs.append(dict(titulo="Nueva orden de compra", nota=f"Emisor: {emp.razon_social} (RIF {emp.rif}). Elija el proveedor o cliente o escriba sus datos. "
                         "Las líneas con producto del inventario entran al kardex cuando la orden se marca como recibida.",
                         form=dict(action=request.path, boton="Emitir orden de compra", campos=[
                             campo("destinatario", "Destinatario", "select", req=False, opciones=dests, valor=f"P:{prov_sel}" if prov_sel.isdigit() else ""),
                             campo("tipo_destinatario", "Tipo (si lo escribe)", "select", opciones=[("proveedor", "Proveedor"), ("cliente", "Cliente")]),
                             campo("dest_nombre", "Razón social (si lo escribe)", req=False), campo("dest_rif", "RIF (si lo escribe)", req=False),
                             campo("dest_direccion", "Dirección", req=False), campo("dest_telefono", "Teléfono", req=False), campo("dest_email", "Correo", "email", req=False),
                             campo("dest_whatsapp", "WhatsApp (58412…)", req=False),
                             campo("fecha", "Fecha", "date", valor=hoy()), campo("fecha_entrega", "Entrega", "date", req=False),
                             campo("moneda", "Moneda", "select", opciones=monedas), campo("tasa", "Tasa BCV (vacío = la del día)", "number", req=False),
                             campo("condicion_pago", "Condición de pago", req=False, valor="Crédito 15 días"), campo("lugar_entrega", "Lugar de entrega", req=False, valor=emp.direccion or ""),
                             campo("observaciones", "Observaciones", "textarea", req=False)] + lineas)))
    return pagina("Órdenes de compra", eid, secs, acciones=[dict(url=url_for("web.proveedores", eid=eid), texto="Proveedores")])


def _nombre_producto(eid, cod):
    p = db.session.get(Producto, (eid, cod)) if cod else None
    return p.nombre if p else ""


def _boton(texto, accion, eid, oid, extra="", color="#87e64b", tinta="#1a4200", confirmar=None):
    from flask_wtf.csrf import generate_csrf
    conf = f' data-confirmar="{escape(confirmar)}"' if confirmar else ""
    return (f'<form method="post" action="{url_for("web.orden_compra_accion", eid=eid, oid=oid)}"{conf}>'
            f'<input type="hidden" name="csrf_token" value="{generate_csrf()}"><input type="hidden" name="accion" value="{accion}">{extra}'
            f'<button style="padding:10px 14px;border:0;border-radius:10px;background:{color};color:{tinta};font-weight:700;cursor:pointer">{escape(texto)}</button></form>')


@ruta("/e/<int:eid>/compras/ordenes/<int:oid>", "compras.ver")
def orden_compra_ver(eid, oid):
    """La orden tal como la recibe el proveedor (mismo modelo de la factura de Gigadevs), lista para imprimir o guardar en PDF."""
    oc = SC.orden(eid, oid)
    st = 'style="padding:10px 14px;border:1px solid #ccc;border-radius:10px;color:#191919;text-decoration:none;background:#fff"'
    barra = [f'<a href="{url_for("web.ordenes_compra", eid=eid)}" {st}>← Volver</a>', f'<button type="button" data-imprimir {st}>Imprimir / PDF</button>']
    if tiene_permiso(g.usuario, "compras.operar") and oc.estado not in ("Anulada", "Recibida"):
        if oc.dest_email: barra.append(_boton("Enviar por correo", "correo", eid, oid))
        if oc.dest_whatsapp: barra.append(_boton("Enviar por WhatsApp", "whatsapp", eid, oid))
        if oc.estado in ("Emitida", "Enviada"): barra.append(_boton("Marcar confirmada", "confirmar", eid, oid, color="#eef3ff", tinta="#191919"))
        inp = lambda n, ph, t="text": f'<input name="{n}" type="{t}" placeholder="{ph}" style="padding:9px;border:1px solid #ccc;border-radius:10px;margin-right:4px;width:130px">'
        barra.append(_boton("Recibir mercancía", "recibir", eid, oid, inp("factura", "N° factura prov.") + inp("control", "N° control"),
                            color="#191919", tinta="#fff", confirmar="¿Registrar la recepción? Las líneas con producto entran al inventario y la factura al libro de compras."))
    if tiene_permiso(g.usuario, "compras.operar") and oc.estado == "Recibida" and not oc.cxp_id:
        inp = lambda n, ph, t="text": f'<input name="{n}" type="{t}" required placeholder="{ph}" style="padding:9px;border:1px solid #ccc;border-radius:10px;margin-right:4px;width:130px">'
        barra.append(_boton("Registrar factura del proveedor", "factura", eid, oid, inp("factura", "N° factura") + inp("control", "N° control") + inp("fecha", "", "date")))
    if tiene_permiso(g.usuario, "compras.operar") and oc.estado not in ("Anulada", "Recibida"):
        barra.append(_boton("Anular", "anular", eid, oid, '<input name="motivo" required placeholder="Motivo" style="padding:9px;border:1px solid #ccc;border-radius:10px;margin-right:4px">',
                            color="#c8372d", tinta="#fff"))
    acciones = (f'<div class="no-print" style="max-width:680px;margin:16px auto 0;font-family:sans-serif;display:flex;gap:8px;flex-wrap:wrap;align-items:center">{"".join(barra)}</div>'
                + "".join(f'<div class="no-print" role="alert" style="max-width:680px;margin:10px auto 0;padding:10px 14px;border-radius:10px;background:#eef9e6;font-family:sans-serif">{escape(m)}</div>'
                          for m in get_flashed_messages())
                + (f'<div class="no-print" style="max-width:680px;margin:10px auto 0;font-family:sans-serif;font-size:13px;color:#707070">Enlace para el destinatario (60 días): '
                   f'<code>{escape(SC.enlace_publico(oc))}</code></div>' if oc.estado != "Anulada" else "")
                + f'<script src="{url_for("static", filename="app.js")}" defer></script><style>@media print{{.no-print{{display:none!important}}}}</style>')
    html = pc.orden_compra_html(SC.oc_dict(oc), current_app.config["APP_BASE_URL"])
    html = html.replace("</body>", acciones + "</body>", 1)
    g.csp_estilos_en_linea = g.csp_scripts_propios = True   # app.js (archivo propio): imprimir y confirmar; ningún script en línea
    return Response(html, mimetype="text/html")


@bp.route("/oc/<token>")
@limiter.limit("60/minute")
def orden_compra_publica(token):
    """Vista de la orden para el proveedor (sin usuario): enlace firmado con la clave de la aplicación, vence a los 60 días."""
    from . import rls
    datos = SC.leer_enlace(token)
    if not datos: abort(404)
    org, eid, oid = datos
    rls.fijar_org(db, int(org))
    oc = db.session.execute(select(OrdenCompra).where(OrdenCompra.id == int(oid), OrdenCompra.empresa_id == int(eid))).scalar_one_or_none()
    emp = db.session.get(Empresa, int(eid))
    if oc is None or emp is None or emp.organizacion_id != int(org): abort(404)
    g.csp_estilos_en_linea = True
    return Response(pc.orden_compra_html(SC.oc_dict(oc), current_app.config["APP_BASE_URL"]), mimetype="text/html")


@bp.route("/ov/<token>")
@limiter.limit("60/minute")
def orden_venta_publica(token):
    """Orden de venta (o pedido facturado) que el bot envía al cliente por WhatsApp. Enlace firmado, 60 días."""
    from . import rls
    datos = SC.leer_enlace_pedido(token)
    if not datos: abort(404)
    org, eid, pid = datos
    rls.fijar_org(db, int(org))
    p = db.session.execute(select(Pedido).where(Pedido.id == int(pid), Pedido.empresa_id == int(eid))).scalar_one_or_none()
    emp = db.session.get(Empresa, int(eid))
    if p is None or emp is None or emp.organizacion_id != int(org): abort(404)
    g.csp_estilos_en_linea = True
    return Response(_html_pedido(p), mimetype="text/html")


def _html_pedido(p):
    from .models_fact import DocumentoVenta
    fac = db.session.get(DocumentoVenta, p.documento_venta_id) if p.documento_venta_id else None
    pie = (f"Factura N° {fac.serie}{fac.numero:08d} · control {fac.numero_control} emitida a su nombre. " if fac else "") + \
          "Presente este documento en nuestra oficina para pagar y retirar, o pague por transferencia y envíe el comprobante por WhatsApp."
    cfg = SC.config_wa(p.empresa_id)
    return pc.orden_compra_html(SC.pedido_dict(p), current_app.config["APP_BASE_URL"], cfg.instrucciones_pago if cfg else None,
                                titulo="ORDEN DE VENTA", etiqueta="Pedido recibido por WhatsApp", pie=pie,
                                aviso="Orden de venta: no es una factura. " + ("La factura fiscal correspondiente ya fue emitida." if fac else "La factura fiscal se emite al confirmar el pago o el despacho."))


@ruta("/e/<int:eid>/whatsapp/pedidos/<int:pid>", "whatsapp.ver")
def pedido_ver(eid, pid):
    p = db.session.execute(select(Pedido).where(Pedido.id == pid, Pedido.empresa_id == eid)).scalar_one_or_none()
    if p is None: abort(404)
    g.csp_estilos_en_linea = True
    volver = f'<div style="max-width:680px;margin:16px auto;font-family:sans-serif"><a href="{url_for("web.pedidos", eid=eid)}">← Pedidos</a></div>'
    return Response(_html_pedido(p).replace("</body>", volver + "</body>", 1), mimetype="text/html")


@ruta("/e/<int:eid>/compras/ordenes/<int:oid>/accion", "compras.operar", ("POST",))
def orden_compra_accion(eid, oid):
    a = F.get("accion")
    def hacer():
        oc = SC.orden(eid, oid, bloquear=True)
        n = SC.numero_oc(oc.numero)
        if a == "correo":
            from . import correo
            if not oc.dest_email: raise ValueError("El destinatario no tiene correo")
            emp = db.session.get(Empresa, eid)
            intro = f"Estimado(a) {oc.dest_nombre}: {emp.razon_social} le envía la orden de compra {n}."
            correo.enviar(oc.dest_email, f"Orden de compra {n} · {emp.razon_social}", intro + f"\n\nVer en línea: {SC.enlace_publico(oc)}",
                          html=pc.orden_compra_html(SC.oc_dict(oc), current_app.config["APP_BASE_URL"], intro))
            if oc.estado == "Emitida": SC.cambiar_estado_oc(eid, g.usuario.username, oc, "Enviada")
            oc.enviado_en = SC._ahora(); msg = f"Orden {n} enviada a {oc.dest_email}"
        elif a == "whatsapp":
            emp = db.session.get(Empresa, eid)
            lineas = "\n".join(f"• {l.descripcion} × {format(l.cantidad.normalize(), 'f')}" for l in oc.lineas[:15])
            texto = (f"*{emp.razon_social}* (RIF {emp.rif}) le envía la *orden de compra N° {oc.numero}*.\n{lineas}\n"
                     f"*Total: {oc.moneda} {pc._bs(oc.total)}*\nVer la orden: {SC.enlace_publico(oc)}\n"
                     f"Responda *CONFIRMAR {oc.numero}* para aceptarla o *RECHAZAR {oc.numero} motivo*.")
            try:
                SC.enviar_wa(eid, oc.dest_whatsapp, texto)
            except WA.ErrorWhatsapp as e:
                db.session.commit(); raise ValueError(str(e)) from e
            if oc.estado == "Emitida": SC.cambiar_estado_oc(eid, g.usuario.username, oc, "Enviada")
            oc.enviado_en = SC._ahora(); msg = f"Orden {n} enviada por WhatsApp"
        elif a == "confirmar": SC.cambiar_estado_oc(eid, g.usuario.username, oc, "Confirmada"); msg = f"Orden {n} confirmada"
        elif a == "recibir":
            k = SC.recibir_orden(eid, g.usuario, oc, hoy_ve().isoformat(), F.get("factura"), F.get("control"))
            msg = f"Orden {n} recibida: {k} producto(s) entraron al inventario" + (
                "; la factura del proveedor quedó en CxP y en el libro de compras" if oc.cxp_id else "; falta registrar la factura del proveedor (libro de compras)")
        elif a == "factura":
            SC.registrar_factura_proveedor(eid, g.usuario, oc, F.get("factura"), F.get("control"), F.get("fecha") or hoy_ve().isoformat())
            msg = "Factura del proveedor registrada: entra a CxP y al libro de compras"
        elif a == "anular":
            if not (F.get("motivo") or "").strip(): raise ValueError("Indique el motivo")
            SC.cambiar_estado_oc(eid, g.usuario.username, oc, "Anulada", F["motivo"].strip()); msg = f"Orden {n} anulada"
        else: raise ValueError("Acción inválida")
        db.session.commit(); return msg
    try:
        intentar(hacer, lambda m: m)
    except OSError as e:  # correo caído
        db.session.rollback(); flash(f"No se pudo enviar el correo: {e}")
    return redirect(url_for("web.orden_compra_ver", eid=eid, oid=oid))


# ====================================================================== pedidos por WhatsApp
@ruta("/e/<int:eid>/whatsapp/pedidos", "whatsapp.ver", ("GET", "POST"))
def pedidos(eid):
    if request.method == "POST":
        if not tiene_permiso(g.usuario, "whatsapp.operar"): return ("", 403)
        def hacer():
            p = db.session.execute(select(Pedido).where(Pedido.id == int(F["pid"]), Pedido.empresa_id == eid).with_for_update()).scalar_one_or_none()
            if p is None: raise ValueError("Pedido inexistente")
            if F["accion"] == "estado":
                nuevo = F["estado"]
                if nuevo not in ESTADOS_PEDIDO or nuevo == "Convertido": raise ValueError("Estado inválido")
                p.estado = nuevo
                registrar(g.usuario.username, "PEDIDO_" + nuevo.upper(), f"empresa={eid} N°{p.numero}")
                aviso = {"Confirmado": f"✅ Su pedido N° {p.numero} fue confirmado.", "Despachado": f"🚚 Su pedido N° {p.numero} va en camino.",
                         "Rechazado": f"Lo sentimos: su pedido N° {p.numero} no pudo ser atendido."}.get(nuevo)
                texto = ((aviso or "") + ("\n" + F["mensaje"].strip() if (F.get("mensaje") or "").strip() else "")).strip()
                err = SC.notificar_pedido(eid, p, texto) if texto and F.get("avisar") == "1" else None
                db.session.commit()
                return f"Pedido N° {p.numero}: {nuevo}" + (f" (no se pudo avisar por WhatsApp: {err})" if err else "")
            if F["accion"] == "oc":
                if p.tipo != "compra": raise ValueError("Solo las ofertas de proveedores se convierten en orden de compra")
                if not p.lineas: raise ValueError("La oferta no tiene líneas con código, cantidad y precio; emita la orden a mano")
                prov = db.session.execute(select(Proveedor).where(Proveedor.empresa_id == eid, Proveedor.whatsapp == p.telefono)).scalars().first()
                c = db.session.get(WhatsappContacto, p.contacto_id) if p.contacto_id else None
                datos = dict(tipo_destinatario="proveedor", dest_rif=(prov.rif if prov else (c.rif if c else None)) or F.get("rif") or "", dest_nombre=(prov.razon_social if prov else p.nombre) or "",
                             dest_direccion=prov.direccion if prov else None, dest_email=prov.email if prov else None, dest_telefono=prov.telefono if prov else None,
                             dest_whatsapp=p.telefono, moneda=F.get("moneda") or "VES", observaciones=f"Según oferta N° {p.numero} recibida por WhatsApp")
                if not datos["dest_rif"]: raise ValueError("Registre al proveedor (RIF) o indíquelo para generar la orden")
                datos["dest_rif"] = validar_rif(datos["dest_rif"])
                oc = SC.crear_orden_compra(eid, g.usuario, datos, [dict(l, alicuota="G") for l in p.lineas], origen="whatsapp", pedido_id=p.id)
                p.estado, p.orden_compra_id = "Convertido", oc.id
                db.session.commit(); return f"Se generó la orden {SC.numero_oc(oc.numero)}"
            raise ValueError("Acción inválida")
        intentar(hacer, lambda m: m); return redirect(request.url)
    ps = db.session.execute(select(Pedido).where(Pedido.empresa_id == eid).order_by(Pedido.id.desc()).limit(300)).scalars().all()
    cs = db.session.execute(select(WhatsappContacto).where(WhatsappContacto.empresa_id == eid).order_by(WhatsappContacto.ultimo_mensaje.desc().nullslast()).limit(200)).scalars().all()
    cfg = SC.config_wa(eid)
    secs = []
    if cfg is None:
        secs.append(dict(titulo="El bot aún no está conectado", nota="Configure el número de WhatsApp Business de la empresa en «Bot de WhatsApp» para recibir pedidos de clientes y ofertas de proveedores."))
    secs += [dict(kpis=[("Pedidos nuevos", sum(p.estado == "Nuevo" and p.tipo == "venta" for p in ps)), ("Ofertas de proveedores", sum(p.estado == "Nuevo" and p.tipo == "compra" for p in ps)),
                        ("Contactos", len(cs)), ("Proveedores en el chat", sum(c.tipo == "proveedor" for c in cs))]),
             dict(titulo="Pedidos y ofertas", tabla=dict(cols=["N°", "Fecha", "Tipo", "Contacto", "Teléfono", "Productos", "Total", "Estado"], num=[6], filas=[
                 dict(cells=[p.numero, p.creado_en.strftime("%Y-%m-%d %H:%M"), ("Orden de compra (PDF)" if p.origen == "whatsapp_pdf" else "Pedido de cliente") if p.tipo == "venta" else "Oferta de proveedor", p.nombre or "", ("+" + p.telefono) if p.telefono else "",
                             "; ".join(f"{l.get('codigo') or ''} ×{l.get('cantidad')}" for l in p.lineas)[:80] or (p.nota or "")[:80], p.total, p.estado],
                      url=url_for("web.pedidos", eid=eid, ver=p.id)) for p in ps]))]
    ver = request.args.get("ver", "")
    p = next((x for x in ps if str(x.id) == ver), None)
    if p:
        if p.tipo == "venta": secs.append(dict(enlace=dict(url=url_for("web.pedido_ver", eid=eid, pid=p.id), texto="Ver orden de venta")))
        secs.append(dict(titulo=f"Detalle del {'pedido' if p.tipo == 'venta' else 'oferta'} N° {p.numero}", nota=p.nota,
                         tabla=dict(cols=["Código", "Descripción", "Cantidad", "Precio", "Subtotal"], num=[2, 3, 4], filas=[
                             dict(cells=[l.get("codigo"), l.get("descripcion"), l.get("cantidad"), l.get("precio"),
                                         f"{float(l.get('cantidad') or 0) * float(l.get('precio') or 0):,.2f}"]) for l in p.lineas])))
    if tiene_permiso(g.usuario, "whatsapp.operar") and ps:
        sel = [(x.id, f"N° {x.numero} · {x.nombre or x.telefono} · {x.estado}") for x in ps if x.estado not in ("Convertido",)]
        secs += [dict(titulo="Atender pedido", nota="Puede avisar al cliente por WhatsApp. Para facturar un pedido confirmado use Facturación → Nueva factura.",
                      form=dict(action=request.path, boton="Actualizar", campos=[h("estado"), campo("pid", "Pedido", "select", opciones=sel, valor=ver),
                                campo("estado", "Nuevo estado", "select", opciones=[(e, e) for e in ESTADOS_PEDIDO if e != "Convertido"]),
                                campo("avisar", "¿Avisar por WhatsApp?", "select", opciones=[("1", "Sí"), ("0", "No")]), campo("mensaje", "Mensaje adicional", req=False)])),
                 dict(titulo="Convertir oferta de proveedor en orden de compra", form=dict(action=request.path, boton="Generar orden de compra", campos=[h("oc"),
                      campo("pid", "Oferta", "select", opciones=[(x.id, f"N° {x.numero} · {x.nombre or x.telefono}") for x in ps if x.tipo == "compra" and x.estado == "Nuevo"]),
                      campo("rif", "RIF del proveedor (si no está registrado)", req=False), campo("moneda", "Moneda de los precios", "select", opciones=[("VES", "Bolívares"), ("USD", "USD"), ("EUR", "EUR")])]))]
    secs.append(dict(titulo="Conversaciones", tabla=dict(cols=["Contacto", "Teléfono", "Tipo", "Último mensaje"], filas=[
        dict(cells=[c.nombre or "—", "+" + c.telefono, c.tipo.capitalize(), c.ultimo_mensaje.strftime("%Y-%m-%d %H:%M") if c.ultimo_mensaje else "—"],
             url=url_for("web.whatsapp_chat", eid=eid, cid=c.id)) for c in cs])))
    return pagina("Pedidos por WhatsApp", eid, secs)


@ruta("/e/<int:eid>/whatsapp/contacto/<int:cid>", "whatsapp.ver", ("GET", "POST"))
def whatsapp_chat(eid, cid):
    c = db.session.get(WhatsappContacto, cid)
    if c is None or c.empresa_id != eid: abort(404)
    if request.method == "POST":
        if not tiene_permiso(g.usuario, "whatsapp.operar"): return ("", 403)
        if F.get("accion") == "responder":
            def resp():
                try:
                    SC.enviar_wa(eid, c.telefono, F["texto"].strip()[:4000], c)
                finally:
                    db.session.commit()
            try:
                intentar(resp, "Mensaje enviado")
            except WA.ErrorWhatsapp as e:
                flash(str(e))
        elif F.get("accion") == "ficha":
            def ficha():
                if F["tipo"] not in ("nuevo", "cliente", "proveedor", "bloqueado"): raise ValueError("Tipo inválido")
                c.tipo, c.nombre = F["tipo"], (F.get("nombre") or "").strip()[:120] or c.nombre
                c.rif = validar_rif(F["rif"]) if F.get("rif") else c.rif
                c.estado = {}
                registrar(g.usuario.username, "WA_CONTACTO", f"empresa={eid} …{c.telefono[-4:]} tipo={c.tipo}"); db.session.commit()
            intentar(ficha, "Contacto actualizado")
        return redirect(request.url)
    ms = db.session.execute(select(WhatsappMensaje).where(WhatsappMensaje.empresa_id == eid, WhatsappMensaje.contacto_id == cid)
                            .order_by(WhatsappMensaje.id.desc()).limit(100)).scalars().all()
    secs = [dict(pares=[("Teléfono", "+" + c.telefono), ("Nombre", c.nombre or "—"), ("Tipo", c.tipo.capitalize()), ("RIF", c.rif or "—")]),
            dict(titulo="Conversación (más reciente primero)", tabla=dict(cols=["Fecha", "De", "Mensaje", "Envío"], filas=[
                dict(cells=[m.ts.strftime("%Y-%m-%d %H:%M"), "Contacto" if m.direccion == "in" else "Empresa", m.texto[:500], "" if m.direccion == "in" else (m.resultado or "")]) for m in ms]))]
    if tiene_permiso(g.usuario, "whatsapp.operar"):
        secs += [dict(titulo="Responder", nota="Meta solo permite texto libre dentro de las 24 h siguientes al último mensaje del contacto.",
                      form=dict(action=request.path, boton="Enviar", campos=[h("responder"), campo("texto", "Mensaje", "textarea")])),
                 dict(titulo="Ficha del contacto", nota="Marcar como proveedor le da el menú de proveedores (órdenes de compra y ofertas). «Bloqueado» hace que el bot lo ignore.",
                      form=dict(action=request.path, boton="Guardar", campos=[h("ficha"), campo("tipo", "Tipo", "select", opciones=[("cliente", "Cliente"), ("proveedor", "Proveedor"), ("nuevo", "Sin clasificar"), ("bloqueado", "Bloqueado")], valor=c.tipo),
                                campo("nombre", "Nombre", req=False, valor=c.nombre), campo("rif", "RIF", req=False, valor=c.rif)]))]
    return pagina(f"WhatsApp · {c.nombre or '+' + c.telefono}", eid, secs, acciones=[dict(url=url_for("web.pedidos", eid=eid), texto="← Pedidos")])


# ====================================================================== configuración del bot
@ruta("/e/<int:eid>/whatsapp/bot", "whatsapp.configurar", ("GET", "POST"))
def whatsapp_config(eid):
    cfg = SC.config_wa(eid)
    cif = current_app.extensions["cifrador"]
    if request.method == "POST":
        a = F.get("accion")
        def guardar():
            nonlocal cfg
            if a == "guardar":
                if cfg is None:
                    if not (F.get("token") and F.get("app_secret")): raise ValueError("Indique el token de acceso y el App Secret")
                    cfg = WhatsappConfig(empresa_id=eid, clave_webhook=secrets.token_urlsafe(24), verify_token=secrets.token_urlsafe(18))
                    db.session.add(cfg)
                cfg.phone_number_id = F["phone_number_id"].strip()
                if not cfg.phone_number_id.isdigit(): raise ValueError("El Phone Number ID es numérico (lo muestra Meta en WhatsApp → Configuración de la API)")
                cfg.numero_visible = (F.get("numero_visible") or "").strip()[:30] or None
                if F.get("token"): cfg.token_cifrado = cif.cifrar(F["token"].strip())
                if F.get("app_secret"): cfg.app_secret_cifrado = cif.cifrar(F["app_secret"].strip())
                cfg.bienvenida = (F.get("bienvenida") or "").strip()[:500] or None
                cfg.instrucciones_pago = (F.get("instrucciones_pago") or "").strip()[:800] or None
                cfg.factura_automatica = F.get("factura_automatica") == "1"
                sid = F.get("serie_id") or ""
                if sid and not db.session.execute(select(SerieFacturacion.id).where(SerieFacturacion.id == int(sid), SerieFacturacion.empresa_id == eid)).first():
                    raise ValueError("Serie inválida")
                cfg.serie_id = int(sid) if sid else None
                if cfg.factura_automatica and not cfg.serie_id: raise ValueError("Para facturar automáticamente elija la serie")
                cfg.dias_credito = max(0, min(int(F.get("dias_credito") or 0), 120))
                cfg.usuario_id = g.usuario.id      # el bot emite en nombre de quien lo configuró (queda en la bitácora)
                cfg.activo = F.get("activo", "1") == "1"
                registrar(g.usuario.username, "WHATSAPP_CONFIGURADO", f"empresa={eid} activo={cfg.activo}")
            elif a == "rotar":
                if cfg is None: raise ValueError("Primero configure el bot")
                cfg.clave_webhook, cfg.verify_token = secrets.token_urlsafe(24), secrets.token_urlsafe(18)
                registrar(g.usuario.username, "WHATSAPP_WEBHOOK_ROTADO", f"empresa={eid}")
            elif a == "prueba":
                SC.enviar_wa(eid, F["telefono"], "Mensaje de prueba del bot de WhatsApp de Contax ✅")
            else: raise ValueError("Acción inválida")
            db.session.commit()
        try:
            intentar(guardar, {"guardar": "Configuración guardada", "rotar": "Se generó una URL nueva: actualícela en Meta", "prueba": "Mensaje de prueba enviado"}.get(a, "Hecho"))
        except WA.ErrorWhatsapp as e:
            db.session.commit(); flash(str(e))
        return redirect(request.url)
    url = current_app.config["APP_BASE_URL"].rstrip("/") + url_for("web.whatsapp_webhook", clave=cfg.clave_webhook) if cfg else None
    series = [(x.id, f"Serie {x.serie or '(sin serie)'} · control hasta {x.control_prefijo}-{x.control_hasta:08d}") for x in db.session.execute(
        select(SerieFacturacion).where(SerieFacturacion.empresa_id == eid, SerieFacturacion.activa.is_(True))).scalars()]
    secs = [dict(pares=[("Estado", ("Activo" if cfg.activo else "Pausado") if cfg else "Sin configurar"), ("Número", (cfg.numero_visible or cfg.phone_number_id) if cfg else "—"),
                        ("URL del webhook (pegar en Meta)", url or "—"), ("Token de verificación (pegar en Meta)", cfg.verify_token if cfg else "—"),
                        ("Campo a suscribir en Meta", "messages")]),
            dict(titulo="Cómo conectarlo", nota="1) En developers.facebook.com cree una app de tipo Business con el producto WhatsApp y registre el número de la empresa. "
                 "2) Copie el Phone Number ID, un token de acceso PERMANENTE (usuario del sistema) y el App Secret (Configuración → Básica) y guárdelos abajo. "
                 "3) En WhatsApp → Configuración pegue la URL del webhook y el token de verificación de arriba y suscriba el campo «messages». "
                 "Desde ese momento el bot atiende a clientes (catálogo y pedidos con los precios del inventario) y a proveedores (órdenes de compra y ofertas). "
                 "Si un cliente o proveedor envía su ORDEN DE COMPRA en PDF, el bot la lee, la ajusta a la existencia, emite la factura (o una orden de venta) y le "
                 "responde con el total, el enlace al documento y cómo pagar o retirar en la oficina."),
            dict(titulo="Credenciales de WhatsApp Business", nota="El token y el App Secret se guardan cifrados; déjelos vacíos para conservar los actuales.",
                 form=dict(action=request.path, boton="Guardar", campos=[h("guardar"), campo("phone_number_id", "Phone Number ID", valor=cfg.phone_number_id if cfg else None),
                           campo("numero_visible", "Número visible (+58 412…)", req=False, valor=cfg.numero_visible if cfg else None),
                           campo("token", "Token de acceso permanente", "password", req=False), campo("app_secret", "App Secret", "password", req=False),
                           campo("bienvenida", "Mensaje de bienvenida (opcional)", "textarea", req=False, valor=cfg.bienvenida if cfg else None),
                           campo("instrucciones_pago", "Cómo pagar o retirar (se envía con cada orden de venta o factura)", "textarea", req=False,
                                 valor=cfg.instrucciones_pago if cfg else None, ph="Pago móvil 0102 J-12345678-9 0414-0000000 · Retiro en Av. … de 8 a 5"),
                           campo("factura_automatica", "Cuando llegue una orden de compra en PDF", "select", opciones=[("0", "Crear orden de venta (la factura la emite el personal)"),
                                 ("1", "Emitir la FACTURA automáticamente con lo disponible")], valor="1" if cfg and cfg.factura_automatica else "0"),
                           campo("serie_id", "Serie para facturar", "select", req=False, opciones=[("", "—")] + series, valor=cfg.serie_id if cfg else None),
                           campo("dias_credito", "Días de crédito de esas facturas", "number", req=False, valor=cfg.dias_credito if cfg else 0),
                           campo("activo", "Bot", "select", opciones=[("1", "Activo"), ("0", "Pausado")], valor="1" if (cfg is None or cfg.activo) else "0")]))]
    if cfg:
        secs += [dict(titulo="Enviar mensaje de prueba", form=dict(action=request.path, boton="Enviar", campos=[h("prueba"), campo("telefono", "WhatsApp destino (58412…)")])),
                 dict(titulo="Regenerar URL del webhook", nota="Si la URL o el token se filtraron: la anterior deja de funcionar al instante.",
                      form=dict(action=request.path, boton="Regenerar", confirmar="¿Regenerar la URL? Deberá actualizarla en Meta.", campos=[h("rotar")]))]
    return pagina("Bot de WhatsApp", eid, secs)


# ====================================================================== webhook público (Meta)
@bp.route("/wa/<clave>", methods=["GET", "POST"])
@csrf.exempt
@limiter.limit("300/minute")
def whatsapp_webhook(clave):
    from . import rls
    if len(clave) > 64: abort(404)
    fila = db.session.execute(db.text("SELECT empresa_id, organizacion_id FROM whatsapp_destino(:c)"), {"c": clave}).first()
    if fila is None: abort(404)
    eid, org = fila
    rls.fijar_org(db, org)
    cfg = SC.config_wa(eid)
    if cfg is None: abort(404)
    if request.method == "GET":  # verificación de la suscripción
        if request.args.get("hub.mode") == "subscribe" and secrets.compare_digest(request.args.get("hub.verify_token", ""), cfg.verify_token):
            return Response(request.args.get("hub.challenge", "")[:200], mimetype="text/plain")
        abort(403)
    cuerpo = request.get_data(cache=True)
    if not WA.firma_valida(current_app.extensions["cifrador"].descifrar(cfg.app_secret_cifrado), cuerpo, request.headers.get("X-Hub-Signature-256", "")):
        abort(403)
    est = current_app.extensions["gestor_licencia"](org).estado()
    from .planes import modulos_de
    if not (est.valida and {"facturacion", "inventario"} & set(modulos_de(est.claims))):
        return {"ok": True, "ignorado": "licencia"}  # sin licencia con el módulo: no se atiende (Meta no debe reintentar)
    try:
        aviso = json.loads(cuerpo or b"{}")
    except ValueError:
        return {"ok": True}
    modulos = db.session.get(Empresa, eid).modulos_empresa(modulos_de(est.claims))
    for tel, nombre, texto, wamid, adjunto in WA.mensajes_entrantes(aviso):
        try:
            SC.procesar_mensaje(eid, tel, nombre, texto, wamid, adjunto=adjunto, modulos=modulos); db.session.commit()
        except Exception:  # noqa: BLE001 — un mensaje defectuoso no debe frenar a los demás ni provocar reintentos en bucle
            db.session.rollback(); rls.fijar_org(db, org)
            current_app.logger.exception("Mensaje de WhatsApp no procesado")
    return {"ok": True}
