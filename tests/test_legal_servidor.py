"""Documentos legales en el servidor de licencias: plantilla → revisión → aprobación de la abogada → aceptación con evidencia."""
import base64
import json
import os
import tempfile

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from licensing_server.server import create_app

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def lg():
    app = create_app("sqlite:///" + os.path.join(tempfile.mkdtemp(), "l.db"), Ed25519PrivateKey.generate(), secreto_admin="z" * 40,
                     correo=lambda *a, **k: None, config={})
    return app.extensions["legal"]


def test_plantillas_sembradas_como_borrador_y_nada_rige_sin_aprobacion(lg):
    docs = lg.listar("surtia")
    assert {d["tipo"] for d in docs} == {"contrato", "terminos", "privacidad", "cookies"} and all(d["estado"] == "borrador" for d in docs)
    assert lg.pendientes("surtia", "ana", "responsable") == []                       # sin versión aprobada no se pide aceptar nada


def test_aprobacion_por_la_abogada_y_aceptacion_con_evidencia(lg):
    did = next(d["id"] for d in lg.listar("surtia") if d["tipo"] == "contrato")
    with pytest.raises(LookupError): lg.aprobar(did, "abg")                          # sin abogada registrada
    with pytest.raises(ValueError, match="Inpreabogado"): lg.guardar_abogado("abg", "María Pérez", "V-12345678", "12a")
    lg.guardar_abogado("abg", "María Pérez", "V-12345678", "123456", "maria@abg.test")
    lg.a_revision(did)
    with pytest.raises(ValueError, match="firma"): lg.aprobar(did, "abg")
    with pytest.raises(ValueError, match="PNG"): lg.guardar_firma("abg", b"no es png")
    lg.guardar_firma("abg", PNG)
    doc = lg.aprobar(did, "abg")
    assert doc["estado"] == "aprobado" and doc["abogado"]["inpreabogado"] == "123456" and len(doc["aprobacion"]) == 64
    pend = lg.pendientes("surtia", "admin@orituco", "responsable")
    assert [p["tipo"] for p in pend] == ["contrato"] and lg.pendientes("surtia", "operador", "usuario") == []   # el operador no acepta el contrato
    ids = lg.aceptar("surtia", [pend[0]["id"]], "admin@orituco", "surtia", "Ana", "J-12345678-0", "Orituco", "Administrador", "1.2.3.4", "Firefox")
    assert len(ids) == 1 and lg.pendientes("surtia", "admin@orituco", "responsable") == []
    assert lg.aceptar("surtia", [pend[0]["id"]], "admin@orituco", "surtia") == []     # no se duplica
    assert lg.aceptaciones("surtia")[0]["huella"] == doc["huella"] and lg.cadena_integra()
    # editar la versión aprobada crea la versión 2; al aprobarla, la 1 queda reemplazada y se pide aceptar de nuevo
    secs = [{"titulo": s["titulo"], "parrafos": s["parrafos"]} for s in doc["secciones"]] + [{"titulo": "Nueva", "parrafos": ["Cláusula nueva."]}]
    v2 = lg.guardar(did, secs, "maestro")
    assert v2 != did and lg.documento(did)["estado"] == "aprobado"
    with pytest.raises(ValueError, match="en preparación"): lg.guardar(did, secs, "maestro")
    lg.a_revision(v2); lg.aprobar(v2, "abg")
    assert lg.documento(did)["estado"] == "reemplazado" and lg.vigente("surtia", "contrato")["version"] == 2
    assert [p["version"] for p in lg.pendientes("surtia", "admin@orituco", "responsable")] == [2]
    with pytest.raises(ValueError, match="vigente"): lg.aceptar("surtia", [did], "otro", "surtia")   # la versión vieja ya no se acepta


def test_la_cadena_detecta_una_aceptacion_alterada(lg):
    from sqlalchemy import update
    from licensing_server.legal import aceptaciones
    did = next(d["id"] for d in lg.listar("contax") if d["tipo"] == "terminos")
    lg.guardar_abogado("abg", "María Pérez", "V-12345678", "123456"); lg.guardar_firma("abg", PNG); lg.a_revision(did); lg.aprobar(did, "abg")
    lg.aceptar("contax", [did], "u1", "contax"); lg.aceptar("contax", [did], "u2", "contax")
    with lg.st.eng.begin() as c: c.execute(update(aceptaciones).where(aceptaciones.c.usuario == "u1").values(ip="9.9.9.9"))
    assert not lg.cadena_integra()
