"""Cara pública de Contax: buscadores (robots, sitemap, verificación), protección perimetral y páginas de error.

- Buscadores: solo la portada y las páginas legales se indexan; todo lo demás lleva «X-Robots-Tag: noindex».
- Protección: los rastreos automáticos (.env, .git, wp-admin, *.php…) se cortan en la capa WSGI con un 404 inmediato,
  sin tocar sesión, base de datos ni plantillas. Modo mantenimiento por variable de entorno (503 con Retry-After).
- Errores: páginas propias de la marca que NO dependen del menú ni de la base de datos; la 500 entrega un código de
  referencia para soporte y nunca muestra trazas ni SQL."""
import re
import secrets
from datetime import date, timedelta

from flask import Response, g, redirect, render_template, request, send_from_directory, url_for

# Rutas que solo pide un escáner de vulnerabilidades: Contax no usa PHP, WordPress ni archivos ocultos.
RASTREOS = re.compile(
    r"(^|/)\.(env|git|svn|hg|aws|ssh|htaccess|htpasswd|ds_store|idea|vscode|docker)"
    r"|wp-(admin|login|content|includes|json|config)|xmlrpc|phpmyadmin|pma/|myadmin|adminer|cgi-bin|/vendor/|/actuator"
    r"|server-status|/telescope|/solr|/boaform|/hnap1|/owa/|/autodiscover|\.(php\d?|asp|aspx|jsp|cgi|pl|sql|bak|old|swp|tar|gz|zip|7z|rar|ini|yml|yaml|log)$",
    re.IGNORECASE)

INDEXABLES = {"web.legal", "robots", "sitemap", "security_txt", "favicon", "static"}
PRIVADAS = ("/e/", "/plataforma", "/admin", "/login", "/logout", "/mfa", "/cuenta", "/clave", "/invitacion", "/recuperar", "/correo",
            "/contrato/", "/oc/", "/ov/", "/pv/", "/wa/", "/bienvenida", "/salud")

ERRORES = {
    400: ("Solicitud no válida", "La petición llegó incompleta o con datos que no se pueden procesar.", "Vuelva a la página anterior e intente de nuevo."),
    401: ("Sesión requerida", "Para ver esta página debe iniciar sesión.", "Ingrese con su usuario y su código de verificación."),
    403: ("Acceso denegado", "Su rol o el plan de su licencia no incluye esta función.", "Si cree que debería tener acceso, consulte al administrador de su empresa."),
    404: ("Página no encontrada", "La dirección no existe, cambió o no pertenece a su organización.", "Revise el enlace o vuelva al inicio."),
    405: ("Acción no permitida", "Esta dirección no acepta ese tipo de solicitud.", "Use los botones y formularios de Contax en lugar de escribir la dirección."),
    413: ("Archivo demasiado grande", "El archivo o formulario supera el máximo de 5 MB por envío.", "Reduzca el archivo (o divídalo) e intente de nuevo."),
    429: ("Demasiadas solicitudes", "Detectamos muchas peticiones seguidas desde su conexión y las frenamos por seguridad.", "Espere unos minutos e intente de nuevo."),
    500: ("Algo salió mal", "Ocurrió un error inesperado. Ningún dato se guardó a medias: la operación se revirtió.", "Intente de nuevo; si se repite, escríbanos con el código de referencia."),
    503: ("Estamos en mantenimiento", "Contax se está actualizando para servirle mejor.", "Vuelva en unos minutos: sus datos están seguros."),
}

HTML_MINIMO = ("<!doctype html><html lang=\"es\"><head><meta charset=\"utf-8\"><title>{c} · Contax</title>"
               "<meta name=\"robots\" content=\"noindex\"></head><body style=\"font-family:system-ui;background:#191919;color:#fff;"
               "display:grid;place-items:center;min-height:100vh;margin:0\"><main><h1>{c} · {t}</h1><p>{m}</p>{r}<p><a style=\"color:#87e64b\" href=\"/\">"
               "Ir al inicio</a></p></main></body></html>")


class FiltroRastreos:
    """Middleware WSGI: responde 404 a rutas de escáneres antes de que Flask abra sesión o conexión a la base."""
    def __init__(self, app_wsgi):
        self.app_wsgi = app_wsgi

    def __call__(self, environ, start_response):
        ruta = environ.get("PATH_INFO", "") or ""
        if (RASTREOS.search(ruta) and not ruta.startswith("/.well-known/")) or "\x00" in ruta or len(ruta) > 2048:
            start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8"), ("Cache-Control", "no-store"),
                                             ("X-Content-Type-Options", "nosniff"), ("X-Robots-Tag", "noindex, nofollow")])
            return [b"No encontrado"]
        return self.app_wsgi(environ, start_response)


def pagina_error(codigo, titulo=None, mensaje=None, consejo=None, referencia=None):
    t, m, c = ERRORES.get(codigo, ERRORES[500])
    titulo, mensaje, consejo = titulo or t, mensaje or m, consejo if consejo is not None else c
    try:
        cuerpo = render_template("error.html", codigo=codigo, titulo=titulo, mensaje=mensaje, consejo=consejo, referencia=referencia,
                                 autenticado=getattr(g, "usuario", None) is not None)
    except Exception:  # noqa: BLE001 — si hasta la plantilla falla, se responde HTML mínimo (nunca una traza)
        cuerpo = HTML_MINIMO.format(c=codigo, t=titulo, m=mensaje, r=f"<p>Referencia: <code>{referencia}</code></p>" if referencia else "")
    resp = Response(cuerpo, status=codigo, mimetype="text/html")
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    if codigo == 503: resp.headers["Retry-After"] = "300"
    return resp


def instalar(app, db):
    app.wsgi_app = FiltroRastreos(app.wsgi_app)

    # ------------------------------------------------------------------ mantenimiento
    @app.before_request
    def mantenimiento():
        if app.config.get("MANTENIMIENTO") and request.endpoint not in ("salud", "static", "robots", "favicon"):
            return pagina_error(503)
        return None
    app.before_request_funcs[None].insert(0, app.before_request_funcs[None].pop())  # antes que sesión y licencia

    # ------------------------------------------------------------------ buscadores
    def base():
        return app.config["APP_BASE_URL"].rstrip("/")

    @app.get("/robots.txt")
    def robots():
        lineas = ["User-agent: *", "Allow: /$", "Allow: /legal", "Allow: /static/"] + [f"Disallow: {p}" for p in PRIVADAS] + \
                 ["", "# Contax® es una marca registrada de Gigadevs Corp", f"Sitemap: {base()}/sitemap.xml", ""]
        if app.config.get("MANTENIMIENTO") or not app.config.get("PRODUCCION"):
            lineas = ["User-agent: *", "Disallow: /", ""]  # entornos de prueba o en mantenimiento no se indexan
        r = Response("\n".join(lineas), mimetype="text/plain"); r.headers["Cache-Control"] = "public, max-age=3600"
        return r

    @app.get("/sitemap.xml")
    def sitemap():
        from .legal import PAGINAS
        hoy = app.config.get("SITEMAP_FECHA") or date.today().isoformat()
        urls = [(f"{base()}/", "weekly", "1.0")] + [(f"{base()}/legal/{p}", "yearly", "0.3") for p in PAGINAS]
        xml = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        xml += [f"  <url><loc>{u}</loc><lastmod>{hoy}</lastmod><changefreq>{f}</changefreq><priority>{p}</priority></url>" for u, f, p in urls]
        r = Response("\n".join(xml + ["</urlset>", ""]), mimetype="application/xml"); r.headers["Cache-Control"] = "public, max-age=3600"
        return r

    @app.get("/.well-known/security.txt")
    def security_txt():  # RFC 9116: a dónde reportar una vulnerabilidad
        vence = (date.today() + timedelta(days=180)).isoformat() + "T00:00:00.000Z"
        txt = (f"Contact: {app.config['CONTACTO_SEGURIDAD']}\nExpires: {vence}\nPreferred-Languages: es, en\n"
               f"Canonical: {base()}/.well-known/security.txt\nPolicy: {base()}/legal/terminos\n")
        return Response(txt, mimetype="text/plain")

    @app.get("/favicon.ico")
    def favicon():
        r = send_from_directory(app.static_folder, "marca/favicon.ico", max_age=86400)
        return r

    @app.context_processor
    def seo():
        return {"BASE_URL": base(), "GOOGLE_VERIFICACION": app.config.get("GOOGLE_SITE_VERIFICATION", ""),
                "BING_VERIFICACION": app.config.get("BING_SITE_VERIFICATION", "")}

    @app.after_request
    def no_indexar(resp):
        indexable = request.endpoint in INDEXABLES or (request.endpoint == "web.inicio" and getattr(g, "usuario", None) is None)
        if not indexable or resp.status_code >= 400:
            resp.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
        return resp

    # ------------------------------------------------------------------ errores
    def manejador(codigo):
        def h(_e):
            if codigo == 401 and request.method == "GET":
                return redirect(url_for("auth.login"))
            return pagina_error(codigo)
        return h
    for c in (400, 401, 403, 404, 405, 413, 429, 503):
        app.register_error_handler(c, manejador(c))

    @app.errorhandler(500)
    def error_interno(e):
        ref = secrets.token_hex(4).upper()
        try: db.session.rollback()
        except Exception: pass  # noqa: BLE001,E701 — la base puede ser justamente lo que falló
        original = getattr(e, "original_exception", None) or e
        app.logger.error("Error 500 ref=%s %s %s", ref, request.method, request.path, exc_info=original)
        return pagina_error(500, referencia=ref)

    from flask_wtf.csrf import CSRFError

    @app.errorhandler(CSRFError)
    def csrf_vencido(_e):  # formulario vencido o enviado desde otro sitio: se explica en vez de un "Bad Request" crudo
        return pagina_error(400, "El formulario expiró", "Por seguridad, los formularios caducan tras un tiempo o si se envían desde otro sitio.",
                            "Vuelva a la página anterior, recárguela e intente de nuevo.")
