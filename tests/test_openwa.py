"""Conexión del bot por OpenWA: cliente de la API, firma de avisos y lectura de mensajes (sin red ni base de datos)."""
import base64
import hashlib
import hmac
import json

import pytest

from app import openwa as OW
from app.whatsapp import ErrorWhatsapp

SID = "8f3c2b1a-9d4e-4c7a-8b2f-1e6d5a4c3b2a"


class Falsa:
    """Pasarela OpenWA mínima: responde como su API y anota cada llamada."""
    def __init__(self, respuestas):
        self.r, self.llamadas = respuestas, []

    def __call__(self, metodo, url, cuerpo, cab):
        self.llamadas.append((metodo, url, json.loads(cuerpo) if cuerpo else None, cab))
        estado, resp = self.r.get((metodo, url.split("/api", 1)[1]), (404, {"message": "not found"}))
        return estado, resp if isinstance(resp, bytes) else json.dumps(resp).encode()


def test_url_de_la_pasarela():
    assert OW.validar_url("https://wa.gigadevs.com/api/") == "https://wa.gigadevs.com"
    assert OW.validar_url("http://openwa:2785") == "http://openwa:2785"          # red privada de Render
    for mala in ("http://wa.gigadevs.com", "ftp://x", "", "javascript:alert(1)"):
        with pytest.raises(ErrorWhatsapp): OW.validar_url(mala)


def test_sesion_qr_envio_y_cierre():
    f = Falsa({("POST", "/sessions"): (201, {"id": SID, "status": "created"}),
               ("POST", f"/sessions/{SID}/webhooks"): (201, {"id": "w1"}),
               ("POST", f"/sessions/{SID}/start"): (400, {"message": "Session already started"}),
               ("GET", f"/sessions/{SID}"): (200, {"status": "ready", "phone": "584141234567", "pushName": "Bodega"}),
               ("GET", f"/sessions/{SID}/qr"): (200, {"qrCode": "data:image/png;base64,AAAA", "status": "qr_ready"}),
               ("POST", f"/sessions/{SID}/messages/send-text"): (201, {"messageId": "true_58414@c.us_3EB0", "timestamp": 1}),
               ("POST", f"/sessions/{SID}/logout"): (400, {"message": "Session not running"}),
               ("DELETE", f"/sessions/{SID}"): (204, b"")})
    ow = OW.Pasarela("https://wa.test", "owa_k1_clave", f)
    assert ow.crear_sesion(OW.nombre_sesion(7, "Ab-12")) == SID and f.llamadas[0][2] == {"name": "contax-e7-ab12"}
    ow.registrar_webhook(SID, "https://erp.test/wa/k", "s" * 32)
    assert f.llamadas[-1][2]["events"][0] == "message.received" and f.llamadas[-1][2]["secret"] == "s" * 32
    ow.iniciar(SID)                                                                  # «already started» no es error
    assert ow.estado(SID) == {"estado": "ready", "telefono": "584141234567", "nombre": "Bodega"}
    assert ow.qr(SID) == "data:image/png;base64,AAAA"
    assert ow.enviar_texto(SID, "0414-123.45.67", "Hola") == "true_58414@c.us_3EB0"
    metodo, url, cuerpo, cab = f.llamadas[-1]
    assert cuerpo == {"chatId": "584141234567@c.us", "text": "Hola"} and cab["X-API-Key"] == "owa_k1_clave" and "clave" not in url   # clave solo en cabecera
    ow.cerrar(SID)                                                                   # no estaba en marcha: igual se borra
    assert f.llamadas[-1][0] == "DELETE"
    with pytest.raises(ErrorWhatsapp): ow.estado("../../admin")                      # ids validados antes de armar la URL


def test_errores_de_la_pasarela():
    ow = OW.Pasarela("https://wa.test", "k", Falsa({("GET", "/sessions"): (401, {"message": "Unauthorized"})}))
    with pytest.raises(ErrorWhatsapp, match="clave de API"): ow.salud()
    with pytest.raises(OW.SesionInexistente): OW.Pasarela("https://wa.test", "k", Falsa({})).estado(SID)
    assert OW.Pasarela("https://wa.test", "k", Falsa({("GET", f"/sessions/{SID}/qr"): (400, {"message": "QR not ready"})})).qr(SID) is None
    with pytest.raises(ErrorWhatsapp): OW.Pasarela("https://wa.test", "", Falsa({}))


def test_firma_de_los_avisos():
    cuerpo = b'{"event":"message.received"}'
    firma = "sha256=" + hmac.new(b"secreto-de-16-chars", cuerpo, hashlib.sha256).hexdigest()
    assert OW.firma_valida("secreto-de-16-chars", cuerpo, firma)
    assert not OW.firma_valida("otro", cuerpo, firma) and not OW.firma_valida("secreto-de-16-chars", cuerpo + b" ", firma)
    assert not OW.firma_valida("", cuerpo, firma) and not OW.firma_valida("x", cuerpo, firma[7:])


def _aviso(**m):
    return {"event": "message.received", "sessionId": SID, "data": {"id": "true_x_1", "type": "text", "fromMe": False, "isGroup": False, "kind": "individual", **m}}


def test_mensajes_entrantes():
    r = list(OW.mensajes_entrantes(_aviso(**{"from": "584141234567@c.us", "body": "Hola", "contact": {"pushName": "Ana"}})))
    assert r == [("584141234567", "Ana", "Hola", "ow.true_x_1", None)]
    # remitente con id de privacidad: se atiende solo si la pasarela informa su número
    assert list(OW.mensajes_entrantes(_aviso(**{"from": "1234567890123@lid", "body": "x"}))) == []
    assert list(OW.mensajes_entrantes(_aviso(**{"from": "1234567890123@lid", "senderPhone": "584149990000", "body": "x"})))[0][0] == "584149990000"
    for ignorado in ({"fromMe": True}, {"isGroup": True}, {"kind": "status"}):
        assert list(OW.mensajes_entrantes(_aviso(**{"from": "584141234567@c.us", "body": "x", **ignorado}))) == []
    assert list(OW.mensajes_entrantes({"event": "message.ack", "data": {}})) == []
    # PDF pequeño: viene dentro del aviso; grande: queda para descargarlo de la pasarela
    pdf = base64.b64encode(b"%PDF-1.4 orden").decode()
    (_, _, texto, _, adj), = OW.mensajes_entrantes(_aviso(**{"from": "584141234567@c.us", "chatId": "584141234567@c.us", "type": "document", "body": "",
                                                          "media": {"mimetype": "application/pdf", "filename": "OC.pdf", "data": pdf}}))
    assert adj["datos"] == b"%PDF-1.4 orden" and adj["nombre"] == "OC.pdf" and adj["chat"] == "584141234567@c.us" and "OC.pdf" in texto
    (*_, adj), = OW.mensajes_entrantes(_aviso(**{"from": "584141234567@c.us", "type": "document", "media": {"mimetype": "application/pdf", "omitted": True}}))
    assert "datos" not in adj and adj["id"] == "true_x_1"


def test_cambios_de_estado():
    assert OW.estado_del_aviso({"event": "session.status", "data": {"status": "qr_ready"}}) == ("qr_ready", None)
    assert OW.estado_del_aviso({"event": "session.authenticated", "data": {"phone": "58414"}}) == ("ready", "58414")
    assert OW.estado_del_aviso({"event": "session.disconnected", "data": {}}) == ("disconnected", None)
    assert OW.estado_del_aviso(_aviso(body="hola")) is None
