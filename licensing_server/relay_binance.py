"""Relé de Binance (servicio aparte en Render, región Frankfurt: `erp-binance-relay`).

Binance no atiende peticiones desde servidores en EE. UU. (HTTP 451) y erp-licencias corre en Ohio. Este relé, alojado en
un país que Binance atiende, reenvía SOLO las dos consultas de solo lectura que usa el cobro en USDT:
  GET /sapi/v1/pay/transactions        (historial de Binance Pay)
  GET /sapi/v1/capital/deposit/hisrec  (historial de depósitos)

Seguridad:
  * Cada petición viene firmada por erp-licencias con su clave Ed25519 (la misma que firma las licencias, con un prefijo
    propio que impide confundir una firma con un token): «contax-binance-relay\\n<t>\\n<ruta?consulta>». El relé la verifica
    con la clave PÚBLICA, que descarga de LICENSE_SERVER_URL/v1/clave-publica: no hay ningún secreto que configurar aquí.
  * Marca de tiempo de ±120 s; la consulta ya viene firmada con el secreto de Binance, que NUNCA pasa por el relé (solo la
    clave de API de solo lectura, que sin el secreto no sirve para nada).
  * Solo esas dos rutas, solo GET, destino fijo https://api.binance.com: no es un proxy abierto.

Despliegue:  gunicorn licensing_server.relay_binance:app   ·   Variables: LICENSE_SERVER_URL (p. ej. https://erp-licencias.onrender.com)
"""
import base64
import json
import os
import time
import urllib.error
import urllib.request

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from flask import Flask, Response, jsonify, request

PREFIJO = b"contax-binance-relay\n"
RUTAS = ("/sapi/v1/pay/transactions", "/sapi/v1/capital/deposit/hisrec")
DESTINO = "https://api.binance.com"
TOLERANCIA_S = 120


def mensaje(t: int, ruta_consulta: str) -> bytes:
    return PREFIJO + f"{int(t)}\n{ruta_consulta}".encode()


def firmar(clave_privada, ruta_consulta: str, t=None) -> str:
    t = int(t if t is not None else time.time())
    return f"t={t},f={base64.urlsafe_b64encode(clave_privada.sign(mensaje(t, ruta_consulta))).decode().rstrip('=')}"


def _reenviar(url, cabeceras, timeout=15):
    req = urllib.request.Request(url, headers=cabeceras)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (destino fijo HTTPS)
            return r.status, r.read(2 * 1024 * 1024)
    except urllib.error.HTTPError as e:
        return e.code, e.read(64 * 1024)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 502, json.dumps({"msg": f"El relé no pudo contactar a Binance ({getattr(e, 'reason', e)})"}).encode()


def create_app(clave_publica=None, url_licencias=None, reloj=time.time, transporte=_reenviar) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 4 * 1024
    estado = {"clave": clave_publica}

    def clave():
        if estado["clave"] is None:   # se descarga una vez de erp-licencias (HTTPS) y queda en memoria
            with urllib.request.urlopen(f"{url_licencias.rstrip('/')}/v1/clave-publica", timeout=10) as r:  # noqa: S310
                b = json.loads(r.read(4096))["ed25519"]
            estado["clave"] = Ed25519PublicKey.from_public_bytes(base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)))
        return estado["clave"]

    @app.after_request
    def cabeceras(resp):
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    @app.get("/salud")
    def salud():
        return jsonify(ok=True)

    @app.get("/binance/<path:ruta>")
    def reenviar(ruta):
        ruta = "/" + ruta
        if ruta not in RUTAS: return jsonify(msg="Ruta no permitida"), 404
        consulta = request.query_string.decode()
        ruta_consulta = f"{ruta}?{consulta}" if consulta else ruta
        partes = dict(p.split("=", 1) for p in request.headers.get("X-Contax-Firma", "").split(",") if "=" in p)
        t, f = partes.get("t", ""), partes.get("f", "")
        if not t.isdigit() or abs(reloj() - int(t)) > TOLERANCIA_S: return jsonify(msg="Firma vencida o ausente"), 401
        try:
            clave().verify(base64.urlsafe_b64decode(f + "=" * (-len(f) % 4)), mensaje(int(t), ruta_consulta))
        except (InvalidSignature, ValueError):
            return jsonify(msg="Firma inválida"), 401
        except (OSError, KeyError, json.JSONDecodeError):
            return jsonify(msg="El relé no pudo obtener la clave pública de erp-licencias"), 503
        apikey = request.headers.get("X-MBX-APIKEY", "")[:128]
        est, cuerpo = transporte(DESTINO + ruta_consulta, {"X-MBX-APIKEY": apikey, "Accept": "application/json"})
        return Response(cuerpo, status=est, content_type="application/json")

    return app


def transporte_por_rele(url_rele: str, clave_privada, reloj=time.time, http=_reenviar):
    """Transporte para BinanceCuenta: en lugar de llamar a api.binance.com, llama al relé con la petición firmada."""
    base = url_rele.rstrip("/")

    def t(url, cabeceras):
        if not url.startswith(DESTINO + "/"): raise ValueError("URL de Binance inesperada")
        ruta_consulta = url[len(DESTINO):]
        return http(f"{base}/binance{ruta_consulta}", {**cabeceras, "X-Contax-Firma": firmar(clave_privada, ruta_consulta, reloj())})
    return t


if os.environ.get("LICENSE_SERVER_URL"):
    app = create_app(url_licencias=os.environ["LICENSE_SERVER_URL"])
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
