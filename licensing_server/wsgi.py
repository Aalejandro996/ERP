"""Despliegue del servidor de licencias:  gunicorn licensing_server.wsgi:app  (detrás de HTTPS; en Render: erp-licencias).

Variables: LICENSE_PRIVATE_KEY, LICENSE_ADMIN_SECRET, DATABASE_URL (o LICENSE_DB_URL), PUBLIC_URL, ERP_URL, BREVO_API_KEY, MAIL_FROM.
USDT (opcional): BINANCE_API_KEY / BINANCE_API_SECRET (clave de SOLO LECTURA de la cuenta personal), BINANCE_PAY_ID y, si también
se aceptan depósitos por red, BINANCE_USDT_RED (p. ej. TRC20) y BINANCE_USDT_DIRECCION. Bs: PAGO_MOVIL_INSTRUCCIONES."""
import base64
import os

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import brevo
from licensing_server.binance import BinanceCuenta
from licensing_server.server import create_app

_b = os.environ["LICENSE_PRIVATE_KEY"]
_url = os.environ.get("LICENSE_DB_URL") or os.environ.get("DATABASE_URL") or "sqlite:///licencias.db"
_secreto = os.environ["LICENSE_ADMIN_SECRET"]
if len(_secreto) < 32:
    raise RuntimeError("LICENSE_ADMIN_SECRET debe tener al menos 32 caracteres")
# Cuenta PERSONAL de Binance, clave de SOLO LECTURA (sin trading, retiros ni transferencias)
_binance = BinanceCuenta(os.environ["BINANCE_API_KEY"], os.environ["BINANCE_API_SECRET"]) if os.environ.get("BINANCE_API_KEY") else None


def _correo(destino, asunto, texto, html=None):
    brevo.enviar(os.environ.get("BREVO_API_KEY"), os.environ.get("MAIL_FROM", "Gigadevs Corp <aalejandro996@icloud.com>"), destino, asunto, texto,
                 html=html, etiquetas=("licencia",))


app = create_app(_url, Ed25519PrivateKey.from_private_bytes(base64.urlsafe_b64decode(_b + "=" * (-len(_b) % 4))),
                 secreto_admin=_secreto, binance=_binance, correo=_correo,
                 config={"url_publica": os.environ.get("PUBLIC_URL", "").rstrip("/"), "url_erp": os.environ.get("ERP_URL", "").rstrip("/"),
                         "pago_movil": os.environ.get("PAGO_MOVIL_INSTRUCCIONES", ""), "binance_pay_id": os.environ.get("BINANCE_PAY_ID", "").strip(),
                         "usdt_red": os.environ.get("BINANCE_USDT_RED", "").strip(), "usdt_direccion": os.environ.get("BINANCE_USDT_DIRECCION", "").strip()})

from werkzeug.middleware.proxy_fix import ProxyFix  # noqa: E402

app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)  # IP real del cliente detrás del proxy de Render


def _revision_periodica(intervalo=int(os.environ.get("BINANCE_REVISION_S", "120"))):
    """Cada 2 min busca en el historial de Binance los pagos de las órdenes en USDT pendientes (una consulta para todas)."""
    import logging
    import threading
    import time as _t
    log = logging.getLogger("erp.licencias.binance")

    def ciclo():
        while True:
            _t.sleep(intervalo)
            try:
                with app.app_context():
                    n = app.extensions["revisar_pendientes"]()
                if n: log.warning("Órdenes en USDT entregadas automáticamente: %s", n)
            except Exception:  # noqa: BLE001 — nunca debe morir el hilo
                log.exception("Revisión de pagos en Binance falló; se reintenta")
    threading.Thread(target=ciclo, name="revision-binance", daemon=True).start()


if _binance is not None:
    _revision_periodica()
