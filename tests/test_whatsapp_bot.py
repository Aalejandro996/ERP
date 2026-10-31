import hashlib
import hmac
from decimal import Decimal

import pytest

from app import whatsapp as WA
from app import whatsapp_bot as B

CAT = {"A001": ("Harina PAN 1kg", Decimal("45.50"), Decimal("10")), "B002": ("Aceite 1L", Decimal("120"), Decimal("0"))}


def test_contacto_nuevo_elige_si_es_cliente_o_proveedor():
    r, est, ev = B.responder("hola", "nuevo", {}, CAT, "Bodega Uno")
    assert "cliente" in r[0] and ev == []
    r, est, ev = B.responder("2", "nuevo", {}, CAT, "Bodega Uno")
    assert ev == [{"tipo_contacto": "proveedor"}] and "proveedores" in r[0]


def test_cliente_arma_y_confirma_su_pedido():
    r, est, _ = B.responder("1", "cliente", {}, CAT, "Bodega")
    assert "A001" in r[0] and "agotado" in r[0]
    r, est, _ = B.responder("a001 x3", "cliente", est, CAT, "Bodega")
    assert est["carrito"] == {"A001": "3"}
    r, est2, _ = B.responder("B002 1", "cliente", est, CAT, "Bodega")
    assert "Solo tenemos 0" in r[0] and est2 == est                              # sin existencia no se agrega
    r, _, _ = B.responder("2", "cliente", est, CAT, "Bodega")
    assert "136,50" in r[0]
    r, est, ev = B.responder("3", "cliente", est, CAT, "Bodega")
    assert est == {} and ev[0]["crear_pedido"]["tipo"] == "venta" and ev[0]["crear_pedido"]["total"] == "136.50"
    assert ev[0]["crear_pedido"]["lineas"] == [{"codigo": "A001", "descripcion": "Harina PAN 1kg", "cantidad": "3", "precio": "45.50"}]
    r, _, ev = B.responder("3", "cliente", {}, CAT, "Bodega")
    assert "vacío" in r[0] and ev == []


def test_proveedor_confirma_rechaza_y_oferta():
    ocs = [(12, "Enviada", Decimal("100"), "USD"), (13, "Recibida", Decimal("5"), "VES")]
    r, _, ev = B.responder("1", "proveedor", {}, CAT, "Bodega", ordenes=ocs)
    assert "N° 12" in r[0] and "N° 13" not in r[0]
    assert B.responder("CONFIRMAR 12", "proveedor", {}, CAT, "Bodega", ordenes=ocs)[2] == [{"oc_confirmada": 12}]
    _, _, ev = B.responder("rechazar 12 sin existencia", "proveedor", {}, CAT, "Bodega", ordenes=ocs)
    assert ev == [{"oc_rechazada": 12, "motivo": "sin existencia"}]
    assert "ya está recibida" in B.responder("confirmar 13", "proveedor", {}, CAT, "Bodega", ordenes=ocs)[0][0]
    assert "No encontramos" in B.responder("confirmar 99", "proveedor", {}, CAT, "Bodega", ordenes=ocs)[0][0]   # no ve órdenes ajenas
    _, est, _ = B.responder("2", "proveedor", {}, CAT, "Bodega")
    _, est, _ = B.responder("A001 50 40,10\nentrega el lunes", "proveedor", est, CAT, "Bodega")
    _, est, ev = B.responder("FIN", "proveedor", est, CAT, "Bodega")
    p = ev[0]["crear_pedido"]
    assert est == {} and p["tipo"] == "compra" and p["lineas"][0] == {"codigo": "A001", "descripcion": "Harina PAN 1kg", "cantidad": "50", "precio": "40.10"}
    assert p["nota"] == "entrega el lunes"


def test_cliente_de_api_whatsapp():
    assert WA.telefono("0412-555.12.34") == "584125551234" and WA.telefono("+58 412 5551234") == "584125551234"
    with pytest.raises(ValueError): WA.telefono("123")
    cuerpo = b'{"x":1}'
    assert WA.firma_valida("sec", cuerpo, "sha256=" + hmac.new(b"sec", cuerpo, hashlib.sha256).hexdigest())
    assert not WA.firma_valida("sec", cuerpo, "sha256=" + "0" * 64) and not WA.firma_valida("sec", cuerpo, "")
    aviso = {"entry": [{"changes": [{"value": {"contacts": [{"wa_id": "58412", "profile": {"name": "Ana"}}],
                                                "messages": [{"from": "58412", "id": "wamid.1", "type": "text", "text": {"body": "hola"}},
                                                             {"from": "58412", "id": "wamid.2", "type": "image"}]}}]}]}
    assert list(WA.mensajes_entrantes(aviso)) == [("58412", "Ana", "hola", "wamid.1", None), ("58412", "Ana", "[image]", "wamid.2", None)]
    enviados = []
    def transporte(url, cuerpo, cab):
        enviados.append((url, cuerpo, cab)); return 200, b'{"messages":[{"id":"wamid.out"}]}'
    assert WA.enviar_texto("123", "TOK", "04125551234", "hola", transporte=transporte) == "wamid.out"
    assert enviados[0][0].endswith("/123/messages") and enviados[0][2]["Authorization"] == "Bearer TOK" and b"584125551234" in enviados[0][1]
    with pytest.raises(WA.ErrorWhatsapp, match="24 h"):
        WA.enviar_texto("123", "TOK", "04125551234", "x", transporte=lambda *a: (400, b'{"error":{"message":"Re-engagement","code":131047}}'))


# ------------------------------------------------------------------ WasenderAPI (WhatsApp por QR)
def test_wasender_lee_avisos_firma_envio_y_adjuntos():
    import json
    from app import wasender as WS
    aviso = {"event": "messages.received", "data": {"messages": {
        "key": {"id": "ABC1", "fromMe": False, "remoteJid": "584141234567@s.whatsapp.net"}, "pushName": "Ana",
        "message": {"conversation": "hola"}}}}
    assert list(WS.mensajes_entrantes(aviso)) == [("584141234567", "Ana", "hola", "ws.ABC1", None)]
    doc = {"event": "messages.upsert", "data": {"messages": [
        {"key": {"id": "D1", "fromMe": False, "remoteJid": "584149990000@s.whatsapp.net"},
         "message": {"documentMessage": {"mimetype": "application/pdf", "fileName": "OC.pdf", "caption": "mi orden"}}},
        {"key": {"id": "X", "fromMe": True, "remoteJid": "584149990000@s.whatsapp.net"}, "message": {"conversation": "eco"}},
        {"key": {"id": "G", "remoteJid": "1203630@g.us"}, "message": {"conversation": "grupo"}}]}}
    (tel, _, texto, mid, adj), = list(WS.mensajes_entrantes(doc))                 # se ignoran fromMe y grupos
    assert tel == "584149990000" and texto == "mi orden" and adj["nombre"] == "OC.pdf" and adj["raw"]["key"]["id"] == "D1"
    assert list(WS.mensajes_entrantes({"event": "messages.update", "data": aviso["data"]})) == []
    cuerpo = b'{"a":1}'
    assert WS.firma_valida("sec", cuerpo, "sec") and WS.firma_valida("sec", cuerpo, hmac.new(b"sec", cuerpo, hashlib.sha256).hexdigest())
    assert not WS.firma_valida("sec", cuerpo, "otro") and not WS.firma_valida("", cuerpo, "")
    llamadas = []
    def t(metodo, url, cuerpo, cab):
        llamadas.append((metodo, url, json.loads(cuerpo) if cuerpo else None, cab))
        if url.endswith("/send-message"): return 200, b'{"success":true,"data":{"msgId":77}}'
        if url.endswith("/decrypt-media"): return 200, b'{"success":true,"publicUrl":"https://cdn.wasender.test/x.pdf"}'
        if url.startswith("https://cdn."): return 200, b"%PDF-1.4 ..."
        return 429, b'{"success":false,"message":"Too many requests"}'
    assert WS.enviar_texto(None, "KEY", "0414-1234567", "hola", transporte=t) == "77"
    assert llamadas[0][1] == "https://www.wasenderapi.com/api/send-message" and llamadas[0][2] == {"to": "+584141234567", "text": "hola"}
    assert llamadas[0][3]["Authorization"] == "Bearer KEY"
    datos, mime = WS.descargar_media(None, "KEY", adj["raw"], transporte=t)
    assert datos.startswith(b"%PDF") and mime == "application/pdf" and llamadas[1][2] == {"data": {"messages": adj["raw"]}}
    with pytest.raises(WA.ErrorWhatsapp, match="límite"): WS.estado_sesion(None, "KEY", transporte=t)
    with pytest.raises(WA.ErrorWhatsapp): WS.enviar_texto("http://inseguro", "KEY", "04141234567", "x", transporte=t)


def test_presupuesto_por_el_bot():
    r, est, ev = B.responder("6", "cliente", {}, CAT, "Bodega")
    assert ev == [] and "agregue los productos" in r[0]
    _, est, _ = B.responder("A001 4", "cliente", {}, CAT, "Bodega")
    r, est, ev = B.responder("presupuesto", "cliente", est, CAT, "Bodega")
    assert ev == [{"crear_presupuesto": {"lineas": [{"codigo": "A001", "descripcion": "Harina PAN 1kg", "cantidad": "4"}]}}] and "carrito" not in est
    assert B.responder("ACEPTO 12", "cliente", {}, CAT, "Bodega")[2] == [{"presupuesto_aceptado": 12}]
