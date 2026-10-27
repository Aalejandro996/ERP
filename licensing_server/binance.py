"""Binance con cuenta PERSONAL — validación automática de pagos en USDT con una clave de API de SOLO LECTURA.

Cómo funciona:
  * Cada orden en USDT tiene un MONTO ÚNICO (p. ej. 25.0371). El cliente paga ese monto exacto a nuestro Binance Pay ID
    (o, si está configurado, lo deposita por la red indicada).
  * El servidor lee el historial de la cuenta (GET /sapi/v1/pay/transactions y, opcional, /sapi/v1/capital/deposit/hisrec),
    busca un ingreso en USDT con ese monto exacto posterior a la orden y, si lo encuentra, entrega la licencia.
  * Binance no devuelve la nota del pago: por eso se identifica por monto único + ventana de tiempo, y cada transacción de
    Binance solo puede pagar UNA orden.

Seguridad: la clave debe tener SOLO el permiso «Permitir lectura» (sin trading, retiros ni transferencias). Las consultas se
firman con HMAC-SHA256 (BINANCE_API_SECRET) y van a una URL fija por HTTPS. El cliente nunca informa el pago: se lee de Binance.
"""
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation

BASE = "https://api.binance.com"
ACREDITADO = 1  # estado de depósito «success»


class ErrorBinance(Exception):
    pass


def _get_http(url, cabeceras, timeout=15):
    req = urllib.request.Request(url, headers=cabeceras)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (URL fija HTTPS)
            return r.status, r.read(2 * 1024 * 1024)
    except urllib.error.HTTPError as e:
        return e.code, e.read(64 * 1024)


def _dec(v):
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


class BinanceCuenta:
    def __init__(self, api_key, secreto, transporte=_get_http, reloj=time.time):
        if not (api_key and secreto):
            raise ErrorBinance("Binance no está configurado (BINANCE_API_KEY / BINANCE_API_SECRET)")
        self.key, self.secreto, self.get, self.reloj = api_key, secreto.encode(), transporte, reloj

    def _llamar(self, ruta: str, params: dict):
        params = {**{k: v for k, v in params.items() if v is not None}, "timestamp": int(self.reloj() * 1000), "recvWindow": 10000}
        q = urllib.parse.urlencode(params)
        firma = hmac.new(self.secreto, q.encode(), hashlib.sha256).hexdigest()
        estado, cuerpo = self.get(f"{BASE}{ruta}?{q}&signature={firma}", {"X-MBX-APIKEY": self.key, "Accept": "application/json"})
        try:
            r = json.loads(cuerpo or b"null")
        except ValueError as e:
            raise ErrorBinance(f"Respuesta no válida de Binance ({estado})") from e
        if estado != 200:
            msg = r.get("msg") if isinstance(r, dict) else None
            raise ErrorBinance(f"Binance respondió {estado}: {str(msg)[:120]}")
        return r

    def pagos_recibidos(self, desde_ms: int, hasta_ms: int) -> list[dict]:
        """Ingresos en USDT por Binance Pay (transferencias a nuestro Pay ID)."""
        r = self._llamar("/sapi/v1/pay/transactions", {"startTime": desde_ms, "endTime": hasta_ms, "limit": 100})
        datos = r.get("data") if isinstance(r, dict) else None
        if not isinstance(datos, list): raise ErrorBinance("Historial de Binance Pay no disponible")
        out = []
        for t in datos:
            monto = _dec(t.get("amount"))
            if monto is None or monto <= 0 or t.get("currency") != "USDT" or not t.get("transactionId"): continue
            out.append({"id": f"pay:{t['transactionId']}", "ts": int(t.get("transactionTime") or 0), "monto": monto,
                        "origen": "Binance Pay", "pagador": str((t.get("payerInfo") or {}).get("binanceId") or "")[:40]})
        return out

    def depositos_recibidos(self, desde_ms: int, hasta_ms: int, red: str | None = None) -> list[dict]:
        """Depósitos en USDT por la red (TRC20, BEP20…) ya acreditados."""
        r = self._llamar("/sapi/v1/capital/deposit/hisrec", {"coin": "USDT", "status": ACREDITADO, "startTime": desde_ms, "endTime": hasta_ms})
        if not isinstance(r, list): raise ErrorBinance("Historial de depósitos no disponible")
        out = []
        for d in r:
            monto = _dec(d.get("amount"))
            if monto is None or monto <= 0 or d.get("coin") != "USDT" or d.get("status") != ACREDITADO or not d.get("txId"): continue
            if red and str(d.get("network", "")).upper() != red.upper(): continue
            out.append({"id": f"dep:{d['txId']}", "ts": int(d.get("insertTime") or 0), "monto": monto,
                        "origen": f"Depósito {d.get('network', '')}", "pagador": ""})
        return out
