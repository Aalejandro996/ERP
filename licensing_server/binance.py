"""Binance Pay (API de comercios) — cobro en USDT.

Seguridad:
  * Peticiones a Binance firmadas con HMAC-SHA512 (BINANCE_PAY_API_KEY / BINANCE_PAY_SECRET, solo en el servidor de licencias).
  * Webhook: la firma RSA-SHA256 se verifica con el certificado público que publica Binance (se consulta por API autenticada).
  * Aun con firma válida, NUNCA se entrega una licencia sin volver a consultar la orden en Binance y comprobar
    estado PAID, monto y moneda contra la orden guardada (defensa en profundidad contra webhooks falsificados o repetidos).
"""
import base64
import hashlib
import hmac
import json
import secrets
import string
import time
import urllib.error
import urllib.request

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

BASE = "https://bpay.binanceapi.com"
TOLERANCIA_MS = 5 * 60 * 1000


class ErrorBinance(Exception):
    pass


def _post_http(url, cuerpo: bytes, cabeceras: dict, timeout=15):
    req = urllib.request.Request(url, cuerpo, cabeceras)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (URL fija HTTPS)
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class BinancePay:
    def __init__(self, api_key, secreto, transporte=_post_http, reloj=time.time):
        if not (api_key and secreto):
            raise ErrorBinance("Binance Pay no está configurado (BINANCE_PAY_API_KEY / BINANCE_PAY_SECRET)")
        self.key, self.secreto, self.post, self.reloj = api_key, secreto.encode(), transporte, reloj
        self._certs = {}

    def _firmar(self, ts: str, nonce: str, cuerpo: str) -> str:
        return hmac.new(self.secreto, f"{ts}\n{nonce}\n{cuerpo}\n".encode(), hashlib.sha512).hexdigest().upper()

    def _llamar(self, ruta: str, datos: dict) -> dict:
        cuerpo = json.dumps(datos, separators=(",", ":"))
        ts = str(int(self.reloj() * 1000))
        nonce = "".join(secrets.choice(string.ascii_letters) for _ in range(32))
        cab = {"Content-Type": "application/json", "BinancePay-Timestamp": ts, "BinancePay-Nonce": nonce,
               "BinancePay-Certificate-SN": self.key, "BinancePay-Signature": self._firmar(ts, nonce, cuerpo)}
        estado, resp = self.post(BASE + ruta, cuerpo.encode(), cab)
        try:
            r = json.loads(resp or b"{}")
        except ValueError as e:
            raise ErrorBinance(f"Respuesta no válida de Binance ({estado})") from e
        if estado != 200 or r.get("status") != "SUCCESS":
            raise ErrorBinance(f"Binance rechazó la operación: {r.get('code')} {str(r.get('errorMessage'))[:120]}")
        return r.get("data") or {}

    def crear_orden(self, ref, monto, descripcion, url_retorno, url_cancelar, url_webhook, expira_s=3600) -> dict:
        datos = {"env": {"terminalType": "WEB"}, "merchantTradeNo": ref, "orderAmount": float(monto), "currency": "USDT",
                 "description": descripcion[:256],
                 "goodsDetails": [{"goodsType": "02", "goodsCategory": "Z000", "referenceGoodsId": ref, "goodsName": descripcion[:256]}],
                 "returnUrl": url_retorno, "cancelUrl": url_cancelar, "webhookUrl": url_webhook,
                 "orderExpireTime": int((self.reloj() + expira_s) * 1000)}
        d = self._llamar("/binancepay/openapi/v3/order", datos)
        if not str(d.get("checkoutUrl", "")).startswith("https://"):
            raise ErrorBinance("Binance no devolvió un enlace de pago válido")
        return {"prepay_id": d.get("prepayId"), "checkout_url": d["checkoutUrl"]}

    def consultar_orden(self, ref) -> dict:
        return self._llamar("/binancepay/openapi/v2/order/query", {"merchantTradeNo": ref})

    def _certificado(self, serie):
        if serie not in self._certs:
            for c in self._llamar("/binancepay/openapi/certificates", {}) or []:
                self._certs[c.get("certSerial")] = serialization.load_pem_public_key(c["certPublic"].encode())
        if serie not in self._certs:
            raise ErrorBinance("Certificado de firma desconocido")
        return self._certs[serie]

    def verificar_webhook(self, cabeceras, cuerpo: bytes) -> dict:
        """Devuelve el JSON del aviso si la firma es auténtica y reciente; si no, lanza ErrorBinance."""
        ts, nonce = cabeceras.get("BinancePay-Timestamp", ""), cabeceras.get("BinancePay-Nonce", "")
        firma, serie = cabeceras.get("BinancePay-Signature", ""), cabeceras.get("BinancePay-Certificate-SN", "")
        if not (ts.isdigit() and nonce and firma and serie) or len(cuerpo) > 64 * 1024:
            raise ErrorBinance("Aviso incompleto")
        if abs(self.reloj() * 1000 - int(ts)) > TOLERANCIA_MS:
            raise ErrorBinance("Aviso fuera de tiempo")
        try:
            self._certificado(serie).verify(base64.b64decode(firma), f"{ts}\n{nonce}\n".encode() + cuerpo + b"\n", padding.PKCS1v15(), hashes.SHA256())
        except (InvalidSignature, ValueError) as e:
            raise ErrorBinance("Firma del aviso no válida") from e
        return json.loads(cuerpo)
