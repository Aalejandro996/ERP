import os


def _req(nombre: str) -> str:
    v = os.environ.get(nombre)
    if not v:
        raise RuntimeError(f"Falta la variable de entorno obligatoria {nombre}")
    return v


def _url_bd(url: str) -> str:
    """Render entrega postgres://…; SQLAlchemy con psycopg 3 necesita postgresql+psycopg://…"""
    for pre in ("postgres://", "postgresql://"):
        if url.startswith(pre): return "postgresql+psycopg://" + url[len(pre):]
    return url


def _url_servicio(v: str) -> str:
    """Render (fromService hostport) entrega «host:puerto» de la red privada, sin esquema."""
    return v if "://" in v else f"http://{v}"


class Config:
    """Sin valores por defecto inseguros: si falta un secreto, la app NO arranca."""
    SECRET_KEY = _req("SECRET_KEY")
    SQLALCHEMY_DATABASE_URI = _url_bd(_req("DATABASE_URL"))
    DB_ROL_APLICACION = os.environ.get("DB_ROL_APLICACION", "")   # p. ej. erp_app: SET ROLE en cada conexión → aplica RLS
    FIELD_ENCRYPTION_KEY = _req("FIELD_ENCRYPTION_KEY")
    LICENSE_SERVER_URL = _url_servicio(_req("LICENSE_SERVER_URL"))
    LICENSE_PUBLIC_KEY = _req("LICENSE_PUBLIC_KEY")
    LICENSE_ADMIN_URL = _url_servicio(os.environ.get("LICENSE_ADMIN_URL") or os.environ["LICENSE_SERVER_URL"]) if os.environ.get("LICENSE_SERVER_URL") else None
    LICENSE_ADMIN_SECRET = os.environ.get("LICENSE_ADMIN_SECRET", "")
    PAGO_MOVIL_INSTRUCCIONES = os.environ.get("PAGO_MOVIL_INSTRUCCIONES", "")   # API del Maestro (HMAC); vacío = consola de licencias deshabilitada
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")

    # Correo (verificación, invitaciones, recuperación, alertas). APP_BASE_URL fija el dominio de los enlaces.
    APP_NOMBRE = os.environ.get("APP_NOMBRE", "ERP")
    APP_LEMA = os.environ.get("APP_LEMA", "Tu empresa en orden. Tu fisco al día.")
    APP_BASE_URL = _req("APP_BASE_URL")
    MAIL_BACKEND = os.environ.get("MAIL_BACKEND", "brevo")            # brevo | smtp | console (solo desarrollo)
    MAIL_FROM = os.environ.get("MAIL_FROM", "Gigadevs Corp <aalejandro996@icloud.com>")
    BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "")
    SMTP_HOST = os.environ.get("SMTP_HOST", "")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USER = os.environ.get("SMTP_USER", "")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
    SMTP_SSL = os.environ.get("SMTP_SSL", "0") == "1"
    PRODUCCION = os.environ.get("FLASK_ENV", "production") == "production"

    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_NAME = "__Host-erp"
    PERMANENT_SESSION_LIFETIME = 1800          # 30 min de inactividad
    SESION_ABSOLUTA_S = 8 * 3600               # y 8 h como máximo aunque haya actividad
    EQUIPO_COOKIE = "__Host-erp-equipo"        # identifica el equipo autorizado (límite de equipos de la licencia)
    MAX_CONTENT_LENGTH = 5 * 1024 * 1024       # 5 MB por petición
    WTF_CSRF_TIME_LIMIT = 3600
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}
