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


class AvilaFalsa:
    """Simula la API de Ávila Pay: crea sesiones de checkout y responde su estado; exige la clave en la cabecera."""
    def __init__(self):
        self.sesiones, self.llamadas, self.caida, self.pagos_usd = {}, [], False, {}

    def transporte(self, metodo, url, cuerpo, cab):
        from licensing_server.avilapay import ErrorAvila
        if self.caida: raise ErrorAvila("No se pudo contactar a Ávila Pay (timeout)")
        if cab.get("Authorization") != "Bearer sk_test_clave12345": return 401, b'{"message":"Unauthorized"}'
        if url.startswith("https://api.avilapay.com/api/v1/gateway/payments"):      # dólares: Zelle / tarjeta internacional
            ruta = url.split("/payments", 1)[1]
            self.llamadas.append((metodo, "usd" + ruta, json.loads(cuerpo) if cuerpo else None))
            if metodo == "POST":
                d = json.loads(cuerpo); pid = f"5d38f849-066b-455f-b1cb-{len(self.pagos_usd) + 1:012d}"
                self.pagos_usd[pid] = {"id": pid, "externalOrderId": d["orderId"], "amount": d["amount"], "currency": d["currency"], "status": "PENDING",
                                       "paymentMethod": d["paymentMethods"][0]}
                return 200, json.dumps({"id": pid, "checkoutUrl": f"https://comercio.avilapay.com/checkout/{pid}", "status": "PENDING"}).encode()
            pg = self.pagos_usd.get(ruta.lstrip("/"))
            return (200, json.dumps(pg).encode()) if pg else (404, b'{"message":"not found"}')
        assert url.startswith("https://comercio.avilapay.com/api/v1/checkout/sessions")
        ruta = url.split("/sessions", 1)[1]
        self.llamadas.append((metodo, ruta, json.loads(cuerpo) if cuerpo else None))
        if metodo == "POST":
            d = json.loads(cuerpo); sk = f"cs_{len(self.sesiones) + 1:040d}"
            self.sesiones[sk] = {"session_key": sk, "status": "PENDING", "amount": d["amount"], "currency": d["currency"], "reference": d.get("reference")}
            return 201, json.dumps({"id": "u1", "session_key": sk, "url": f"https://comercio.avilapay.com/pay/{sk}", "status": "PENDING"}).encode()
        if ruta.startswith("?"): return 200, json.dumps({"data": list(self.sesiones.values())[:1]}).encode()
        s = self.sesiones.get(ruta.lstrip("/"))
        return (200, json.dumps(s).encode()) if s else (404, b'{"message":"not found"}')


class Entorno:
    def __init__(self):
        self.t = [1_000_000.0]
        self.priv = Ed25519PrivateKey.generate()
        self.correos = []
        self.bf = BinanceFalso()
        self.binance = BinanceCuenta("KEY", "SECRET", transporte=self.bf.transporte, reloj=lambda: self.t[0])
        self.av = AvilaFalsa()
        url = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "lic.db")
        self.app = create_app(url, self.priv, reloj=lambda: self.t[0], secreto_admin=SECRETO, binance=self.binance,
                              correo=lambda d, a, x, **k: self.correos.append((d, a, x)), config={"url_erp": "https://erp.test", "pago_movil": "Banco X 0414", "binance_pay_id": "123456789",
                                      "usdt_red": "TRC20", "usdt_direccion": "TXyz",
                                      "url_publica": "https://lic.test", "sync": {"control_financiero": "q" * 40}}, avilapay_transporte=self.av.transporte,
                              binance_transporte=self.bf.transporte)
        self.st = self.app.extensions["almacen"]
        self.http = self.app.test_client()
        self.admin = ClienteAdmin("https://lic.test", SECRETO, transporte=self._admin_http, reloj=lambda: self.t[0])

    def _admin_http(self, metodo, url, cuerpo, cab):
        r = self.http.open(url.split("lic.test", 1)[1], method=metodo, data=cuerpo, headers=cab)
        if getattr(self, "alterar", False): return r.status_code, r.data.replace(b"activa", b"suspen"), dict(r.headers)
        return r.status_code, r.data, dict(r.headers)       # con cabeceras: se verifica la firma de la respuesta

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
    assert [c[1] for c in e.correos] == ["Su licencia Contax (Cash — Facturación)", "Factura N° A00000001 · Contax"]       # clave y luego factura, una vez
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


def test_plan_personalizado_por_modulos(e):
    from app.planes import modulos_de, nombre_plan
    with pytest.raises(ValueError, match="al menos un módulo"): e.clave(plan="PERSONALIZADO")
    with pytest.raises(ValueError, match="Módulo inválido"): e.clave(plan="PERSONALIZADO", modulos=["facturacion", "nomina"])
    lid, clave = e.clave(plan="PERSONALIZADO", modulos=["facturacion", "auditoria", "facturacion"])
    gp = e.gestor("srvP")
    claims = gp.activar(clave).claims
    assert claims["plan"] == "PERSONALIZADO" and claims["modulos"] == ["auditoria", "facturacion"]
    assert modulos_de(claims) == {"auditoria", "facturacion"} and nombre_plan(claims) == "Personalizado (Auditoría financiera, Facturación)"
    assert modulos_de({"plan": "PERSONALIZADO", "modulos": ["facturacion", "inventado"]}) == {"facturacion"}   # lo desconocido no habilita nada
    e.admin.modificar(lid, modulos=["inventario"])
    assert gp.renovar().claims["modulos"] == ["inventario"]                         # llega en la renovación
    e.admin.modificar(lid, plan="CASH")                                                          # al cambiar de plan se limpian los módulos
    assert e.admin.licencias()[0]["modulos"] is None
    with pytest.raises(ErrorAdmin, match="catálogo"): e.admin.fijar_precio(plan="PERSONALIZADO", meses=1, monto_usd="10", max_usuarios=1, max_empresas=1, max_equipos=1)


def test_factura_construida_solo_con_el_cliente(e):
    _configurar_facturacion(e)
    with pytest.raises(ErrorAdmin, match="pendientes"): e.admin.facturar_cliente(7, "maestro")
    # licencia emitida por el Maestro con venta registrada: clave + factura (segundo correo) al instante
    r = e.admin.crear_licencia(cliente="Taller Uno", correo="t@uno.test", organizacion_ref="7", plan="PERSONALIZADO", modulos=["facturacion"], dias=30,
                               venta={"monto": "40", "moneda": "USDT", "tasa": "887.302", "forma_pago": "USDT por Binance", "cliente_rif": "J-30000000-1"})
    assert [c[1].split(" ")[0] for c in e.correos] == ["Su", "Factura"] and r["factura"]
    f = e.admin.facturas()[0]
    assert f["moneda"] == "USDT" and "Personalizado (Facturación), 30 días" in f["lineas"][0]["descripcion"] and Decimal(f["total"]) == Decimal("35492.08")
    # sin rango disponible la venta queda pendiente; al elegir el cliente se factura sola
    from licensing_server import facturas as fx
    from sqlalchemy import update
    with e.st.eng.begin() as c: c.execute(update(fx.config).values(control_hasta=1))     # rango agotado
    r2 = e.admin.crear_licencia(cliente="Taller Uno", correo="t@uno.test", organizacion_ref="7", plan="CASH", dias=365,
                                venta={"monto": "250", "moneda": "USD", "tasa": "887.302", "cliente_rif": "J-30000000-1"})
    assert "control" in r2["factura"]["error"] and len(e.admin.pendientes_factura(org="7")) == 1
    e.admin.fx_rango(desde=2, hasta=10, fecha="2026-10-29")
    ids = e.admin.facturar_cliente(7, "maestro")["ids"]
    assert len(ids) == 1 and e.admin.pendientes_factura(org="7") == [] and e.admin.factura(ids[0])["numero_completo"] == "A00000002"
    with pytest.raises(ErrorAdmin, match="RIF"):
        e.admin.crear_licencia(cliente="X", correo="x@y.test", organizacion_ref="8", plan="CASH", venta={"monto": "1", "cliente_rif": "malo"})


# ------------------------------------------------------------------ precios de referencia, combos personalizados y demo de 72 h
def test_precios_de_referencia_sin_pisar_los_existentes(e):
    precios = {(p["plan"], p["meses"]): p for p in e.admin.precios()}
    assert precios[("CASH", 1)]["monto_usd"] == "25.00" and precios[("CASH", 12)]["monto_usd"] == "250.00"      # mensual y anual (10 meses)
    assert {("ERP_CONTABLE", 1), ("ERP_ADMIN", 12), ("ALL_IN_ONE", 1), ("ALL_IN_ONE", 12)} <= set(precios)
    e.admin.fijar_precio(plan="CASH", meses=1, monto_usd="19.99", max_usuarios=2, max_empresas=1, max_equipos=2)
    assert e.st.sembrar_referencia() == 0                                       # ya se sembró una vez
    assert e.admin.sembrar_precios()["agregados"] == 0                          # forzado: no falta nada…
    assert next(p for p in e.admin.precios() if p["plan"] == "CASH" and p["meses"] == 1)["monto_usd"] == "19.99"   # …y lo del Maestro queda


def test_combo_personalizado_mensual_y_anual_segun_lo_que_incluye(e):
    pm = e.admin.combos()["precios_modulo"]
    assert pm["facturacion"] == "18.00" and pm["contabilidad"] == "25.00"
    assert e.admin.precio_sugerido(["facturacion", "contabilidad"], 1) == "43.00"
    assert e.admin.precio_sugerido(["facturacion", "contabilidad"], 12) == "430.00"          # anual: 10 de 12 meses
    e.admin.fijar_precio_modulo("facturacion", "20")
    mensual = e.admin.crear_combo(nombre="Tienda + Contable", modulos=["facturacion", "contabilidad"], meses=1)["id"]
    anual = e.admin.crear_combo(nombre="Tienda + Contable", modulos=["facturacion", "contabilidad"], meses=12, monto_usd="399")["id"]
    cs = {c["id"]: c for c in e.admin.combos()["combos"]}
    assert cs[mensual]["monto_usd"] == "45.00" and cs[anual]["monto_usd"] == "399.00" and cs[mensual]["modulos"] == "contabilidad,facturacion"
    with pytest.raises(ErrorAdmin, match="módulo"): e.admin.crear_combo(nombre="Vacío", modulos=[], meses=1)
    # el cliente compra el combo anual por pago móvil: recibe una licencia PERSONALIZADA con exactamente esos módulos
    o = e.admin.crear_orden(organizacion_ref="5", cliente="Beta", correo="b@beta.test", combo_id=anual, moneda="VES", tasa_bcv="40", cliente_rif="J-12345678-9")
    assert Decimal(o["monto"]) == Decimal("15960.00")
    e.admin.confirmar(o["ref"], "maestro")
    claims = e.gestor("srvC").activar(_clave_de_correo(e.correos[0][2])).claims
    assert claims["plan"] == "PERSONALIZADO" and claims["modulos"] == ["contabilidad", "facturacion"]
    e.admin.activar_combo(anual, False)                                                       # retirado de la venta
    assert all(c["id"] != anual for c in e.admin.combos(activos=True)["combos"])
    with pytest.raises(ErrorAdmin, match="no está a la venta"):
        e.admin.crear_orden(organizacion_ref="5", cliente="Beta", correo="b@beta.test", combo_id=anual, moneda="VES", tasa_bcv="40")


def test_demo_de_72_horas_una_sola_vez(e):
    from app.planes import modulos_de
    r = e.admin.solicitar_demo(organizacion_ref="7", cliente="Gamma", correo="g@gamma.test", cliente_rif="J-11111111-1")
    assert r["vence_en"] == e.t[0] + 72 * 3600
    gd = e.gestor("srvD")
    claims = gd.activar(_clave_de_correo(e.correos[-1][2])).claims
    assert claims["plan"] == "DEMO" and modulos_de(claims) == {"facturacion", "inventario"} and claims["max_empresas"] == 1
    for otra in (dict(organizacion_ref="7", correo="otro@gamma.test"), dict(organizacion_ref="8", correo="g@gamma.test"),
                 dict(organizacion_ref="9", correo="z@z.test", cliente_rif="J-11111111-1")):
        with pytest.raises(ErrorAdmin, match="ya utilizó"): e.admin.solicitar_demo(cliente="Gamma", **otra)
    e.t[0] += 72 * 3600 + 1
    assert not gd.renovar().valida                                               # a las 72 h deja de servir
    lid, _ = e.clave(plan="DEMO", dias=365)                                      # emitida por el Maestro: igual dura 72 h
    l = next(x for x in e.admin.licencias() if x["id"] == lid)
    assert l["vence_en"] - l["creada_en"] == 72 * 3600


# ------------------------------------------------------------------ Ávila Pay (bolívares, confirmación automática)
def _aviso_avila(e, cuerpo: dict, secreto="whsec_secreto_avisos"):
    import hashlib, hmac
    crudo = json.dumps(cuerpo).encode()
    return e.http.post("/v1/avilapay/aviso", data=crudo, headers={"X-AvilaPay-Signature": hmac.new(secreto.encode(), crudo, hashlib.sha256).hexdigest(),
                                                                "Content-Type": "application/json"})


def _avila_configurada(e):
    e.admin.fijar_precio(plan="ALL_IN_ONE", meses=12, monto_usd="240", max_usuarios=10, max_empresas=5, max_equipos=10)
    with pytest.raises(ErrorAdmin, match="clave"): e.admin.guardar_avilapay(api_key="sk_live_otraclave999", autor="m")   # se prueba antes de guardar
    r = e.admin.guardar_avilapay(api_key="sk_test_clave12345", webhook_secret="whsec_secreto_avisos", metodos=["pago_movil", "debito"], autor="maestro")
    assert r["modo"].startswith("Pruebas")


def test_avilapay_configuracion_cifrada_y_monitoreo(e):
    est = e.admin.avilapay()
    assert est["configurada"] is False
    assert e.http.post("/v1/avilapay/aviso", json={}, headers={"X-AvilaPay-Signature": "ab"}).status_code == 401
    assert "no hay secreto de avisos cargado" in e.admin.avilapay()["eventos"][0]["detalle"]          # el registro dice por qué
    _avila_configurada(e)
    from licensing_server.server import avila_cfg
    from sqlalchemy import select
    with e.st.eng.connect() as c:
        fila = c.execute(select(avila_cfg)).first()
    assert "sk_test_clave12345" not in str(fila) and "whsec_secreto_avisos" not in str(fila)       # nunca legible en la base
    est = e.admin.avilapay()
    assert est["activa"] and est["secreto"] and est["metodos"] == ["pago_movil", "debito"] and est["webhook_url"] == "https://lic.test/v1/avilapay/aviso"
    assert "clave12345" not in json.dumps(est) and est["clave"].endswith("2345")                    # solo se muestra enmascarada
    e.admin.guardar_avilapay(activa=False, metodos=["pago_movil"], autor="m")                       # vacío = conserva la clave
    assert e.admin.avilapay()["activa"] is False and e.admin.probar_avilapay()["ok"]
    assert any(x["evento"] == "avilapay_configurada" for x in e.admin.avilapay()["eventos"])


def test_avilapay_cobro_aviso_y_entrega_una_sola_vez(e):
    _avila_configurada(e)
    o = e.admin.crear_orden(organizacion_ref="9", cliente="Alfa", correo="adm@alfa.test", plan="ALL_IN_ONE", meses=12, moneda="VES", tasa_bcv="36.5")
    assert o["proveedor"] == "avilapay" and o["checkout_url"].startswith("https://comercio.avilapay.com/pay/cs_")
    metodo, _, enviado = e.av.llamadas[-1]
    # sin método elegido: ajuste del más caro habilitado (débito 1,5 % + IGTF 3 %) → 8760 / 0,955, redondeado hacia arriba
    assert o["monto"] == "9172.78" and o["precio"] == "8760.00" and o["ajuste"] == "412.78"
    assert enviado["amount"] == 9172.78 and enviado["currency"] == "VES" and enviado["reference"] == o["ref"] and enviado["methods"] == ["pago_movil", "debito"]
    assert enviado["webhook_url"] == "https://lic.test/v1/avilapay/aviso" and enviado["return_url"] == f"https://erp.test/admin/licencia?orden={o['ref']}"
    sk = o["checkout_url"].rsplit("/", 1)[1]
    pago = {"event": "payment.completed", "data": {"session_key": sk, "reference": o["ref"], "amount": 9172.78, "payment_reference": "PM-0001"}}
    # aviso sin firma o con firma de otro secreto: rechazado
    assert e.http.post("/v1/avilapay/aviso", json=pago).status_code == 401
    assert _aviso_avila(e, pago, secreto="otro_secreto_cualquiera").status_code == 401
    # aviso firmado pero Ávila Pay aún no lo da por pagado: no se entrega (el aviso solo dispara la consulta)
    assert _aviso_avila(e, pago).status_code == 200 and e.correos == []
    e.av.sesiones[sk]["status"] = "PAID"
    for _ in range(3): assert _aviso_avila(e, pago).status_code == 200                              # reintentos del aviso
    e.app.extensions["revisar_pendientes"](); e.admin.verificar(o["ref"])
    assert len(e.correos) == 1 and e.admin.ordenes(org="9")[0]["estado"] == "entregada"
    est = e.admin.avilapay()
    assert est["ultimo_aviso"].startswith("payment.completed") and "PM-0001" in est["ordenes"][0]["detalle_pago"]
    assert any(x["evento"] == "avilapay_aviso_rechazado" for x in est["eventos"])
    clave = _clave_de_correo(e.correos[0][2])
    assert e.gestor("srvA").activar(clave).claims["plan"] == "ALL_IN_ONE"


def test_avilapay_no_entrega_con_otro_monto_otra_sesion_o_vencida(e):
    _avila_configurada(e)
    pedir = lambda org: e.admin.crear_orden(organizacion_ref=org, cliente="A", correo="a@a.test", plan="ALL_IN_ONE", meses=12, moneda="VES", tasa_bcv="36.5")
    o1, o2 = pedir("1"), pedir("2")
    sk1, sk2 = (x["checkout_url"].rsplit("/", 1)[1] for x in (o1, o2))
    e.av.sesiones[sk1].update(status="PAID", amount=100.0)                                          # pagó otro monto
    assert e.admin.verificar(o1["ref"])["estado"] == "por_verificar" and e.correos == []
    # aviso de una orden con la sesión de OTRA orden: ignorado
    e.av.sesiones[sk2]["status"] = "PAID"
    r = _aviso_avila(e, {"event": "payment.completed", "data": {"session_key": sk2, "reference": o1["ref"]}})
    assert r.get_json()["ignorado"] and e.correos == []
    o3 = pedir("3")
    e.t[0] += 3600
    e.av.sesiones[o3["checkout_url"].rsplit("/", 1)[1]]["status"] = "EXPIRED"
    assert e.admin.verificar(o3["ref"])["estado"] == "vencida"


def test_avilapay_caido_o_pausado_cae_al_pago_movil(e):
    _avila_configurada(e)
    e.av.caida = True
    o = e.admin.crear_orden(organizacion_ref="9", cliente="Alfa", correo="adm@alfa.test", plan="ALL_IN_ONE", meses=12, moneda="VES", tasa_bcv="36.5")
    assert "checkout_url" not in o and "Banco X" in o["instrucciones"]
    assert "timeout" in e.admin.avilapay()["ultimo_error"]
    e.av.caida = False
    e.admin.guardar_avilapay(activa=False, autor="m")
    o = e.admin.crear_orden(organizacion_ref="9", cliente="Alfa", correo="adm@alfa.test", plan="ALL_IN_ONE", meses=12, moneda="VES", tasa_bcv="36.5")
    assert "checkout_url" not in o


def test_avilapay_firma_y_clave():
    from licensing_server import avilapay as AV
    import hashlib, hmac
    f = hmac.new(b"whsec_x", b"{}", hashlib.sha256).hexdigest()
    assert AV.firma_valida("whsec_x", b"{}", f) and AV.firma_valida("whsec_x", b"{}", "sha256=" + f)
    assert not AV.firma_valida("whsec_x", b"{ }", f) and not AV.firma_valida("", b"{}", f) and not AV.firma_valida("whsec_x", b"{}", "")
    for mala in ("", "pk_live_123456789", "sk_live_corta", "sk_prod_1234567890"):
        with pytest.raises(AV.ErrorAvila): AV.AvilaPay(mala)
    with pytest.raises(AV.ErrorAvila): AV.AvilaPay("sk_test_clave12345", lambda *a: (200, b"{}")).sesion("../admin")


# ------------------------------------------------------------------ dólares: Binance desde la consola, Zelle y tarjeta internacional
def test_binance_configurado_desde_la_consola_reemplaza_las_variables(e):
    e.admin.fijar_precio(plan="CASH", meses=1, monto_usd="25", max_usuarios=3, max_empresas=1, max_equipos=2)
    est = e.admin.cobros_dolares()["binance"]
    assert est["origen"] == "variables de entorno" and est["pay_id"] == "123456789"
    with pytest.raises(ErrorAdmin, match="Pay ID"): e.admin.guardar_binance(api_key="KEY", api_secret="SECRET", pay_id="abc", autor="m")
    e.admin.guardar_binance(api_key="KEY", api_secret="SECRET", pay_id="987654321", red="TRC20", direccion="T" + "x" * 33, autor="maestro")
    from licensing_server.server import binance_cfg
    from sqlalchemy import select
    with e.st.eng.connect() as c:
        assert "SECRET" not in str(c.execute(select(binance_cfg)).first())                          # cifrado en la base
    est = e.admin.cobros_dolares()["binance"]
    assert est["origen"] == "consola" and est["pay_id"] == "987654321" and est["clave"] == "KEY…KEY" and est["activa"]
    o = _orden_usdt(e)
    assert "987654321" in o["instrucciones"] and "TRC20" in o["instrucciones"]                     # usa lo de la consola
    e.t[0] += 60; e.bf.pago(o["monto_esperado"], e.t[0], "T9")
    e.app.extensions["revisar_pendientes"]()
    assert len(e.correos) == 1 and e.admin.cobros_dolares()["binance"]["ultima_revision_en"]
    assert e.admin.probar_binance()["ok"]
    e.admin.guardar_binance(activa=False, autor="m")                                                # vacío = conserva clave y Pay ID
    assert {"codigo": "USDT", "nombre": "USDT (Binance)"} not in e.admin.metodos_pago()
    with pytest.raises(ErrorAdmin, match="USDT"): _orden_usdt(e)


def test_zelle_y_tarjeta_internacional_por_avilapay(e):
    _avila_configurada(e)
    assert [m["codigo"] for m in e.admin.metodos_pago()] == ["USDT", "VES:pago_movil", "VES:debito"]
    with pytest.raises(ErrorAdmin, match="Zelle"):
        e.admin.crear_orden(organizacion_ref="9", cliente="Alfa", correo="adm@alfa.test", plan="ALL_IN_ONE", meses=12, moneda="zelle")
    e.admin.guardar_avilapay_usd(metodos=["zelle", "tarjeta"], webhook_secret="whsec_avisos_dolares", autor="maestro")
    assert [m["codigo"] for m in e.admin.metodos_pago()] == ["USDT", "zelle", "tarjeta", "VES:pago_movil", "VES:debito"]
    o = e.admin.crear_orden(organizacion_ref="9", cliente="Alfa", correo="adm@alfa.test", plan="ALL_IN_ONE", meses=12, moneda="zelle", tasa_bcv="36.5")
    assert o["moneda"] == "USD" and o["monto"] == "252.64" and o["ajuste"] == "12.64" and o["checkout_url"].startswith("https://comercio.avilapay.com/checkout/")
    _, _, enviado = e.av.llamadas[-1]
    assert enviado["amount"] == 25264 and enviado["currency"] == "USD" and enviado["paymentMethods"] == ["ZELLE"] and enviado["orderId"] == o["ref"]
    pid = o["checkout_url"].rsplit("/", 1)[1]
    import hashlib, hmac
    def aviso(cuerpo, secreto="whsec_avisos_dolares"):
        crudo = json.dumps(cuerpo).encode()
        return e.http.post("/v1/avilapay/aviso-usd", data=crudo, headers={"AvilaPay-Signature": hmac.new(secreto.encode(), crudo, hashlib.sha256).hexdigest(),
                                                                         "AvilaPay-Event": "payment.succeeded", "Content-Type": "application/json"})
    evento = {"payment_id": pid, "external_order_id": o["ref"], "amount": 25264, "currency": "USD", "status": "SUCCEEDED"}
    assert aviso(evento, secreto="whsec_otro_cualquiera").status_code == 401                         # firma con otro secreto
    assert e.http.post("/v1/avilapay/aviso-usd", json=evento).status_code == 401                    # sin cabecera de firma
    motivos = [x["detalle"] for x in e.admin.avilapay()["eventos"] if x["evento"] == "avilapay_aviso_rechazado"]
    assert "USD: el aviso no trae la cabecera de firma" in motivos[0] and "USD: la firma no coincide" in motivos[1]
    assert aviso(evento).status_code == 200 and e.correos == []                                     # Ávila Pay aún no lo aprueba
    e.av.pagos_usd[pid]["amount"] = 2400                                                            # aprobado por otro monto: a revisión
    e.av.pagos_usd[pid]["status"] = "SUCCEEDED"
    assert e.admin.verificar(o["ref"])["estado"] == "por_verificar" and e.correos == []
    o2 = e.admin.crear_orden(organizacion_ref="9", cliente="Alfa", correo="adm@alfa.test", plan="ALL_IN_ONE", meses=12, moneda="tarjeta", tasa_bcv="36.5")
    pid2 = o2["checkout_url"].rsplit("/", 1)[1]
    assert e.av.pagos_usd[pid2]["paymentMethod"] == "TARJETA_INTERNACIONAL"
    e.av.pagos_usd[pid2]["status"] = "SUCCEEDED"
    for _ in range(2): assert aviso({**evento, "payment_id": pid2, "external_order_id": o2["ref"]}).status_code == 200
    assert len(e.correos) == 1 and e.admin.ordenes(org="9")[0]["estado"] == "entregada"
    mon = e.admin.cobros_dolares()
    assert mon["avilapay"]["activa"] and mon["avilapay"]["secreto_propio"] and mon["avilapay"]["webhook_url"] == "https://lic.test/v1/avilapay/aviso-usd"
    assert {x["ref"] for x in mon["ordenes"]} >= {o["ref"], o2["ref"]}
    e.admin.guardar_avilapay_usd(activa=False, autor="m")
    assert [m["codigo"] for m in e.admin.metodos_pago()] == ["USDT", "VES:pago_movil", "VES:debito"]


def test_ajuste_por_forma_de_pago_cubre_la_comision(e):
    _avila_configurada(e)
    cm = e.admin.comisiones()
    assert cm["metodos"]["tarjeta"] == "4.20" and cm["metodos"]["pago_movil"] == "1.00" and cm["impuesto"] == "3.00"   # tarifario de lista
    metodos = [m["codigo"] for m in e.admin.metodos_pago()]
    assert "VES:pago_movil" in metodos and "VES:debito" in metodos and "VES" not in metodos
    pedir = lambda moneda: e.admin.crear_orden(organizacion_ref="9", cliente="Alfa", correo="a@a.test", plan="ALL_IN_ONE", meses=12, moneda=moneda, tasa_bcv="36.5")
    o = pedir("VES:pago_movil")                                                                     # 8760 / (1 − 1 % − 3 %)
    assert o["monto"] == "9125.00" and e.av.llamadas[-1][2]["methods"] == ["pago_movil"]
    with pytest.raises(ErrorAdmin, match="habilitado"): pedir("VES:tdc")                            # no está entre los habilitados
    e.admin.fijar_comisiones(metodos={"pago_movil": "0"}, impuesto="0", autor="m")                  # tarifa negociada
    assert pedir("VES:pago_movil")["monto"] == "8760.00"
    with pytest.raises(ErrorAdmin, match="entre 0 y 25"): e.admin.fijar_comisiones(impuesto="40")
    # el monto cobrado es el total: un pago por solo el precio (sin el ajuste) no se entrega solo
    sk = o["checkout_url"].rsplit("/", 1)[1]
    e.av.sesiones[sk].update(status="PAID", amount=8760.0)
    assert e.admin.verificar(o["ref"])["estado"] == "por_verificar"


def test_modulos_por_usuario_acotan_los_permisos_del_puesto():
    from types import SimpleNamespace
    from flask import Flask, g
    from app.security import PERMISOS, PUESTOS, tiene_permiso
    from app.models import ROLES_CLIENTE
    assert set(ROLES_CLIENTE) <= set(PERMISOS) and set(ROLES_CLIENTE) == set(PUESTOS)   # todo puesto tiene permisos y descripción
    with Flask(__name__).app_context():
        g.modulos = frozenset({"contabilidad", "administrativo", "facturacion", "inventario", "auditoria"})
        fact = SimpleNamespace(rol="Facturador", modulos="facturacion")
        assert tiene_permiso(fact, "facturacion.emitir") and not tiene_permiso(fact, "inventario.ver")   # inventario: fuera de sus módulos
        assert not tiene_permiso(SimpleNamespace(rol="Facturador", modulos=None), "asientos.ver")         # fuera de su puesto
        assert tiene_permiso(SimpleNamespace(rol="Facturador", modulos=None), "inventario.ver")           # NULL = todos los del plan
        assert tiene_permiso(SimpleNamespace(rol="Administrador", modulos="facturacion"), "asientos.crear")  # el Administrador, siempre todo
        assert not tiene_permiso(SimpleNamespace(rol="Consulta", modulos=None), "facturacion.emitir")
        g.modulos = frozenset({"facturacion"})
        assert not tiene_permiso(SimpleNamespace(rol="Contador", modulos=None), "asientos.ver")           # el plan sigue mandando


def test_las_migraciones_que_corren_en_cada_despliegue_aceptan_todos_los_puestos():
    import pathlib, re
    from app.models import ROLES
    for m in ("002_multicliente_facturacion.sql", "012_puestos_y_modulos_usuario.sql", "013_rol_abogado.sql"):
        sql = pathlib.Path("migrations", m).read_text(encoding="utf-8")
        lista = re.search(r"ck_usuarios_rol CHECK \(rol IN \(([^)]*)\)", sql).group(1)
        assert set(re.findall(r"'([^']+)'", lista)) == set(ROLES), m


def test_suscripcion_de_quincena_pagada_desde_la_app_con_avila_pay(e):
    """La app de Quincena VE pide los planes, crea el cobro en Ávila Pay y, pagado, recibe la suscripción firmada (Negocio Plus)."""
    import json as _j
    from app.licensing.admin import firma
    _avila_configurada(e)
    def sync(metodo, ruta, datos=None, secreto="q" * 40):
        cuerpo = _j.dumps(datos).encode() if datos is not None else b""
        return e.http.open(ruta, method=metodo, data=cuerpo, headers={"X-Gigadevs-Firma": firma(secreto, metodo, ruta, cuerpo, t=e.t[0]),
                                                                      "Content-Type": "application/json"})
    com = e.app.extensions["comercial"]
    com.guardar_plan("control_financiero", {"codigo": "plus", "nombre": "Negocio Plus", "precio_mensual": "5", "precio_anual": "50"})
    assert sync("GET", "/sync/v1/control_financiero/planes", secreto="x" * 40).status_code == 401
    r = sync("GET", "/sync/v1/control_financiero/planes").get_json()
    assert r["planes"][0]["precios"] == {"mensual": "5.00", "anual": "50.00"} and [m["codigo"] for m in r["metodos"]["bolivares"]] == ["pago_movil", "debito"]
    r = sync("POST", "/sync/v1/control_financiero/checkout", {"referencia": "Ana@Correo.test", "nombre": "Ana", "correo": "ana@correo.test",
              "documento": "v123456780", "plan": "plus", "periodo": "mensual", "metodo": "pago_movil", "tasa_bcv": "40", "retorno": "https://quincena.test/?orden=x"})
    assert r.status_code == 201, r.get_json()
    o = r.get_json()
    assert o["moneda"] == "VES" and o["checkout_url"].startswith("https://comercio.avilapay.com/pay/cs_") and o["ref"].startswith("OP")
    _, _, enviado = e.av.llamadas[-1]
    assert enviado["reference"] == o["ref"] and enviado["methods"] == ["pago_movil"] and enviado["return_url"] == "https://quincena.test/?orden=x"
    assert Decimal(o["monto"]) > Decimal("200")                                       # $5 × 40 Bs + ajuste por comisión
    est = sync("GET", f"/sync/v1/control_financiero/ordenes/{o['ref']}").get_json()
    assert est["estado"] == "Pendiente" and "token" not in est
    sk = o["checkout_url"].rsplit("/", 1)[1]
    e.av.sesiones[sk]["status"] = "PAID"
    pago = {"event": "payment.completed", "data": {"session_key": sk, "reference": o["ref"], "amount": float(o["monto"]), "payment_reference": "PM-7"}}
    assert _aviso_avila(e, pago).status_code == 200                                  # el aviso de Ávila Pay confirma el pago
    est = sync("GET", f"/sync/v1/control_financiero/ordenes/{o['ref']}").get_json()
    assert est["estado"] == "Pagada" and est["vence_en"] > e.t[0] and est["token"].count(".") == 1
    sub = e.st.listar_suscripciones("control_financiero")[0]
    assert sub["referencia_sujeto"] == "ana@correo.test" and sub["plan"] == "plus" and sub["max_usuarios"] == 1
    assert _aviso_avila(e, pago).status_code == 200 and len(e.st.listar_suscripciones("control_financiero")) == 1   # un pago, una suscripción
    assert com.clientes("control_financiero")[0]["documento"] == "V-12345678-0"


def test_las_respuestas_del_servidor_van_firmadas_y_una_alterada_se_rechaza(e):
    """El Maestro solo acepta respuestas firmadas por el servidor de licencias para su petición: una respuesta cambiada en el camino
    (o repetida de otra petición) se descarta."""
    e.admin.crear_licencia(cliente="Alfa", correo="a@alfa.test", organizacion_ref="1", plan="CASH", dias=30, autor="m")
    assert e.admin.licencias()[0]["estado"] == "activa"
    e.alterar = True
    with pytest.raises(ErrorAdmin, match="firma válida"): e.admin.licencias()
    e.alterar = False


def test_la_api_de_los_sistemas_rechaza_firmas_ajenas_repetidas_y_limita_peticiones(e):
    import json as _j
    from app.licensing.admin import firma, verificar_respuesta
    ruta = "/sync/v1/control_financiero/planes"
    cab = firma("q" * 40, "GET", ruta, b"", t=e.t[0])
    r = e.http.get(ruta, headers={"X-Gigadevs-Firma": cab})
    nonce = dict(x.split("=", 1) for x in cab.split(","))["n"]
    assert r.status_code == 200 and verificar_respuesta("q" * 40, r.headers["X-Gigadevs-Respuesta"], nonce, 200, r.data, e.t[0])
    assert e.http.get(ruta, headers={"X-Gigadevs-Firma": cab}).status_code == 401                      # la misma firma no sirve dos veces
    assert e.http.get(ruta, headers={"X-Gigadevs-Firma": firma("x" * 40, "GET", ruta, b"", t=e.t[0])}).status_code == 401
    assert e.http.get(ruta, headers={"X-Gigadevs-Firma": firma("q" * 40, "GET", ruta, b"", t=e.t[0] - 600)}).status_code == 401   # vieja
    from sqlalchemy import func, select
    from licensing_server.server import eventos
    with e.st.eng.connect() as c:
        assert c.execute(select(func.count()).select_from(eventos).where(eventos.c.evento == "sync_rechazado")).scalar() >= 3   # cada intento queda registrado
    cuerpo = _j.dumps({"x": 1}).encode()
    assert e.http.post("/sync/v1/control_financiero/checkout", data=cuerpo, headers={"X-Gigadevs-Firma": firma("q" * 40, "POST", "/sync/v1/control_financiero/checkout", cuerpo, t=e.t[0]),
                                                                                    "Content-Type": "text/plain"}).status_code == 415
    for _ in range(130): ultima = e.http.get(ruta)
    assert ultima.status_code == 429                                                                  # límite por IP


def test_cliente_admin_acepta_https_y_la_red_privada_de_render():
    from app.licensing.admin import url_permitida
    for u in ("https://erp-licencias.onrender.com/admin/v1/x", "http://erp-licencias-ab12:10000/admin/v1/x", "http://localhost:5001/x", "http://127.0.0.1:5001/x"):
        assert url_permitida(u), u
    for u in ("http://erp-licencias.onrender.com/x", "http://203.0.113.5/x", "ftp://erp/x", "http://[::1]/x", "file:///etc/passwd"):
        assert not url_permitida(u), u
