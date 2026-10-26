"""Despliegue del servidor de licencias:  gunicorn licensing_server.wsgi:app  (detrás de HTTPS; en Render: erp-licencias).

Variables: LICENSE_PRIVATE_KEY, LICENSE_ADMIN_SECRET, DATABASE_URL (o LICENSE_DB_URL), PUBLIC_URL, ERP_URL,
BREVO_API_KEY, MAIL_FROM, y opcionales BINANCE_PAY_API_KEY / BINANCE_PAY_SECRET (USDT) y PAGO_MOVIL_INSTRUCCIONES (Bs)."""
import base64
import os

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import brevo
from licensing_server.binance import BinancePay
from licensing_server.server import create_app

_b = os.environ["LICENSE_PRIVATE_KEY"]
_url = os.environ.get("LICENSE_DB_URL") or os.environ.get("DATABASE_URL") or "sqlite:///licencias.db"
_secreto = os.environ["LICENSE_ADMIN_SECRET"]
if len(_secreto) < 32:
    raise RuntimeError("LICENSE_ADMIN_SECRET debe tener al menos 32 caracteres")
_binance = BinancePay(os.environ["BINANCE_PAY_API_KEY"], os.environ["BINANCE_PAY_SECRET"]) if os.environ.get("BINANCE_PAY_API_KEY") else None


def _correo(destino, asunto, texto):
    brevo.enviar(os.environ.get("BREVO_API_KEY"), os.environ.get("MAIL_FROM", "Gigadevs Corp <aalejandro996@icloud.com>"), destino, asunto, texto,
                 etiquetas=("licencia",))


app = create_app(_url, Ed25519PrivateKey.from_private_bytes(base64.urlsafe_b64decode(_b + "=" * (-len(_b) % 4))),
                 secreto_admin=_secreto, binance=_binance, correo=_correo,
                 config={"url_publica": os.environ.get("PUBLIC_URL", "").rstrip("/"), "url_erp": os.environ.get("ERP_URL", "").rstrip("/"),
                         "pago_movil": os.environ.get("PAGO_MOVIL_INSTRUCCIONES", "")})

from werkzeug.middleware.proxy_fix import ProxyFix  # noqa: E402

app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)  # IP real del cliente detrás del proxy de Render
