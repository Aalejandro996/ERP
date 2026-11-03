"""Ávila Pay (comercio.avilapay.com): cobro en BOLÍVARES con confirmación automática — Pago Móvil, débito inmediato,
crédito inmediato y tarjeta nacional. Checkout alojado por Ávila Pay: el servidor crea la sesión con su clave privada y
el cliente paga en la página de Ávila Pay; nosotros nunca vemos datos bancarios ni de tarjeta.

API (Portal Comercios → Gateway → Documentación, v1.0):
  POST /api/v1/checkout/sessions {amount (unidades, no céntimos), currency, description, reference, methods, customer,
       return_url, cancel_url, webhook_url, metadata}  → {id, session_key "cs_…", url, status, expires_at}
  GET  /api/v1/checkout/sessions/{session_key}         → {status PENDING|PROCESSING|PAID|FAILED|EXPIRED, amount, currency,
       payment_method, paid_at}   (público; se usa para confirmar el pago leyendo a Ávila Pay, no el aviso)
  GET  /api/v1/checkout/sessions?limit=1               → lista (con clave): sirve para probar la clave
  Aviso: POST con {event payment.completed|failed|expired, data{session_key, reference, amount, payment_reference…}};
         cabecera X-AvilaPay-Signature = HMAC-SHA256 hexadecimal del cuerpo crudo con el webhook_secret.

DÓLARES (Gateway PaaS, guía v2.0) — misma clave sk_…, otra base y montos en CENTAVOS:
  POST https://api.avilapay.com/api/v1/gateway/payments {amount (centavos), currency "USD", orderId (idempotencia),
       customerEmail, description, returnUrl, cancelUrl, paymentMethods ["ZELLE"|"TARJETA_INTERNACIONAL"]}
       → {id (UUID), checkoutUrl, status PENDING}
  GET  …/gateway/payments/{id} → {status PENDING|SUCCEEDED|FAILED|REFUNDED, amount (centavos), currency, paymentMethod}
  Aviso: la URL se registra en el portal (Centro de Integración → Webhooks); cuerpo {payment_id, external_order_id, amount,
         status…}; cabecera «AvilaPay-Signature» = HMAC-SHA256 hexadecimal del cuerpo crudo con su webhook_secret.

La clave (sk_live_… / sk_test_…) y los secretos de avisos los carga el Maestro en su consola; se guardan cifrados.
"""
import hashlib
import hmac
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation

BASE = "https://comercio.avilapay.com"
BASE_API = "https://api.avilapay.com"
# Apple Pay / Google Pay no tienen código propio en la API: son billeteras sobre la red de tarjetas y el checkout de
# tarjeta internacional de Ávila Pay las ofrece en el teléfono del cliente (si Ávila Pay las tiene activas para el comercio).
METODOS_USD = {"zelle": ("ZELLE", "Zelle"), "tarjeta": ("TARJETA_INTERNACIONAL", "Tarjeta internacional (TDC/TDD)"),
               "billetera": ("TARJETA_INTERNACIONAL", "Apple Pay / Google Pay")}
ESTADOS_USD = {"PENDING": "Esperando pago", "SUCCEEDED": "Pagado", "FAILED": "Falló o fue abandonado", "REFUNDED": "Reembolsado"}
RE_UUID = re.compile(r"^[0-9a-fA-F-]{32,40}$")
METODOS = {"pago_movil": "Pago Móvil", "debito": "Débito inmediato", "credito": "Crédito inmediato", "tdc": "Tarjeta nacional (TDC/TDD)"}
ESTADOS = {"PENDING": "Esperando pago", "PROCESSING": "Verificando pago", "PAID": "Pagado", "FAILED": "Falló", "EXPIRED": "Vencido"}
RE_CLAVE = re.compile(r"^sk_(live|test)_[A-Za-z0-9_\-]{8,200}$")
RE_SESION = re.compile(r"^cs_[A-Za-z0-9]{8,128}$")


class ErrorAvila(Exception):
    pass


def _http(metodo, url, cuerpo, cabeceras, timeout=20):
    req = urllib.request.Request(url, cuerpo, cabeceras, method=metodo)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (URL fija HTTPS)
            return r.status, r.read(512 * 1024)
    except urllib.error.HTTPError as e:
        return e.code, e.read(64 * 1024)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ErrorAvila(f"No se pudo contactar a Ávila Pay ({getattr(e, 'reason', e)})") from e


def modo(api_key) -> str:
    return "Pruebas (sk_test)" if str(api_key or "").startswith("sk_test_") else "Producción (sk_live)"


def monto(v) -> Decimal:
    try:
        return Decimal(str(v)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError) as e:
        raise ErrorAvila("Monto inválido en la respuesta de Ávila Pay") from e


class AvilaPay:
    """Cliente de la API. `transporte(metodo, url, cuerpo, cabeceras) -> (estado, bytes)` se reemplaza en las pruebas."""

    def __init__(self, api_key, transporte=None):
        self.key = str(api_key or "").strip()
        if not RE_CLAVE.match(self.key): raise ErrorAvila("La clave de Ávila Pay debe empezar por sk_live_ o sk_test_")
        self.t = transporte or _http

    def _req(self, metodo, ruta, datos=None, base=BASE):
        cab = {"Authorization": f"Bearer {self.key}", "Accept": "application/json"}
        if datos is not None: cab["Content-Type"] = "application/json"
        estado, resp = self.t(metodo, base + ruta, None if datos is None else json.dumps(datos).encode(), cab)
        try:
            r = json.loads(resp or b"null")
        except ValueError:
            r = None
        if estado >= 400:
            msg = (r or {}).get("message") or (r or {}).get("error") if isinstance(r, dict) else None
            if estado in (401, 403): msg = "la clave de API no es válida o fue revocada"
            raise ErrorAvila(f"Ávila Pay: {str(msg or f'error {estado}')[:250]}")
        return r

    def probar(self) -> int:
        """Comprueba la clave: devuelve cuántas sesiones informa la primera página (0 en una cuenta nueva)."""
        r = self._req("GET", "/api/v1/checkout/sessions?limit=1&page=1")
        if isinstance(r, list): return len(r)
        if isinstance(r, dict):
            for k in ("data", "sessions", "items"):
                if isinstance(r.get(k), list): return len(r[k])
        return 0

    def crear_sesion(self, importe, referencia, descripcion, correo=None, nombre=None, return_url=None, cancel_url=None,
                     webhook_url=None, metodos=None) -> dict:
        datos = {"amount": float(Decimal(str(importe)).quantize(Decimal("0.01"))), "currency": "VES", "description": str(descripcion)[:140],
                 "reference": referencia, "metadata": {"orden": referencia}}
        if metodos: datos["methods"] = [m for m in metodos if m in METODOS]
        cliente = {k: v for k, v in (("name", nombre), ("email", correo)) if v}
        if cliente: datos["customer"] = cliente
        for k, v in (("return_url", return_url), ("cancel_url", cancel_url), ("webhook_url", webhook_url)):
            if v: datos[k] = v
        r = self._req("POST", "/api/v1/checkout/sessions", datos)
        sk, url = str((r or {}).get("session_key") or ""), str((r or {}).get("url") or "")
        if not RE_SESION.match(sk) or not url.startswith(BASE + "/"): raise ErrorAvila("Ávila Pay devolvió una sesión inválida")
        return {"session_key": sk, "url": url, "expires_at": r.get("expires_at"), "id": r.get("id")}

    def sesion(self, session_key) -> dict:
        if not RE_SESION.match(str(session_key or "")): raise ErrorAvila("Sesión de Ávila Pay inválida")
        r = self._req("GET", f"/api/v1/checkout/sessions/{urllib.parse.quote(session_key, safe='')}")
        if not isinstance(r, dict): raise ErrorAvila("Respuesta inválida de Ávila Pay")
        return {"estado": str(r.get("status") or "").upper(), "monto": monto(r.get("amount")), "moneda": str(r.get("currency") or "VES").upper(),
                "metodo": r.get("payment_method"), "pagado_en": r.get("paid_at"), "session_key": r.get("session_key") or session_key}


    # -------------------------------------------------------------- dólares: Zelle y tarjeta internacional
    def crear_pago_usd(self, importe_usd, referencia, descripcion, metodos, correo=None, return_url=None, cancel_url=None) -> dict:
        centavos = int((Decimal(str(importe_usd)) * 100).quantize(Decimal("1")))
        datos = {"amount": centavos, "currency": "USD", "orderId": referencia, "description": str(descripcion)[:140],
                 "paymentMethods": [METODOS_USD[m][0] for m in metodos if m in METODOS_USD], "metadata": {"orden": referencia}}
        for k, v in (("customerEmail", correo), ("returnUrl", return_url), ("cancelUrl", cancel_url)):
            if v: datos[k] = v
        r = self._req("POST", "/api/v1/gateway/payments", datos, base=BASE_API)
        pid, url = str((r or {}).get("id") or ""), str((r or {}).get("checkoutUrl") or "")
        if not RE_UUID.match(pid) or not url.startswith(BASE + "/"): raise ErrorAvila("Ávila Pay devolvió un pago inválido")
        return {"id": pid, "url": url}

    def pago_usd(self, pago_id) -> dict:
        if not RE_UUID.match(str(pago_id or "")): raise ErrorAvila("Pago de Ávila Pay inválido")
        r = self._req("GET", f"/api/v1/gateway/payments/{pago_id}", base=BASE_API)
        if not isinstance(r, dict): raise ErrorAvila("Respuesta inválida de Ávila Pay")
        try:
            centavos = int(r.get("amount"))
        except (TypeError, ValueError) as e:
            raise ErrorAvila("Monto inválido en la respuesta de Ávila Pay") from e
        return {"estado": str(r.get("status") or "").upper(), "monto": (Decimal(centavos) / 100).quantize(Decimal("0.01")),
                "moneda": str(r.get("currency") or "").upper(), "metodo": r.get("paymentMethod"), "pagado_en": r.get("succeededAt"),
                "orden": r.get("externalOrderId")}


def firma_valida(secreto: str, cuerpo: bytes, cabecera: str) -> bool:
    if not secreto or not cabecera: return False
    f = cabecera.strip()
    if f.startswith("sha256="): f = f[7:]
    return hmac.compare_digest(f.lower(), hmac.new(secreto.encode(), cuerpo, hashlib.sha256).hexdigest())
