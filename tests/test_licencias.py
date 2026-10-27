import base64
import json
import os
import tempfile
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.licensing.admin import ClienteAdmin, ErrorAdmin, firma
from app.licensing.client import GestorLicencia, huella_instalacion
from licensing_server.binance import BinancePay, ErrorBinance
from licensing_server.server import create_app, licencias

SECRETO = "s" * 40


class Almacen:
    def __init__(self): self.d = {}
    def obtener(self): return dict(self.d)
    def guardar(self, d): self.d = dict(d)


class BinanceFalso:
    """Simula la API de Binance Pay, incluida la firma RSA de los avisos."""
    def __init__(self, reloj):
        self.rsa = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.ordenes, self.reloj = {}, reloj

    def transporte(self, url, cuerpo, cab):
        d = json.loads(cuerpo)
        if url.endswith("/certificates"):
            pem = self.rsa.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
            return 200, json.dumps({"status": "SUCCESS", "data": [{"certSerial": "SN1", "certPublic": pem}]}).encode()
        if url.endswith("/v3/order"):
            self.ordenes[d["merchantTradeNo"]] = {"status": "INITIAL", "orderAmount": str(d["orderAmount"]), "currency": d["currency"], "merchantTradeNo": d["merchantTradeNo"]}
            return 200, json.dumps({"status": "SUCCESS", "data": {"prepayId": "P1", "checkoutUrl": "https://pay.binance.com/checkout/x"}}).encode()
        if url.endswith("/order/query"):
            return 200, json.dumps({"status": "SUCCESS", "data": self.ordenes[d["merchantTradeNo"]]}).encode()
        return 404, b"{}"

    def aviso(self, ref, ts=None):
        cuerpo = json.dumps({"bizType": "PAY", "bizStatus": "PAY_SUCCESS", "data": json.dumps({"merchantTradeNo": ref})}).encode()
        ts = str(int((ts or self.reloj()) * 1000)); nonce = "n" * 32
        f = self.rsa.sign(f"{ts}\n{nonce}\n".encode() + cuerpo + b"\n", padding.PKCS1v15(), hashes.SHA256())
        return {"BinancePay-Timestamp": ts, "BinancePay-Nonce": nonce, "BinancePay-Signature": base64.b64encode(f).decode(),
                "BinancePay-Certificate-SN": "SN1", "Content-Type": "application/json"}, cuerpo


class Entorno:
    def __init__(self):
        self.t = [1_000_000.0]
        self.priv = Ed25519PrivateKey.generate()
        self.correos = []
        self.bf = BinanceFalso(lambda: self.t[0])
        self.binance = BinancePay("KEY", "SECRET", transporte=self.bf.transporte, reloj=lambda: self.t[0])
        url = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "lic.db")
        self.app = create_app(url, self.priv, reloj=lambda: self.t[0], secreto_admin=SECRETO, binance=self.binance,
                              correo=lambda d, a, x, **k: self.correos.append((d, a, x)), config={"url_erp": "https://erp.test", "pago_movil": "Banco X 0414"})
        self.st = self.app.extensions["almacen"]
        self.http = self.app.test_client()
        self.admin = ClienteAdmin("https://lic.test", SECRETO, transporte=self._admin_http, reloj=lambda: self.t[0])

    def _admin_http(self, metodo, url, cuerpo, cab):
        r = self.http.open(url.split("lic.test", 1)[1], method=metodo, data=cuerpo, headers=cab)
        return r.status_code, r.data

    def transporte(self, url, data, timeout=10):
        r = self.http.post("/" + url.split("/", 3)[3], json=data)
        return r.status_code, r.get_json()

    def gestor(self, secreto, almacen=None):
        return GestorLicencia("https://lic.test", self.priv.public_key(), almacen or Almacen(),
                              huella_instalacion(secreto), self.transporte, lambda: self.t[0])

    def clave(self, **kw):
        return self.st.crear_licencia(kw.pop("cliente", "Cliente A"), **kw)


@pytest.fixture
def e(): return Entorno()


def _clave_de_correo(texto):
    return next(l.split(": ", 1)[1] for l in texto.splitlines() if "Clave de licencia:" in l)


# ------------------------------------------------------------------ activación (comportamiento previo)
def test_activacion_y_uso(e):
    _, clave = e.clave(plan="CASH", max_equipos=3)
    g = e.gestor("srvA")
    est = g.activar(clave)
    assert est.valida and est.claims["plan"] == "CASH" and est.claims["max_equipos"] == 3
    assert g.estado().valida


def test_uso_unico_segunda_instalacion_rechazada(e):
    _, clave = e.clave()
    assert e.gestor("srvA").activar(clave).valida
    r = e.gestor("srvB").activar(clave)
    assert not r.valida and "en uso" in r.motivo


def test_varias_instalaciones_si_el_maestro_lo_permite(e):
    _, clave = e.clave(max_instancias=2)
    assert e.gestor("srvA").activar(clave).valida and e.gestor("srvB").activar(clave).valida
    assert not e.gestor("srvC").activar(clave).valida


def test_token_copiado_a_otra_instalacion_no_sirve(e):
    _, clave = e.clave()
    a = Almacen(); e.gestor("srvA", a).activar(clave)
    copia = Almacen(); copia.guardar(a.obtener())
    assert not e.gestor("srvB", copia).estado().valida


def test_token_alterado_rechazado(e):
    _, clave = e.clave()
    a = Almacen(); g = e.gestor("srvA", a); g.activar(clave)
    d = a.obtener(); p, s = d["token"].split("."); d["token"] = p[:-2] + "AA." + s; a.guardar(d)
    assert not g.estado().valida


def test_vencimiento_y_renovacion(e):
    _, clave = e.clave()
    g = e.gestor("srvA"); g.activar(clave)
    e.t[0] += 8 * 86400
    assert not g.estado().valida
    assert g.renovar().valida and g.estado().valida


def test_retroceso_de_reloj(e):
    _, clave = e.clave()
    g = e.gestor("srvA"); g.activar(clave)
    e.t[0] += 3 * 86400; assert g.estado().valida
    e.t[0] -= 3 * 86400
    assert "reloj" in g.estado().motivo.lower()


def test_revocacion_remota_y_liberacion(e):
    lid, clave = e.clave()
    a = Almacen(); g = e.gestor("srvA", a); g.activar(clave)
    e.st.liberar(lid)
    assert e.gestor("srvB").activar(clave).valida
    e.st.modificar(lid, {"estado": "revocada"})
    assert not e.gestor("srvB").renovar().valida


def test_claves_hasheadas(e):
    _, clave = e.clave()
    assert not e.gestor("srvA").activar("ERP-000000-000000-000000-000000").valida
    with e.st.eng.connect() as c:
        guardado = [r[0] for r in c.execute(licencias.select().with_only_columns(licencias.c.key_hash))]
    assert clave not in guardado and len(guardado[0]) == 64


# ------------------------------------------------------------------ API del Maestro
def test_api_admin_rechaza_sin_firma_repetida_o_vencida(e):
    assert e.http.get("/admin/v1/licencias").status_code == 401
    cab = {"X-ERP-Firma": firma(SECRETO, "GET", "/admin/v1/licencias", b"", t=e.t[0])}
    assert e.http.get("/admin/v1/licencias", headers=cab).status_code == 200
    assert e.http.get("/admin/v1/licencias", headers=cab).status_code == 401              # repetición (mismo nonce)
    viejo = {"X-ERP-Firma": firma(SECRETO, "GET", "/admin/v1/licencias", b"", t=e.t[0] - 600)}
    assert e.http.get("/admin/v1/licencias", headers=viejo).status_code == 401            # fuera de ventana
    otra = {"X-ERP-Firma": firma("x" * 40, "GET", "/admin/v1/licencias", b"", t=e.t[0])}
    assert e.http.get("/admin/v1/licencias", headers=otra).status_code == 401             # secreto incorrecto
    alterada = {"X-ERP-Firma": firma(SECRETO, "POST", "/admin/v1/precios", b"{}", t=e.t[0])}
    assert e.http.put("/admin/v1/precios", data=b'{"plan":"CASH"}', headers=alterada).status_code == 401  # cuerpo/método distinto


def test_maestro_crea_licencia_y_la_clave_solo_va_por_correo(e):
    r = e.admin.crear_licencia(cliente="Bodega", correo="dueno@bodega.test", plan="ERP_ADMIN", max_equipos=4, dias=30, organizacion_ref="7")
    assert "clave" not in json.dumps(r).lower() or set(r) == {"id"}
    (dest, _, texto), = e.correos
    clave = _clave_de_correo(texto)
    assert dest == "dueno@bodega.test" and e.gestor("srvA").activar(clave).claims["org"] == "7"
    e.admin.reenviar(r["id"])                                                              # nueva clave: la anterior se anula
    nueva = _clave_de_correo(e.correos[-1][2])
    assert nueva != clave and not e.gestor("srvB").activar(clave).valida and e.gestor("srvB").activar(nueva).valida
    e.admin.modificar(r["id"], plan="ALL_IN_ONE", max_equipos=9, autor="maestro")
    assert e.admin.licencias(org="7")[0]["max_equipos"] == 9
    with pytest.raises(ErrorAdmin): e.admin.modificar(r["id"], plan="'; DROP TABLE lic_licencias; --")
    assert e.admin.licencias()                                                              # la tabla sigue ahí


# ------------------------------------------------------------------ muro de pago
def test_pago_usdt_con_binance_entrega_una_sola_vez(e):
    e.admin.fijar_precio(plan="CASH", meses=1, monto_usd="25", max_usuarios=3, max_empresas=1, max_equipos=2)
    o = e.admin.crear_orden(organizacion_ref="5", cliente="Bodega", correo="a@b.test", plan="CASH", meses=1, moneda="USDT", monto_usd="0.01")
    assert o["monto"] == "25.00" and o["checkout_url"].startswith("https://")             # el monto lo fija el servidor
    cab, cuerpo = e.bf.aviso(o["ref"])
    assert e.http.post("/v1/pagos/binance/webhook", data=cuerpo, headers=cab).status_code == 200
    assert e.correos == []                                                                 # Binance dice INITIAL: no se entrega
    e.bf.ordenes[o["ref"]]["status"] = "PAID"
    for _ in range(3):                                                                      # avisos repetidos
        cab, cuerpo = e.bf.aviso(o["ref"])
        assert e.http.post("/v1/pagos/binance/webhook", data=cuerpo, headers=cab).status_code == 200
    assert len(e.correos) == 1 and e.admin.ordenes(org="5")[0]["estado"] == "entregada"
    est = e.gestor("srvX").activar(_clave_de_correo(e.correos[0][2]))
    assert est.claims["plan"] == "CASH" and est.claims["max_equipos"] == 2


def test_webhook_falso_o_monto_distinto_no_entrega(e):
    e.admin.fijar_precio(plan="CASH", meses=1, monto_usd="25", max_usuarios=3, max_empresas=1, max_equipos=2)
    o = e.admin.crear_orden(organizacion_ref="5", cliente="B", correo="a@b.test", plan="CASH", meses=1, moneda="USDT")
    cab, cuerpo = e.bf.aviso(o["ref"])
    cab["BinancePay-Signature"] = base64.b64encode(b"x" * 256).decode()
    assert e.http.post("/v1/pagos/binance/webhook", data=cuerpo, headers=cab).status_code == 400
    cab, cuerpo = e.bf.aviso(o["ref"], ts=e.t[0] - 3600)
    assert e.http.post("/v1/pagos/binance/webhook", data=cuerpo, headers=cab).status_code == 400   # aviso viejo
    e.bf.ordenes[o["ref"]].update(status="PAID", orderAmount="1.00")                          # pagó menos
    cab, cuerpo = e.bf.aviso(o["ref"])
    e.http.post("/v1/pagos/binance/webhook", data=cuerpo, headers=cab)
    assert e.correos == []
    with pytest.raises(ErrorBinance): e.binance.verificar_webhook({}, b"{}")


def test_pago_movil_en_bolivares_lo_confirma_el_maestro(e):
    e.admin.fijar_precio(plan="ALL_IN_ONE", meses=12, monto_usd="240", max_usuarios=10, max_empresas=5, max_equipos=10)
    o = e.admin.crear_orden(organizacion_ref="9", cliente="Alfa", correo="adm@alfa.test", plan="ALL_IN_ONE", meses=12, moneda="VES", tasa_bcv="36.5")
    assert Decimal(o["monto"]) == Decimal("8760.00") and "Banco X" in o["instrucciones"]
    with pytest.raises(ErrorAdmin): e.admin.informar_pago(o["ref"], organizacion_ref="1", referencia="123456")   # otra organización
    with pytest.raises(ErrorAdmin): e.admin.informar_pago(o["ref"], organizacion_ref="9", referencia="1 OR 1=1")
    e.admin.informar_pago(o["ref"], organizacion_ref="9", referencia="00123456", banco="Banesco")
    assert e.admin.ordenes(org="9")[0]["estado"] == "por_verificar" and e.correos == []
    e.admin.confirmar(o["ref"], "maestro"); e.admin.confirmar(o["ref"], "maestro")       # doble clic
    assert len(e.correos) == 1 and e.correos[0][0] == "adm@alfa.test"
    with pytest.raises(ErrorAdmin): e.admin.crear_orden(organizacion_ref="9", cliente="A", correo="x@y.test", plan="CASH", meses=7, moneda="VES", tasa_bcv="36")
