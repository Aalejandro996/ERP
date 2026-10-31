from flask import request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect

db = SQLAlchemy()
csrf = CSRFProtect()
# Límite general por IP para las páginas de la app (los formularios sensibles —login, MFA, recuperación— tienen los suyos,
# mucho más estrictos). 300/hora era tan bajo que lo agotaba el chequeo de salud de Render y el uso normal de un contador.
limiter = Limiter(key_func=get_remote_address, default_limits=["120 per minute", "2000 per hour"])

# Rutas que nunca cuentan para el límite: el chequeo de salud de Render (cada 5–10 s), los archivos estáticos que pide
# cada página y lo que leen los buscadores. Ninguna toca datos de clientes.
SIN_LIMITE = frozenset({"salud", "static", "robots", "sitemap", "security_txt", "favicon"})


@limiter.request_filter
def _sin_limite():
    return request.endpoint in SIN_LIMITE
