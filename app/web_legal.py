"""Consola Maestro → Legal: documentos legales de cada sistema de Gigadevs, aprobación de la abogada y registro de aceptaciones.

Flujo: plantilla (borrador) → edición (Maestro o abogada) → «Enviar a revisión» → la abogada la aprueba con su clave y su código
de verificación en dos pasos (o la devuelve con observaciones). La versión aprobada rige desde ese momento: se pide aceptarla al
próximo ingreso y queda impresa con los datos de la abogada (nombre, cédula, Inpreabogado), su firma y la huella del texto."""
import base64
import re
import secrets
import time

from flask import Response, abort, current_app, flash, g, redirect, render_template, request, url_for
from sqlalchemy import select

from . import plantilla_correo as pc
from . import sistemas
from .auditoria import registrar
from .extensions import db
from .models import Usuario
from .web import F, campo, pagina, ruta
from .web_plataforma import _admin, _fecha

h = lambda v: campo("accion", "", "hidden", valor=v)
ESTADOS = {"borrador": "Borrador", "revision": "En revisión", "aprobado": "Vigente (aprobado)", "reemplazado": "Reemplazado"}
TIPOS = {"contrato": "Contrato de licencia y suscripción", "terminos": "Términos y condiciones", "privacidad": "Privacidad", "cookies": "Cookies"}


def _nombre_sistema(sid):
    return sistemas.SISTEMAS.get(sid, {}).get("nombre", sid)


def _hacer(fn, ok):
    from .licensing.admin import ErrorAdmin
    try:
        r = fn(); db.session.commit(); flash(ok(r) if callable(ok) else ok); return r
    except (ErrorAdmin, ValueError, OSError) as e:
        db.session.rollback(); flash(str(e)[:250]); return None


def _es_abogada():
    return g.usuario.rol == "Abogado"


def secciones_a_texto(secciones):
    """Editor de texto simple: «## Título» abre una sección; los párrafos se separan con una línea en blanco."""
    return "\n\n".join(f"## {s['titulo']}\n\n" + "\n\n".join(s["parrafos"]) for s in secciones)


def texto_a_secciones(texto):
    secs, actual = [], None
    for bloque in re.split(r"\n\s*\n", (texto or "").replace("\r\n", "\n").strip()):
        b = bloque.strip()
        if not b: continue
        if b.startswith("## "):
            titulo, _, resto = b[3:].partition("\n")
            actual = {"titulo": titulo.strip(), "parrafos": []}; secs.append(actual)
            if resto.strip(): actual["parrafos"].append(" ".join(resto.split()))
        elif actual is not None:
            actual["parrafos"].append(" ".join(b.split()))
    if not secs: raise ValueError("Use «## Título» para abrir cada sección")
    return secs


# ====================================================================== lista de documentos
@ruta("/plataforma/legal", "legal", ("GET", "POST"))
def maestro_legal():
    if request.method == "POST" and F.get("accion") == "abogada":
        if g.usuario.rol != "Maestro": abort(403)
        def registrar_abogada():
            from .auth.routes import enviar_invitacion
            from .security import hashear
            usuario, correo = (F.get("username") or "").strip().lower(), (F.get("email") or "").strip().lower()
            if not re.fullmatch(r"[a-z0-9._-]{3,40}", usuario): raise ValueError("Usuario: 3 a 40 letras, números, punto o guion")
            if "@" not in correo: raise ValueError("Correo inválido")
            u = db.session.execute(select(Usuario).where((Usuario.username == usuario) | (Usuario.email == correo))).scalar_one_or_none()
            if u is not None and u.rol != "Abogado": raise ValueError("Ese usuario o correo ya pertenece a otra cuenta")
            _admin().legal_guardar_abogado(usuario, nombre=F.get("nombre"), cedula=F.get("cedula"), inpreabogado=F.get("inpreabogado"), correo=correo)
            nuevo = u is None
            if nuevo:
                u = Usuario(username=usuario, email=correo, email_verificado=False, rol="Abogado", organizacion_id=None,
                            password_hash=hashear(secrets.token_urlsafe(32)), debe_cambiar_clave=False)
                db.session.add(u); db.session.flush()
                enviar_invitacion(u, "Gigadevs Corp (área legal)", g.usuario.username)
            registrar(g.usuario.username, "ABOGADA_REGISTRADA", f"{usuario} Inpreabogado {F.get('inpreabogado')}", org=None)
            return nuevo
        _hacer(registrar_abogada, lambda nuevo: "Abogada registrada" + (": recibirá la invitación para activar su cuenta." if nuevo else "."))
        return redirect(url_for("web.maestro_legal"))
    from .licensing.admin import ErrorAdmin
    sel = request.args.get("sistema") if request.args.get("sistema") in sistemas.SISTEMAS else None
    try:
        adm = _admin(); datos = adm.legal_documentos(sel); abgs = adm.legal_abogados(); acs = adm.legal_aceptaciones()
    except (ErrorAdmin, ValueError, OSError) as e:
        flash(f"No se pudo consultar el servicio de licencias: {str(e)[:150]}"); datos, abgs, acs = {"documentos": [], "cadena_integra": True}, [], []
    docs = datos["documentos"]
    filas = [dict(cells=[_nombre_sistema(d["sistema"]), TIPOS.get(d["tipo"], d["tipo"]), f"v{d['version']}", ESTADOS.get(d["estado"], d["estado"]),
                         _fecha(d["actualizado_en"]), _fecha(d["aprobado_en"]) if d.get("aprobado_en") else "—"],
                  url=url_for("web.maestro_legal_documento", did=d["id"]), clase=f"producto-{d['sistema']}" if d["estado"] == "aprobado" else "")
             for d in docs if d["estado"] != "reemplazado" or sel]
    en_revision = [d for d in docs if d["estado"] == "revision"]
    secs = [dict(kpis=[("Vigentes", sum(d["estado"] == "aprobado" for d in docs)), ("En revisión", len(en_revision)),
                       ("Borradores", sum(d["estado"] == "borrador" for d in docs)), ("Aceptaciones registradas", len(acs))]),
            dict(titulo="Cómo funciona", clase="maestro-contexto",
                 nota="Cada documento nace como borrador desde la plantilla. Se edita, se envía a revisión y solo rige cuando la abogada lo aprueba con su "
                      "clave y su código de verificación. Desde ese momento se pide aceptarlo al próximo ingreso: el administrador de cada cliente "
                      "acepta el contrato, los términos y la privacidad; los demás usuarios, los términos y la privacidad. Cada aceptación queda "
                      "registrada con la versión exacta, la hora, la IP y la huella del texto. Marco legal: docs/MARCO_LEGAL.md."
                      + ("" if datos.get("cadena_integra", True) else " ⚠ La cadena de aceptaciones NO está íntegra: revise la bitácora.")),
            dict(titulo="Documentos" + (f" de {_nombre_sistema(sel)}" if sel else " de todos los sistemas"),
                 nota="Filtrar: " + " · ".join(_nombre_sistema(s) for s in sistemas.SISTEMAS) + " (use ?sistema=… en la dirección).",
                 tabla=dict(cols=["Sistema", "Documento", "Versión", "Estado", "Actualizado", "Aprobado"], filas=filas, ancha=True))]
    if _es_abogada() and en_revision:
        secs.insert(1, dict(titulo=f"{len(en_revision)} documento(s) esperan su aprobación", clase="maestro-hero",
                            nota="Ábralos desde la tabla para revisarlos, devolverlos con observaciones o aprobarlos."))
    if g.usuario.rol == "Maestro":
        secs.append(dict(titulo="Abogada autorizada", tabla=dict(cols=["Nombre", "Cédula", "Inpreabogado", "Usuario", "Firma", "Estado"], filas=[
            dict(cells=[a["nombre"], a["cedula"], a["inpreabogado"], a["usuario"], "Registrada" if a["tiene_firma"] else "Pendiente",
                        "Activa" if a["activo"] else "Inactiva"]) for a in abgs])))
        secs.append(dict(titulo="Registrar abogada", clase="maestro-form", abierto=not abgs,
                         nota="Se crea su usuario de Gigadevs (rol Abogado, sin acceso a datos de clientes) y recibe la invitación para crear su clave "
                              "y su verificación en dos pasos. Al entrar registra su firma.",
                         form=dict(action=request.path, boton="Registrar y enviar invitación", campos=[h("abogada"),
                             campo("nombre", "Nombre completo"), campo("cedula", "Cédula", ph="V-12345678"),
                             campo("inpreabogado", "N° de Inpreabogado", ph="123456"), campo("username", "Usuario"),
                             campo("email", "Correo", "email")])))
    return pagina("Legal · Documentos", None, secs, acciones=[dict(url=url_for("web.maestro_legal_aceptaciones"), texto="Aceptaciones")])


# ====================================================================== un documento
@ruta("/plataforma/legal/<int:did>", "legal", ("GET", "POST"))
def maestro_legal_documento(did):
    from .licensing.admin import ErrorAdmin
    if request.method == "POST":
        a, autor = F.get("accion"), g.usuario.username
        if a == "guardar":
            nuevo = _hacer(lambda: _admin().legal_guardar(did, texto_a_secciones(F.get("texto")), autor, F.get("observaciones")),
                           lambda n: "Cambios guardados" + (" como una versión nueva (la aprobada sigue vigente hasta aprobar esta)." if n != did else "."))
            if nuevo: return redirect(url_for("web.maestro_legal_documento", did=nuevo))
        elif a == "revision":
            _hacer(lambda: _admin().legal_revision(did), "Enviado a revisión de la abogada")
        elif a == "devolver":
            if not _es_abogada(): abort(403)
            _hacer(lambda: _admin().legal_devolver(did, g.usuario.username, F.get("observaciones")), "Devuelto con observaciones")
        elif a == "aprobar":
            if not _es_abogada(): abort(403)
            def aprobar():
                from .auth.routes import _mfa_confirmado
                from .security import verificar_clave
                if not verificar_clave(F.get("clave", ""), g.usuario.password_hash)[0]: raise ValueError("Clave incorrecta: el documento no se aprobó")
                if not _mfa_confirmado(g.usuario, F.get("codigo")): raise ValueError("Código de verificación incorrecto: el documento no se aprobó")
                doc = _admin().legal_aprobar(did, g.usuario.username)
                registrar(g.usuario.username, "DOCUMENTO_LEGAL_APROBADO", f"{doc['sistema']}/{doc['tipo']} v{doc['version']} huella={doc['huella'][:16]}", org=None)
                try:
                    from . import drive
                    drive.subir_documento_aprobado(doc)
                except Exception as e:  # noqa: BLE001 — Drive no detiene la aprobación; se reintenta desde Legal
                    current_app.logger.warning("Drive: no se subió el documento %s: %s", did, e)
                return doc
            _hacer(aprobar, lambda d: f"Aprobado: {d['titulo']} v{d['version']} rige desde ahora.")
        else: flash("Acción inválida")
        return redirect(url_for("web.maestro_legal_documento", did=did))
    try:
        doc = _admin().legal_documento(did)
    except (ErrorAdmin, ValueError, OSError) as e:
        flash(str(e)[:200]); return redirect(url_for("web.maestro_legal"))
    editable = doc["estado"] in ("borrador", "revision", "aprobado")
    secs = [dict(titulo=f"{doc['titulo']} · versión {doc['version']}", clase="maestro-hero",
                 nota=f"{ESTADOS.get(doc['estado'], doc['estado'])} · huella SHA-256 {doc['huella'][:24]}…"
                      + (f" · aprobado el {_fecha(doc['aprobado_en'])} por Abg. {doc['abogado']['nombre']} (Inpreabogado {doc['abogado']['inpreabogado']})"
                         if doc.get("abogado") else "")
                      + (f" · Observaciones: {doc['observaciones']}" if doc.get("observaciones") else ""))]
    secs += [dict(titulo=f"{i}. {s['titulo']}", nota=" ".join(s["parrafos"])) for i, s in enumerate(doc["secciones"], 1)]
    if editable:
        secs.append(dict(titulo="Editar" + (" (crea la versión siguiente)" if doc["estado"] == "aprobado" else ""), clase="maestro-form",
                         nota="«## Título» abre cada sección; separe los párrafos con una línea en blanco.",
                         form=dict(action=request.path, boton="Guardar cambios", campos=[h("guardar"),
                             campo("texto", "Texto del documento", "textarea", valor=secciones_a_texto(doc["secciones"]), filas=28, max=120000),
                             campo("observaciones", "Observaciones", "textarea", req=False, valor=doc.get("observaciones"))])))
    if doc["estado"] == "borrador":
        secs.append(dict(titulo="Enviar a revisión", form=dict(action=request.path, boton="Enviar a la abogada", campos=[h("revision")])))
    if doc["estado"] == "revision" and _es_abogada():
        secs += [dict(titulo="Aprobar este documento", clase="maestro-form",
                      nota="Al aprobar, el documento rige desde ahora con su nombre, cédula, Inpreabogado y firma, y se pide aceptarlo al próximo ingreso. "
                           "Confirme con su clave y su código de verificación.",
                      form=dict(action=request.path, boton="Aprobar con mi firma", confirmar="¿Aprobar y firmar este documento?", campos=[h("aprobar"),
                          campo("clave", "Su clave", "password"), campo("codigo", "Código de verificación", ph="123456")])),
                 dict(titulo="Devolver con observaciones", form=dict(action=request.path, boton="Devolver", campos=[h("devolver"),
                      campo("observaciones", "Qué hay que corregir", "textarea")]))]
    return pagina(f"Legal · {TIPOS.get(doc['tipo'], doc['tipo'])} de {_nombre_sistema(doc['sistema'])}", None, secs,
                  acciones=[dict(url=url_for("web.maestro_legal_imprimir", did=did), texto="Ver documento firmado / imprimir", primaria=True),
                            dict(url=url_for("web.maestro_legal", sistema=doc["sistema"]), texto="← Documentos")])


def documento_html(doc, base_url=None):
    """Documento listo para imprimir o guardar en PDF: marca del sistema, texto, huella y bloque de aprobación con la firma."""
    from markupsafe import escape as e
    marca = pc.MARCAS.get(doc["sistema"], pc.MARCAS["contax"])
    base = (base_url or current_app.config.get("APP_BASE_URL") or "").rstrip("/")
    cuerpo = "".join(f'<h2>{i}. {e(s["titulo"])}</h2>' + "".join(f"<p>{e(p)}</p>" for p in s["parrafos"]) for i, s in enumerate(doc["secciones"], 1))
    ab = doc.get("abogado")
    aprobacion = (f'<div class="aprob"><div><b>Aprobado por</b><br>Abg. {e(ab["nombre"])}<br>C.I. {e(ab["cedula"])} · Inpreabogado N° {e(ab["inpreabogado"])}'
                  f'<br>{e(time.strftime("%d/%m/%Y %H:%M UTC", time.gmtime(doc["aprobado_en"])))}</div>'
                  + (f'<img alt="Firma" src="data:image/png;base64,{e(ab["firma"])}">' if ab.get("firma") else "") + "</div>"
                  if ab and doc["estado"] in ("aprobado", "reemplazado") else '<div class="aprob borrador">BORRADOR — sin aprobación de la abogada. No rige.</div>')
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(doc['titulo'])} · v{doc['version']}</title><style>
body{{margin:0;background:#f4f4f2;font-family:'Inter','Segoe UI',Helvetica,Arial,sans-serif;color:#303030}}
.hoja{{max-width:820px;margin:24px auto;background:#fff;border:1px solid #e6e6e3;border-radius:14px;overflow:hidden}}
.cab{{display:flex;align-items:center;gap:12px;padding:18px 26px;background:#191919;color:#fff;border-bottom:4px solid {marca[2]}}}
.cab img{{width:38px;height:38px;border-radius:9px}}.cab b{{font-size:20px}}.cab small{{display:block;color:#bbb}}
.cuerpo{{padding:26px 34px}}h1{{font-size:21px;margin:0 0 4px;color:#191919}}.meta{{color:#707070;font-size:12px;margin-bottom:18px;word-break:break-all}}
h2{{font-size:14px;margin:20px 0 6px;color:#191919}}p{{font-size:13px;line-height:1.65;text-align:justify;margin:0 0 8px}}
.aprob{{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-top:26px;padding:16px;border:2px solid #188038;border-radius:12px;background:#eef9e6;font-size:13px}}
.aprob img{{max-height:80px;max-width:240px}}.aprob.borrador{{border-color:#c8372d;background:#fdecea;color:#c8372d;font-weight:700;justify-content:center}}
@media print{{body{{background:#fff}}.hoja{{border:0;margin:0}}}}</style></head><body><div class="hoja">
<div class="cab"><img src="{e(base)}/static/marca/{marca[1]}" alt=""><div><b>{e(marca[0])}</b><small>Gigadevs Corp · documento legal</small></div></div>
<div class="cuerpo"><h1>{e(doc['titulo'])}</h1><div class="meta">Versión {doc['version']} · Huella SHA-256 del texto: {e(doc['huella'])}</div>
{cuerpo}{aprobacion}</div></div></body></html>"""


@ruta("/plataforma/legal/<int:did>/documento", "legal")
def maestro_legal_imprimir(did):
    from .licensing.admin import ErrorAdmin
    try:
        doc = _admin().legal_documento(did)
    except (ErrorAdmin, ValueError, OSError):
        abort(404)
    g.csp_estilos_en_linea = True
    return Response(documento_html(doc), mimetype="text/html")


# ====================================================================== abogada: datos y firma
@ruta("/plataforma/legal/abogada", "legal", ("GET", "POST"))
def maestro_legal_abogada():
    from .licensing.admin import ErrorAdmin
    if request.method == "POST":
        if not _es_abogada(): abort(403)
        a = F.get("accion")
        if a == "datos":
            _hacer(lambda: _admin().legal_guardar_abogado(g.usuario.username, nombre=F.get("nombre"), cedula=F.get("cedula"),
                                                          inpreabogado=F.get("inpreabogado"), correo=g.usuario.email), "Datos actualizados")
        elif a == "firma":
            def firma():
                arch = request.files.get("firma")
                datos = arch.read(300 * 1024 + 1) if arch else b""
                _admin().legal_firma(g.usuario.username, base64.b64encode(datos).decode())
                registrar(g.usuario.username, "ABOGADA_FIRMA_REGISTRADA", "", org=None)
            _hacer(firma, "Firma registrada")
        return redirect(request.path)
    try:
        abgs = _admin().legal_abogados()
    except (ErrorAdmin, ValueError, OSError) as e:
        flash(str(e)[:200]); abgs = []
    if not _es_abogada():
        return pagina("Legal · Abogada", None, [dict(tabla=dict(cols=["Nombre", "Cédula", "Inpreabogado", "Usuario", "Firma"], filas=[
            dict(cells=[a["nombre"], a["cedula"], a["inpreabogado"], a["usuario"], "Registrada" if a["tiene_firma"] else "Pendiente"]) for a in abgs])),
            dict(nota="La abogada registra su firma desde su propio usuario. Para registrar una abogada, use Legal → Documentos.")])
    yo = next((a for a in abgs if a["usuario"] == g.usuario.username), None)
    return pagina("Mis datos y firma", None, [
        dict(kpis=[("Inpreabogado", yo["inpreabogado"] if yo else "—"), ("Firma", "Registrada" if yo and yo["tiene_firma"] else "Pendiente")]),
        dict(titulo="Mis datos profesionales", form=dict(action=request.path, boton="Guardar", campos=[h("datos"),
             campo("nombre", "Nombre completo", valor=yo and yo["nombre"]), campo("cedula", "Cédula", valor=yo and yo["cedula"]),
             campo("inpreabogado", "N° de Inpreabogado", valor=yo and yo["inpreabogado"])])),
        dict(titulo="Mi firma", nota="Imagen PNG de su firma sobre fondo blanco o transparente (máx. 300 KB). Se imprime en cada documento que apruebe; "
                                     "la aprobación se confirma además con su clave y su código de verificación.",
             form=dict(action=request.path, boton="Guardar firma", archivo=True, campos=[h("firma"), campo("firma", "Firma (PNG)", "file", accept="image/png")]))])


# ====================================================================== registro de aceptaciones
@ruta("/plataforma/legal/aceptaciones", "legal")
def maestro_legal_aceptaciones():
    from .licensing.admin import ErrorAdmin
    sel = request.args.get("sistema") if request.args.get("sistema") in sistemas.SISTEMAS else None
    try:
        acs = _admin().legal_aceptaciones(sel); integra = _admin().legal_documentos()["cadena_integra"]
    except (ErrorAdmin, ValueError, OSError) as e:
        flash(str(e)[:200]); acs, integra = [], True
    return pagina("Legal · Aceptaciones", None, [
        dict(kpis=[("Aceptaciones", len(acs)), ("Personas", len({a["usuario"] for a in acs})), ("Cadena de evidencia", "Íntegra" if integra else "ROTA")]),
        dict(titulo="Registro de aceptaciones", nota="Solo inserción y encadenado por SHA-256: alterar o borrar un registro rompe la cadena.",
             tabla=dict(cols=["Fecha (UTC)", "Sistema", "Documento", "Versión", "Quién", "Organización / cuenta", "Rol", "IP", "Huella"], ancha=True, filas=[
                 dict(cells=[time.strftime("%Y-%m-%d %H:%M", time.gmtime(a["aceptado_en"])), _nombre_sistema(a["sistema"]), TIPOS.get(a["tipo"], a["tipo"]),
                             f"v{a['version']}", f"{a.get('nombre') or ''} ({a['usuario']})", f"{a.get('organizacion') or ''} {a.get('sujeto') or ''}".strip() or "—",
                             a.get("rol") or "—", a.get("ip") or "—", a["huella"][:12] + "…"]) for a in acs]))])


# ====================================================================== aceptación al ingresar (usuarios de los clientes de Contax)
REVISAR_CADA_S = 900       # tras aceptar (o sin pendientes) no se vuelve a consultar durante 15 minutos
LIBRES = {"web.legal_aceptar", "web.legal", "auth.logout", "static", "salud"}


def _perfil(u):
    return "responsable" if u.rol == "Administrador" else "usuario"


def _pendientes(u):
    """Documentos vigentes de Contax que esta persona aún no aceptó, con su texto completo."""
    adm = _admin()
    pend = adm.legal_pendientes("contax", u.username, _perfil(u))
    return [{**p, "documento": adm.legal_documento(p["id"])} for p in pend]


def compuerta():
    """before_request: un usuario de cliente con documentos vigentes sin aceptar solo puede verlos y aceptarlos (o salir)."""
    from flask import session
    from .licensing.admin import ErrorAdmin
    u = getattr(g, "usuario", None)
    if u is None or u.organizacion_id is None or request.endpoint is None: return None
    from . import RUTAS_LIBRES
    if request.endpoint in LIBRES or (request.endpoint in RUTAS_LIBRES and request.endpoint != "web.admin_licencia"): return None
    if session.get("legal_ok_hasta", 0) > time.time() and session.get("legal_ok_usuario") == u.id: return None
    if current_app.extensions.get("admin_licencias") is None: return None          # sin servicio de licencias configurado (desarrollo)
    try:
        pend = _admin().legal_pendientes("contax", u.username, _perfil(u))
    except (ErrorAdmin, ValueError, OSError) as e:                                # el servicio no responde: no se corta el trabajo
        current_app.logger.warning("Legal: no se pudo consultar documentos pendientes: %s", e)
        session["legal_ok_hasta"], session["legal_ok_usuario"] = time.time() + 300, u.id
        return None
    if pend: return redirect(url_for("web.legal_aceptar"))
    session["legal_ok_hasta"], session["legal_ok_usuario"] = time.time() + REVISAR_CADA_S, u.id
    return None


from .web import bp  # noqa: E402


@bp.route("/legal/aceptar", methods=["GET", "POST"])
def legal_aceptar():
    from flask import session
    from .licensing.admin import ErrorAdmin
    from .models import Organizacion
    u = getattr(g, "usuario", None)
    if u is None or u.organizacion_id is None: return redirect("/")
    try:
        pend = _pendientes(u)
    except (ErrorAdmin, ValueError, OSError) as e:
        flash(f"No pudimos cargar los documentos legales: {str(e)[:120]}"); return redirect("/")
    if not pend:
        session["legal_ok_hasta"], session["legal_ok_usuario"] = time.time() + REVISAR_CADA_S, u.id
        return redirect("/")
    if request.method == "POST":
        marcados = {x for x in request.form.getlist("acepto")}
        if marcados != {str(p["id"]) for p in pend}:
            flash("Marque la casilla de cada documento para aceptarlo.")
        else:
            org = db.session.get(Organizacion, u.organizacion_id)
            ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0].strip()
            try:
                _admin().legal_aceptar("contax", documentos=[p["id"] for p in pend], usuario=u.username, origen="contax", nombre=u.username,
                                       sujeto=org.rif if org else None, organizacion=org.nombre if org else None, rol=u.rol, ip=ip,
                                       agente=(request.user_agent.string or "")[:300])
            except (ErrorAdmin, ValueError, OSError) as e:
                flash(f"No se pudo registrar la aceptación: {str(e)[:150]}"); return redirect(request.path)
            registrar(u.username, "DOCUMENTOS_LEGALES_ACEPTADOS", ", ".join(f"{p['tipo']} v{p['version']}" for p in pend))
            db.session.commit()
            enviar_copia(u, org, [p["documento"] for p in pend])
            session["legal_ok_hasta"], session["legal_ok_usuario"] = time.time() + REVISAR_CADA_S, u.id
            flash("Gracias. Su aceptación quedó registrada" + (" y le enviamos una copia del contrato a su correo." if u.email else "."))
            return redirect("/")
    return render_template("legal_aceptar.html", titulo="Documentos legales", empresa=None, pendientes=pend, nombre_sistema="Contax",
                           hay_contrato=any(p["tipo"] == "contrato" for p in pend))


def enviar_copia(u, org, docs):
    """Copia de los documentos aceptados al correo de quien aceptó (el contrato completo) y, si Drive está conectado, al Drive de Gigadevs."""
    from . import correo
    contrato = next((d for d in docs if d["tipo"] == "contrato"), None)
    if u.email and contrato:
        try:
            correo.enviar(u.email, f"Copia de su contrato · {contrato['titulo']}",
                          f"Estimado(a) {u.username}:\n\nGracias por aceptar el {contrato['titulo']} (versión {contrato['version']}). Le enviamos una copia para "
                          "sus registros. Su aceptación quedó registrada con la fecha y hora, su dirección IP y la huella digital del texto.",
                          titulo="Copia de su contrato", filas=[("Organización", org.nombre if org else "—"), ("Documento", contrato["titulo"]),
                                                               ("Versión", str(contrato["version"])), ("Huella SHA-256", contrato["huella"][:32] + "…")],
                          html=documento_html(contrato))
        except Exception as e:  # noqa: BLE001 — la aceptación ya quedó registrada
            current_app.logger.warning("Legal: no se envió la copia del contrato a %s: %s", u.email, e)
    try:
        from . import drive
        for d in docs: drive.subir_aceptacion(d, u.username, org.rif if org else "", org.nombre if org else "")
    except Exception as e:  # noqa: BLE001
        current_app.logger.warning("Drive: no se subió la aceptación: %s", e)
