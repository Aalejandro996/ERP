"""Consola Maestro → Contratos (a partir de presupuestos aceptados), firma electrónica pública del cliente y páginas legales
(Términos y condiciones, Privacidad y Cookies)."""
import hashlib
from datetime import date

from flask import Response, abort, current_app, flash, g, get_flashed_messages, redirect, render_template, request, url_for
from markupsafe import escape

from . import legal as L
from . import plantilla_correo as pc
from .auditoria import registrar
from .extensions import db, limiter
from .web import F, bp, campo, pagina, ruta
from .web_plataforma import _admin, csrf_token_seguro

h = lambda v: campo("accion", "", "hidden", valor=v)
SALT = "contrato-firma"


def enlace_firma(cid) -> str:
    from itsdangerous import URLSafeTimedSerializer
    return current_app.config["APP_BASE_URL"].rstrip("/") + url_for("web.contrato_publico", token=URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=SALT).dumps(int(cid)))


def _con_emisor(c, em):
    return dict(c, emisor_rif=em.get("rif"))


def huella(c) -> str:
    return hashlib.sha256(L.texto_plano(c).encode()).hexdigest()


@ruta("/plataforma/contratos", "plataforma", ("GET", "POST"))
def maestro_contratos():
    from .licensing.admin import ErrorAdmin
    if request.method == "POST":
        try:
            r = _admin().crear_contrato(**{k: F.get(k) for k in ("presupuesto_id", "cliente_nombre", "cliente_rif", "cliente_domicilio", "cliente_correo", "representante",
                                                                 "representante_ci", "representante_cargo", "objeto", "forma_pago", "soporte_mensual", "inicio",
                                                                 "meses", "especiales")},
                                        opcionales=F.getlist("opcionales"), version_legal=L.VERSION, autor=g.usuario.username)
            registrar(g.usuario.username, "CONTRATO_CREADO", f"id={r['id']} presupuesto={F.get('presupuesto_id')}", org=None); db.session.commit()
            return redirect(url_for("web.maestro_contrato_ver", cid=r["id"]))
        except (ErrorAdmin, ValueError, OSError) as e:
            db.session.rollback(); flash(str(e))
        return redirect(request.url)
    try:
        adm = _admin(); cts, ps = adm.contratos(), adm.presupuestos()
    except (ErrorAdmin, ValueError, OSError) as e:
        flash(str(e)); cts, ps = [], []
    con_contrato = {c["presupuesto_id"] for c in cts if c["estado"] != "Anulado"}
    aceptados = [p for p in ps if p["estado"] == "Aceptado" and p["id"] not in con_contrato]
    sel = request.args.get("presupuesto", "")
    p = next((x for x in aceptados if str(x["id"]) == sel), aceptados[0] if aceptados else None)
    sugeridas = L.sugerir_opcionales(" ".join(l["descripcion"] + " " + (l.get("detalle") or "") for l in p["lineas"]) + " " + (p.get("condiciones") or "")) if p else []
    secs = [dict(kpis=[("Contratos", len(cts)), ("Por firmar", sum(c["estado"] in ("Borrador", "Enviado") for c in cts)), ("Firmados", sum(c["estado"] == "Firmado" for c in cts)),
                       ("Presupuestos aceptados sin contrato", len(aceptados))]),
            dict(titulo="Contratos", tabla=dict(cols=["N°", "Cliente", "Presupuesto", "Monto USD", "Vigencia", "Estado"], num=[3], filas=[
                dict(cells=[c["numero_txt"], c["cliente_nombre"], c["presupuesto_txt"], c["monto"], f"{c['inicio']} → {c['fin']}", c["estado"]],
                     url=url_for("web.maestro_contrato_ver", cid=c["id"]), clase="anulado" if c["estado"] == "Anulado" else "") for c in cts]))]
    if not aceptados:
        secs.append(dict(titulo="Armar contrato", nota="No hay presupuestos aceptados sin contrato. Marque un presupuesto como «Aceptado» en Presupuestos y vuelva aquí."))
    else:
        opciones = [(x["id"], f"{x['numero_txt']} · {x['cliente_nombre']} · ${x['total']}") for x in aceptados]
        lineas_txt = "; ".join(l["descripcion"] for l in p["lineas"])
        secs.append(dict(titulo="Armar contrato", abierto=True, nota="Elija el presupuesto aceptado: el objeto, el monto y el cliente se toman de él. Complete el representante "
                         "legal y marque las cláusulas especiales que apliquen (ya vienen sugeridas según lo cotizado). Las cláusulas fijas cubren pagos y SENIAT, "
                         "LC/FT (LOCDOFT y ONCDOFT), delitos informáticos, datos personales, propiedad intelectual, firma electrónica, responsabilidad y jurisdicción.",
                         form=dict(action=request.path, boton="Armar contrato", campos=[
                             campo("presupuesto_id", "Presupuesto aceptado", "select", opciones=opciones, valor=p["id"]),
                             campo("cliente_nombre", "Razón social del cliente", valor=p["cliente_nombre"]), campo("cliente_rif", "RIF (J-12345678-9)", valor=p.get("cliente_rif")),
                             campo("cliente_domicilio", "Domicilio fiscal", valor=p.get("cliente_direccion")), campo("cliente_correo", "Correo para notificaciones y firma", "email", valor=p.get("cliente_correo")),
                             campo("representante", "Representante legal (nombre completo)", valor=p.get("atencion")), campo("representante_ci", "Cédula del representante"),
                             campo("representante_cargo", "Cargo", valor="Presidente"),
                             campo("objeto", "Objeto (servicios)", "textarea", valor=lineas_txt),
                             campo("forma_pago", "Forma de pago", valor="50% al firmar y 50% a la entrega" if "unico" == p.get("periodo") else "100% al firmar"),
                             campo("soporte_mensual", "Soporte mensual USD (si aplica)", "number", req=False),
                             campo("inicio", "Inicio", "date", valor=date.today().isoformat()), campo("meses", "Duración (meses)", "number", valor=12),
                             campo("opcionales", "Cláusulas especiales", "checks", req=False, opciones=list(L.NOMBRE_OPCIONAL.items()), valor=sugeridas),
                             campo("especiales", "Estipulaciones adicionales (una por línea, opcional)", "textarea", req=False)])))
    return pagina("Contratos", None, secs, acciones=[dict(url=url_for("web.legal", pagina="terminos"), texto="Ver términos y condiciones")])


@ruta("/plataforma/contratos/<int:cid>", "plataforma", ("GET", "POST"))
def maestro_contrato_ver(cid):
    from .licensing.admin import ErrorAdmin
    if request.method == "POST":
        a = F.get("accion")
        try:
            adm = _admin()
            if a == "enviar": adm.enviar_contrato(cid, enlace_firma(cid), (F.get("correo") or "").strip() or None); flash("Contrato enviado al cliente para su firma")
            elif a == "anular": adm.estado_contrato(cid, "Anulado"); flash("Contrato anulado")
            else: raise ValueError("Acción inválida")
            registrar(g.usuario.username, "CONTRATO_" + a.upper(), str(cid), org=None); db.session.commit()
        except (ErrorAdmin, ValueError, OSError) as e:
            db.session.rollback(); flash(str(e))
        return redirect(request.url)
    try:
        r = _admin().contrato(cid)
    except (ErrorAdmin, ValueError, OSError) as e:
        flash(str(e)); return redirect(url_for("web.maestro_contratos"))
    c, em = _con_emisor(r["contrato"], r["emisor"]), r["emisor"]
    st = "padding:10px 14px;border-radius:10px;font-weight:700;cursor:pointer"
    form = lambda accion, texto, estilo, extra="": (f'<form method="post" action="{url_for("web.maestro_contrato_ver", cid=cid)}"><input type="hidden" name="csrf_token" value="{csrf_token_seguro()}">'
                                                   f'<input type="hidden" name="accion" value="{accion}">{extra}<button style="{st};{estilo}">{texto}</button></form>')
    integro = (c.get("firma_hash") == huella(c)) if c["estado"] == "Firmado" else None
    barra = (f'<div class="no-print" style="max-width:760px;margin:16px auto 0;font-family:sans-serif;display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
             f'<a href="{url_for("web.maestro_contratos")}" style="{st};border:1px solid #ccc;color:#191919;text-decoration:none;background:#fff">← Contratos</a>'
             f'<button type="button" data-imprimir style="{st};border:1px solid #ccc;background:#fff">Imprimir / PDF</button>'
             + (form("enviar", "Enviar para firma", "border:0;background:#87e64b;color:#1a4200",
                     f'<input name="correo" type="email" placeholder="{escape(c.get("cliente_correo") or "correo del cliente")}" style="padding:9px;border:1px solid #ccc;border-radius:10px;margin-right:4px">')
                if c["estado"] in ("Borrador", "Enviado") else "")
             + (form("anular", "Anular", "border:0;background:#c8372d;color:#fff") if c["estado"] != "Anulado" else "") + "</div>"
             + (f'<div class="no-print" style="max-width:760px;margin:10px auto 0;font-family:sans-serif;font-size:13px;color:#707070">Enlace de firma (30 días): '
                f'<code>{escape(enlace_firma(cid))}</code></div>' if c["estado"] in ("Borrador", "Enviado") else "")
             + (f'<div class="no-print" style="max-width:760px;margin:10px auto 0;padding:10px 14px;border-radius:10px;font-family:sans-serif;'
                f'background:{"#eef9e6" if integro else "#fdecea"}">{"✔ El texto actual coincide con la huella firmada." if integro else "✘ El texto actual NO coincide con la huella firmada."}</div>'
                if integro is not None else "")
             + "".join(f'<div class="no-print" role="alert" style="max-width:760px;margin:10px auto 0;padding:10px 14px;border-radius:10px;background:#eef9e6;font-family:sans-serif">{escape(m)}</div>'
                       for m in get_flashed_messages())
             + f'<script src="{url_for("static", filename="app.js")}" defer></script><style>@media print{{.no-print{{display:none!important}}}}</style>')
    html = pc.contrato_html(c, L.clausulas(c), em, current_app.config["APP_BASE_URL"]).replace("<body style=", "<body style=", 1)
    g.csp_estilos_en_linea = g.csp_scripts_propios = True
    return Response(html.replace("</body>", barra + "</body>", 1), mimetype="text/html")


@bp.route("/contrato/<token>", methods=["GET", "POST"])
@limiter.limit("30/minute")
def contrato_publico(token):
    """El cliente lee el contrato y lo acepta electrónicamente con su nombre y cédula (sin usuario de Contax)."""
    from itsdangerous import BadSignature, URLSafeTimedSerializer
    from .licensing.admin import ErrorAdmin
    try:
        cid = URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=SALT).loads(token, max_age=30 * 86400)
    except BadSignature:
        abort(404)
    try:
        adm = _admin(); r = adm.contrato(cid)
    except (ErrorAdmin, ValueError, OSError):
        abort(404)
    c = _con_emisor(r["contrato"], r["emisor"])
    if c["estado"] == "Anulado": abort(404)
    if request.method == "POST":
        try:
            if F.get("acepto") != "1": raise ValueError("Marque la casilla de aceptación")
            adm.firmar_contrato(cid, nombre=F.get("nombre"), ci=F.get("ci"), hash=huella(c), ip=request.remote_addr, agente=request.headers.get("User-Agent", "")[:200])
            registrar(f"cliente:{(F.get('ci') or '')[:12]}", "CONTRATO_FIRMADO", f"contrato={c['numero_txt']}", org=None); db.session.commit()
            flash("¡Gracias! El contrato quedó aceptado. Guarde o imprima esta página como constancia.")
        except (ErrorAdmin, ValueError, OSError) as e:
            db.session.rollback(); flash(str(e))
        return redirect(request.url)
    form = ""
    if c["estado"] in ("Borrador", "Enviado"):
        form = (f'<form method="post" style="margin-top:22px;padding:16px;border:2px solid #191919;border-radius:14px;font-family:sans-serif">'
                f'<input type="hidden" name="csrf_token" value="{csrf_token_seguro()}"><div style="font-weight:800;margin-bottom:8px">Aceptación electrónica</div>'
                '<input name="nombre" required minlength="5" placeholder="Nombre y apellido del representante" style="width:100%;padding:10px;margin:4px 0;border:1px solid #ccc;border-radius:10px">'
                '<input name="ci" required placeholder="Cédula (V-12345678)" style="width:100%;padding:10px;margin:4px 0;border:1px solid #ccc;border-radius:10px">'
                '<label style="display:block;margin:10px 0;font-size:13px"><input type="checkbox" name="acepto" value="1" required> Declaro que leí y acepto este contrato en nombre de '
                f'{escape(c["cliente_nombre"])}, que tengo facultades para obligarla y que los datos suministrados son verdaderos.</label>'
                '<button style="padding:12px 18px;border:0;border-radius:10px;background:#87e64b;color:#1a4200;font-weight:800;cursor:pointer">Aceptar y firmar</button></form>')
    avisos = "".join(f'<div role="alert" style="margin:0 0 12px;padding:10px 14px;border-radius:10px;background:#eef9e6;font-family:sans-serif">{escape(m)}</div>' for m in get_flashed_messages())
    g.csp_estilos_en_linea = g.csp_scripts_propios = True
    return Response(pc.contrato_html(c, L.clausulas(c), r["emisor"], current_app.config["APP_BASE_URL"], avisos + form), mimetype="text/html")


@bp.route("/legal")
@bp.route("/legal/<pagina>")
def legal(pagina="terminos"):
    if pagina not in L.PAGINAS: abort(404)
    titulo, secciones = L.PAGINAS[pagina]
    return render_template("legal.html", titulo=titulo, secciones=secciones, pagina=pagina, version=L.VERSION, paginas=L.PAGINAS)
