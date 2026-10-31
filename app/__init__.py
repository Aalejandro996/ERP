import secrets

from flask import Flask, flash, g, redirect, render_template, request, url_for

from .planes import MODULOS, modulos_de, nombre_plan

RUTAS_LIBRES = {"salud", "auth.login", "auth.logout", "auth.mfa_verificar", "auth.mfa_configurar", "auth.mfa_codigos", "auth.cambiar_clave",
                "auth.verificar_correo", "auth.correo_confirmar", "auth.invitacion", "auth.recuperar", "auth.recuperar_token",
                "auth.cuenta", "web.admin_licencia", "static", "web.legal", "robots", "sitemap", "security_txt", "favicon"}


class AlmacenLicenciaOrg:
    """Licencia de UNA organización, guardada en la BD (no en un JSON plano editable)."""
    def __init__(self, org_id):
        self.org_id = org_id

    def obtener(self):
        from .extensions import db
        from .models import LicenciaOrganizacion
        fila = db.session.get(LicenciaOrganizacion, self.org_id)
        return dict(fila.datos) if fila else None

    def guardar(self, datos):
        from .extensions import db
        from .models import LicenciaOrganizacion
        fila = db.session.get(LicenciaOrganizacion, self.org_id)
        if fila is None:
            fila = LicenciaOrganizacion(organizacion_id=self.org_id, datos={}); db.session.add(fila)
        fila.datos = datos; db.session.commit()


def create_app(config_obj=None):
    # Importaciones diferidas: los módulos de cálculo/licencias se pueden usar y probar sin cargar Flask-SQLAlchemy.
    from cryptography.fernet import Fernet
    from config import Config
    from . import rls
    from .extensions import csrf, db, limiter
    from .licensing.client import GestorLicencia, huella_instalacion
    from .licensing.token import clave_publica_desde_b64
    app = Flask(__name__)
    app.config.from_object(config_obj or Config)
    Fernet(app.config["FIELD_ENCRYPTION_KEY"])  # falla al arrancar si la clave es inválida
    if app.config.get("PRODUCCION") and app.config["MAIL_BACKEND"] not in ("smtp", "brevo"):
        raise RuntimeError("En producción MAIL_BACKEND debe ser 'brevo' o 'smtp' (verificación de correo y recuperación de claves)")
    db.init_app(app); csrf.init_app(app); limiter.init_app(app)
    rls.instalar()
    if app.config.get("DB_ROL_APLICACION"):
        with app.app_context():
            rls.rol_limitado(db.engine, app.config["DB_ROL_APLICACION"])

    from .licensing.admin import ClienteAdmin
    app.extensions["admin_licencias"] = (lambda: ClienteAdmin(app.config["LICENSE_ADMIN_URL"], app.config["LICENSE_ADMIN_SECRET"])) \
        if app.config.get("LICENSE_ADMIN_SECRET") else None

    from . import models, models2, models_fact, models_compras  # noqa: F401  (registra tablas)
    from .cifrado import CifradorCampos
    app.extensions["cifrador"] = CifradorCampos(app.config["FIELD_ENCRYPTION_KEY"])

    from .auth.routes import bp as auth_bp
    from .web import bp as web_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(web_bp)

    huella_cache = {}

    def huella():
        """Huella de ESTA instalación (todas las licencias de clientes quedan atadas a ella)."""
        from .models import LicenciaLocal
        if "h" not in huella_cache:
            fila = db.session.get(LicenciaLocal, 1)
            if fila is None:
                fila = LicenciaLocal(id=1, datos={}, instance_secret=secrets.token_hex(32)); db.session.add(fila); db.session.commit()
            try:  # requiere permiso sobre pg_control_system(); si no lo hay (p. ej. Render), se usa el OID de la base
                with db.engine.connect() as c:
                    sys_id = c.execute(db.text("SELECT system_identifier FROM pg_control_system()")).scalar()
            except Exception:  # noqa: BLE001
                sys_id = db.session.execute(db.text("SELECT current_database() || ':' || oid FROM pg_database WHERE datname = current_database()")).scalar()
            huella_cache["h"] = huella_instalacion(fila.instance_secret, str(sys_id))
        return huella_cache["h"]

    def gestor(org_id):
        return GestorLicencia(app.config["LICENSE_SERVER_URL"], clave_publica_desde_b64(app.config["LICENSE_PUBLIC_KEY"]),
                              AlmacenLicenciaOrg(org_id), huella())
    app.extensions["gestor_licencia"] = gestor

    @app.before_request
    def compuerta_licencia():
        """Falla cerrado: sin licencia válida la organización no opera; el plan firmado decide los módulos."""
        g.modulos, g.licencia = frozenset(), None
        u = getattr(g, "usuario", None)
        if u is None or u.organizacion_id is None or request.endpoint is None:
            return None  # anónimo (lo frenan las rutas) o Maestro (plataforma, sin datos de clientes)
        est = app.extensions["gestor_licencia"](u.organizacion_id).estado()
        if est.valida:
            g.licencia, g.modulos = est.claims, modulos_de(est.claims)
            return None
        if request.endpoint in RUTAS_LIBRES:
            return None
        if u.rol == "Administrador":
            flash(f"Licencia no válida: {est.motivo}")
            return redirect(url_for("web.admin_licencia"))
        return render_template("error.html", codigo=402, titulo="Licencia no válida",
                               mensaje="Su organización no tiene una licencia vigente. Contacte al administrador."), 402

    from .tiempo import a_hora_ve
    app.add_template_filter(a_hora_ve, "hora_ve")

    @app.get("/salud")
    def salud():  # comprobación de Render: aplicación y base de datos
        db.session.execute(db.text("SELECT 1"))
        return {"ok": True}

    @app.context_processor
    def contexto():
        lic = getattr(g, "licencia", None) or {}
        demo = None
        if lic.get("plan") == "DEMO":  # tiempo que le queda a la demo de 72 h
            import time
            s = max(int(lic.get("vence_licencia", lic.get("exp", 0))) - int(time.time()), 0)
            demo = f"quedan {s // 3600} h {(s % 3600) // 60} min" if s else "vencida"
        return {"MODULOS": MODULOS, "modulos": getattr(g, "modulos", frozenset()), "plan_nombre": nombre_plan(getattr(g, "licencia", None)),
                "demo_restante": demo}

    @app.after_request
    def cabeceras(resp):
        resp.headers["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; "
                                                   "base-uri 'self'; form-action 'self'" + ("; upgrade-insecure-requests" if app.config.get("PRODUCCION") else "")
                                                   + ("; style-src 'self' 'unsafe-inline'; script-src " + ("'self'" if getattr(g, "csp_scripts_propios", False) else "'none'")
                                                      if getattr(g, "csp_estilos_en_linea", False) else ""))
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        # same-origin: el Referer solo viaja dentro del ERP (lo exige la protección CSRF sobre HTTPS) y nunca a otros sitios
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        resp.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        if request.endpoint != "static":
            resp.headers["Cache-Control"] = "no-store"
        return resp

    from . import publico
    publico.instalar(app, db)  # robots, sitemap, filtro de rastreos, mantenimiento y páginas de error

    return app
