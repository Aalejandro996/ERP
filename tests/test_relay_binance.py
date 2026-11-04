"""Relé de Binance (Frankfurt): reenvía solo las consultas de solo lectura firmadas por erp-licencias."""
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from licensing_server import relay_binance as R
from licensing_server.binance import BinanceCuenta, ErrorBinance

import pytest


class Binance:
    def __init__(self, estado=200): self.llamadas, self.estado = [], estado

    def __call__(self, url, cab):
        self.llamadas.append((url, cab))
        return self.estado, json.dumps({"code": "000000", "success": True, "data": []} if self.estado == 200 else {"msg": "restricted"}).encode()


def _entorno(t=1_000_000.0, estado=200):
    priv = Ed25519PrivateKey.generate()
    bn = Binance(estado)
    rele = R.create_app(clave_publica=priv.public_key(), reloj=lambda: t, transporte=bn).test_client()
    def http(url, cab):
        r = rele.get(url.split("rele.test", 1)[1], headers=cab); return r.status_code, r.data
    return priv, bn, rele, http


def test_consulta_firmada_llega_a_binance_tal_cual():
    priv, bn, _, http = _entorno()
    cuenta = BinanceCuenta("KEY", "SECRET", transporte=R.transporte_por_rele("https://rele.test", priv, lambda: 1_000_000.0, http), reloj=lambda: 1_000_000.0)
    assert cuenta.pagos_recibidos(0, 1) == []
    url, cab = bn.llamadas[0]
    assert url.startswith("https://api.binance.com/sapi/v1/pay/transactions?") and "&signature=" in url and cab["X-MBX-APIKEY"] == "KEY"
    assert "SECRET" not in url and "X-Contax-Firma" not in cab                                       # el secreto de Binance no viaja


def test_rechaza_otra_clave_firma_vieja_y_otras_rutas():
    priv, bn, rele, http = _entorno()
    otra = Ed25519PrivateKey.generate()
    ruta = "/sapi/v1/pay/transactions?timestamp=1&signature=ab"
    assert rele.get("/binance" + ruta, headers={"X-Contax-Firma": R.firmar(otra, ruta, 1_000_000)}).status_code == 401
    assert rele.get("/binance" + ruta, headers={"X-Contax-Firma": R.firmar(priv, ruta, 1_000_000 - 600)}).status_code == 401
    assert rele.get("/binance" + ruta).status_code == 401
    alterada = ruta.replace("timestamp=1", "timestamp=2")                                            # la firma cubre la consulta
    assert rele.get("/binance" + alterada, headers={"X-Contax-Firma": R.firmar(priv, ruta, 1_000_000)}).status_code == 401
    for prohibida in ("/sapi/v1/capital/withdraw/apply", "/api/v3/order", "/sapi/v1/asset/transfer"):
        assert rele.get("/binance" + prohibida, headers={"X-Contax-Firma": R.firmar(priv, prohibida, 1_000_000)}).status_code == 404
    assert bn.llamadas == []


def test_bloqueo_por_ubicacion_da_un_mensaje_claro():
    cuenta = BinanceCuenta("KEY", "SECRET", transporte=lambda url, cab: (451, b'{"msg":"Service unavailable from a restricted location"}'))
    with pytest.raises(ErrorBinance, match="ubicación del servidor"): cuenta.pagos_recibidos(0, 1)
