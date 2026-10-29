import json
import os
import tempfile
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.licensing.admin import ClienteAdmin, ErrorAdmin, firma
from app.licensing.client import GestorLicencia, huella_instalacion
from licensing_server.binance import BinanceCuenta, ErrorBinance
from licensing_server.server import create_app, licencias

SECRETO = "s" * 40


class Almacen:
    def __init__(self): self.d = {}
    def obtener(self): return dict(self.d)
    def guardar(self, d): self.d = dict(d)


class BinanceFalso:
    """Simula la API de una cuenta PERSONAL de Binance: historial de Binance Pay y de depósitos, con firma HMAC."""
    def __init__(self):
        self.pagos, self.depositos, self.consultas = [], [], []

    def transporte(self, url, cab):
        import hashlib, hmac, urllib.parse
        base, q = url.split("?", 1)
        params, firma_ = q.rsplit("&signature=", 1)
        assert hmac.compare_digest(hmac.new(b"SECRET", params.encode(), hashlib.sha256).hexdigest(), firma_) and cab["X-MBX-APIKEY"] == "KEY"
        args = dict(urllib.parse.parse_qsl(params)); self.consultas.append(base)
        desde, hasta = int(args["startTime"]), int(args["endTime"])
        if base.endswith("/sapi/v1/pay/transactions"):
            return 200, json.dumps({"code": "000000", "success": True, "data": [x for x in self.pagos if desde <= x["transactionTime"] <= hasta]}).encode()
        if base.endswith("/sapi/v1/capital/deposit/hisrec"):
            return 200, json.dumps([x for x in self.depositos if desde <= x["insertTime"] <= hasta]).encode()
        return 404, b"{}"

    def pago(self, monto, ts, tid, moneda="USDT"):
        self.pagos.append({"orderType": "C2C", "transactionId": tid, "transactionTime": int(ts * 1000), "amount": str(monto), "currency": moneda,
                           "payerInfo": {"binanceId": "777"}})


class Entorno:
    def __init__(self):
        self.t = [1_000_000.0]
        self.priv = Ed25519PrivateKey.generate()
        self.correos = []
        self.bf = BinanceFalso()
        self.binance = BinanceCuenta("KEY", "SECRET", transporte=self.bf.transporte, reloj=lambda: self.t[0])
        url = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "lic.db")
        self.app = create_app(url, self.priv, reloj=lambda: self.t[0], secreto_admin=SECRETO, binance=self.binance,
                              correo=lambda d, a, x, **k: self.correos.append((d, a, x)), config={"url_erp": "https://erp.test", "pago_movil": "Banco X 0414", "binance_pay_id": "123456789",
                                      "usdt_red": "TRC20", "usdt_direccion": "TXyz"})
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
def _orden_usdt(e, org="5"):
    return e.admin.crear_orden(organizacion_ref=org, cliente="Bodega", correo="a@b.test", plan="CASH", meses=1, moneda="USDT", monto_usd="0.01")


def test_usdt_monto_unico_y_entrega_automatica_una_sola_vez(e):
    e.admin.fijar_precio(plan="CASH", meses=1, monto_usd="25", max_usuarios=3, max_empresas=1, max_equipos=2)
    o = _orden_usdt(e)
    exacto = Decimal(o["monto_esperado"])
    assert Decimal("25.0001") <= exacto <= Decimal("25.0999") and "123456789" in o["instrucciones"]   # el monto lo fija el servidor
    assert e.app.extensions["revisar_pendientes"]() == 0 and e.correos == []                        # aún no pagó
    e.t[0] += 60; e.bf.pago(exacto, e.t[0], "T1")
    for _ in range(3):                                                                              # revisiones repetidas
        e.app.extensions["revisar_pendientes"]()
    e.admin.verificar(o["ref"])
    assert len(e.correos) == 1 and e.admin.ordenes(org="5")[0]["estado"] == "entregada"
    est = e.gestor("srvX").activar(_clave_de_correo(e.correos[0][2]))
    assert est.claims["plan"] == "CASH" and est.claims["max_equipos"] == 2


def test_usdt_no_entrega_con_monto_distinto_pago_viejo_o_reusado(e):
    e.admin.fijar_precio(plan="CASH", meses=1, monto_usd="25", max_usuarios=3, max_empresas=1, max_equipos=2)
    e.bf.pago("25", e.t[0] - 3600, "VIEJO")                                                         # pago anterior a la orden
    o1, o2 = _orden_usdt(e, "5"), _orden_usdt(e, "6")
    assert o1["monto_esperado"] != o2["monto_esperado"]                                              # montos únicos entre pendientes
    e.t[0] += 60
    e.bf.pago("25", e.t[0], "REDONDEO")                                                             # redondeó: no corresponde a ninguna
    e.bf.pago(o1["monto_esperado"], e.t[0], "T1", moneda="BTC")                                     # otra moneda
    e.app.extensions["revisar_pendientes"]()
    assert e.correos == []
    e.bf.pago(o1["monto_esperado"], e.t[0], "T2")
    e.app.extensions["revisar_pendientes"]()
    assert len(e.correos) == 1 and e.correos[0][0] == "a@b.test"
    # la misma transacción no puede pagar otra orden aunque alguien fuerce el monto en la base
    from licensing_server.server import ordenes
    from sqlalchemy import update
    with e.st.eng.begin() as c:
        c.execute(update(ordenes).where(ordenes.c.ref == o2["ref"]).values(monto_esperado=Decimal(o1["monto_esperado"])))
    e.admin.verificar(o2["ref"])
    assert len(e.correos) == 1


def test_usdt_por_deposito_en_red_y_vencimiento(e):
    e.admin.fijar_precio(plan="CASH", meses=1, monto_usd="25", max_usuarios=3, max_empresas=1, max_equipos=2)
    o = _orden_usdt(e)
    e.t[0] += 120
    e.bf.depositos.append({"coin": "USDT", "amount": o["monto_esperado"], "status": 1, "network": "BSC", "txId": "0xotra", "insertTime": int(e.t[0] * 1000)})
    e.app.extensions["revisar_pendientes"]()
    assert e.correos == []                                                                          # red distinta a la aceptada
    e.bf.depositos.append({"coin": "USDT", "amount": o["monto_esperado"], "status": 1, "network": "TRX", "txId": "0xtrc", "insertTime": int(e.t[0] * 1000)})
    e.bf.depositos[-1]["network"] = "TRC20"
    e.app.extensions["revisar_pendientes"]()
    assert len(e.correos) == 1
    o2 = _orden_usdt(e)
    e.t[0] += 25 * 3600
    assert e.admin.verificar(o2["ref"])["estado"] == "vencida"
    e.bf.pago(o2["monto_esperado"], e.t[0], "TARDE")                                                # pago después de vencer: no entrega solo
    e.app.extensions["revisar_pendientes"]()
    assert len(e.correos) == 1


def test_binance_firma_y_errores():
    b = BinanceCuenta("KEY", "SECRET", transporte=lambda url, cab: (401, b'{"code":-2015,"msg":"Invalid API-key"}'))
    with pytest.raises(ErrorBinance, match="401"): b.pagos_recibidos(0, 1)
    with pytest.raises(ErrorBinance): BinanceCuenta("", "")


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


# ------------------------------------------------------------------ facturación de Gigadevs
def _configurar_facturacion(e):
    e.admin.fx_guardar(razon_social="Gigadevs Corp", rif="j-50000000-1", domicilio="Caracas, Venezuela", iva_pct="16", serie="A",
                       imprenta_razon_social="Imprenta Digital SA", imprenta_rif="J-44444444-4", imprenta_providencia="SENIAT/INTI/2026/0001")
    e.admin.fx_rango(desde=1, hasta=3, fecha="2026-10-01")


def test_licencia_pagada_llega_con_su_factura_en_un_segundo_correo(e):
    _configurar_facturacion(e)
    e.admin.fijar_precio(plan="CASH", meses=1, monto_usd="25", max_usuarios=3, max_empresas=1, max_equipos=2)
    o = e.admin.crear_orden(organizacion_ref="5", cliente="Bodega SA", correo="a@b.test", plan="CASH", meses=1, moneda="USDT",
                            tasa_bcv="887.302", cliente_rif="J-12345678-9")
    e.t[0] += 60; e.bf.pago(o["monto_esperado"], e.t[0], "T1")
    e.app.extensions["revisar_pendientes"](); e.app.extensions["revisar_pendientes"]()
    assert [c[1] for c in e.correos] == ["Su licencia ERP (CASH)", "Factura N° A00000001 · ERP"]       # clave y luego factura, una vez
    f = e.admin.facturas()[0]
    exacto = Decimal(o["monto_esperado"])
    assert f["orden_ref"] == o["ref"] and f["numero_control"] == "00-00000001" and f["cliente_rif"] == "J-12345678-9"
    assert Decimal(f["total"]) == (exacto * Decimal("887.302")).quantize(Decimal("0.01"))               # el total es lo cobrado (IVA incluido)
    assert Decimal(f["base"]) + Decimal(f["iva"]) == Decimal(f["total"]) and Decimal(f["iva_pct"]) == 16
    assert "Gigadevs Corp" in e.correos[1][2] and "N° de Control" not in e.correos[0][2]
    e.admin.facturar_orden(o["ref"])                                                                    # idempotente
    assert len(e.admin.facturas()) == 1


def test_sin_rango_la_licencia_se_entrega_y_se_factura_despues(e):
    e.admin.fijar_precio(plan="CASH", meses=1, monto_usd="25", max_usuarios=3, max_empresas=1, max_equipos=2)
    o = e.admin.crear_orden(organizacion_ref="9", cliente="Alfa", correo="adm@alfa.test", plan="CASH", meses=1, moneda="VES", tasa_bcv="887.30",
                            cliente_rif="J-11111111-1")
    e.admin.informar_pago(o["ref"], organizacion_ref="9", referencia="00123456")
    e.admin.confirmar(o["ref"], "maestro")
    assert len(e.correos) == 1 and e.admin.facturas() == []                                            # licencia sí, factura pendiente
    with pytest.raises(ErrorAdmin, match="Configure"): e.admin.facturar_orden(o["ref"])
    _configurar_facturacion(e)
    e.admin.facturar_orden(o["ref"])
    f = e.admin.facturas()[0]
    assert f["moneda"] == "VES" and Decimal(f["total"]) == Decimal(o["monto"]) and len(e.correos) == 2


def test_construir_factura_manual_anular_y_cadena(e):
    _configurar_facturacion(e)
    r = e.admin.crear_factura(cliente_nombre="Cliente Externo", cliente_rif="V-12345678-9", cliente_correo="x@y.test", moneda="USD", tasa_bcv="887.302",
                              lineas=[{"descripcion": "Implementación", "cantidad": "2", "precio": "100", "alicuota": "G"},
                                      {"descripcion": "Capacitación", "cantidad": "1", "precio": "50", "alicuota": "E"}])
    f = e.admin.factura(r["id"])
    assert f["numero_completo"] == "A00000001" and Decimal(f["iva"]) == (Decimal("177460.40") * Decimal("0.16")).quantize(Decimal("0.01"))
    assert Decimal(f["exento"]) == Decimal("44365.10") and len(e.correos) == 1 and "Capacitación" in e.correos[0][2]
    with pytest.raises(ErrorAdmin, match="RIF"): e.admin.crear_factura(cliente_nombre="X", cliente_rif="123", moneda="VES", lineas=[{"descripcion": "a", "cantidad": "1", "precio": "1", "alicuota": "G"}])
    with pytest.raises(ErrorAdmin, match="motivo"): e.admin.anular_factura(r["id"], "")
    e.admin.anular_factura(r["id"], "Error en el adquirente")
    assert e.admin.factura(r["id"])["estado"] == "Anulada"
    with pytest.raises(ErrorAdmin): e.admin.anular_factura(r["id"], "otra vez")
    e.admin.crear_factura(cliente_nombre="Y", cliente_rif="J-22222222-2", moneda="VES", lineas=[{"descripcion": "a", "cantidad": "1", "precio": "1", "alicuota": "G"}], enviar=False)
    e.admin.crear_factura(cliente_nombre="Z", cliente_rif="J-33333333-3", moneda="VES", lineas=[{"descripcion": "a", "cantidad": "1", "precio": "1", "alicuota": "G"}], enviar=False)
    with pytest.raises(ErrorAdmin, match="control"):                                                   # rango 1..3 agotado
        e.admin.crear_factura(cliente_nombre="W", cliente_rif="J-44444444-4", moneda="VES", lineas=[{"descripcion": "a", "cantidad": "1", "precio": "1", "alicuota": "G"}])
    assert e.admin.fx_config()["cadena_integra"] is True
    from licensing_server import facturas as fx
    from sqlalchemy import update
    with e.st.eng.begin() as c: c.execute(update(fx.facturas).where(fx.facturas.c.id == 2).values(total=Decimal("1")))
    assert e.admin.fx_config()["cadena_integra"] is False                                               # alterar una factura se detecta
