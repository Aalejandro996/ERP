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


def _directores(lg):
    lg.guardar_director("dir1", "Adrián Muñoz", "V-11111111", "Director General", "dir1@gigadevs.test"); lg.guardar_firma("dir1", PNG)
    lg.guardar_director("dir2", "Soranyer Ávila", "V-10807720", "Directora General", "dir2@gigadevs.test"); lg.guardar_firma("dir2", PNG)


def _autorizar_todo(lg, doc):
    for e in doc["enlaces"]: lg.autorizar(e["token"], "autorizado", "1.1.1.1", "Firefox")


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
    with pytest.raises(ValueError, match="Directores Generales"): lg.aprobar(did, "abg")      # sin los dos directores no se aprueba
    _directores(lg)
    with pytest.raises(ValueError, match="Ya hay 2"): lg.guardar_director("dir3", "Otra Persona", "V-22222222", "Director", "x@y.test")
    doc = lg.aprobar(did, "abg")
    assert doc["estado"] == "autorizacion" and len(doc["enlaces"]) == 2 and lg.pendientes("surtia", "admin@orituco", "responsable") == []   # aún no rige
    ctx = lg.autorizacion(doc["enlaces"][0]["token"])
    assert ctx["estado"] == "pendiente" and ctx["director"]["nombre"] == "Adrián Muñoz" and ctx["documento"]["huella"] == doc["huella"]
    lg.autorizar(doc["enlaces"][0]["token"], "autorizado", "1.1.1.1", "Firefox")
    with pytest.raises(ValueError, match="ya se usó"): lg.autorizar(doc["enlaces"][0]["token"], "autorizado")
    assert lg.documento(did)["estado"] == "autorizacion"                                        # falta el segundo director
    lg.autorizar(doc["enlaces"][1]["token"], "autorizado", "2.2.2.2", "Chrome")
    doc = lg.documento(did)
    assert doc["estado"] == "aprobado" and doc["abogado"]["inpreabogado"] == "123456" and len(doc["aprobacion"]) == 64
    assert [a["estado"] for a in doc["autorizaciones"]] == ["autorizado", "autorizado"] and all(a["firma"] and a["evidencia"] for a in doc["autorizaciones"])
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
    lg.a_revision(v2); _autorizar_todo(lg, lg.aprobar(v2, "abg"))
    assert lg.documento(did)["estado"] == "reemplazado" and lg.vigente("surtia", "contrato")["version"] == 2
    assert [p["version"] for p in lg.pendientes("surtia", "admin@orituco", "responsable")] == [2]
    with pytest.raises(ValueError, match="vigente"): lg.aceptar("surtia", [did], "otro", "surtia")   # la versión vieja ya no se acepta


def test_la_cadena_detecta_una_aceptacion_alterada(lg):
    from sqlalchemy import update
    from licensing_server.legal import aceptaciones
    did = next(d["id"] for d in lg.listar("contax") if d["tipo"] == "terminos")
    lg.guardar_abogado("abg", "María Pérez", "V-12345678", "123456"); lg.guardar_firma("abg", PNG); _directores(lg); lg.a_revision(did)
    _autorizar_todo(lg, lg.aprobar(did, "abg"))
    lg.aceptar("contax", [did], "u1", "contax"); lg.aceptar("contax", [did], "u2", "contax")
    with lg.st.eng.begin() as c: c.execute(update(aceptaciones).where(aceptaciones.c.usuario == "u1").values(ip="9.9.9.9"))
    assert not lg.cadena_integra()


def test_si_un_director_no_autoriza_vuelve_a_borrador(lg):
    did = next(d["id"] for d in lg.listar("criteriseg") if d["tipo"] == "terminos")
    lg.guardar_abogado("abg", "María Pérez", "V-12345678", "123456"); lg.guardar_firma("abg", PNG); _directores(lg); lg.a_revision(did)
    doc = lg.aprobar(did, "abg")
    with pytest.raises(ValueError, match="motivo"): lg.autorizar(doc["enlaces"][0]["token"], "rechazado")
    assert lg.autorizar(doc["enlaces"][0]["token"], "rechazado", comentario="Falta la cláusula de arbitraje")["estado"] == "borrador"
    d = lg.documento(did)
    assert d["estado"] == "borrador" and "Falta la cláusula de arbitraje" in d["observaciones"] and [a["estado"] for a in d["autorizaciones"]] == ["rechazado", "anulado"]
    with pytest.raises(ValueError, match="ya se usó"): lg.autorizar(doc["enlaces"][1]["token"], "autorizado")


def test_sin_abogada_con_inpreabogado_ningun_documento_rige(lg):
    """Defensa adicional: aunque un documento quedara «aprobado» sin abogada (p. ej. por una edición manual de la base), no rige
    ni se le pide a nadie aceptarlo."""
    from sqlalchemy import update
    import licensing_server.legal as LG
    did = next(d["id"] for d in lg.listar("surtia") if d["tipo"] == "contrato")
    with lg.st.eng.begin() as c:
        c.execute(update(LG.documentos).where(LG.documentos.c.id == did).values(estado="aprobado", abogado_id=None))
    d = lg.documento(did)
    assert lg.vigente(d["sistema"], d["tipo"]) is None
    assert all(p["id"] != did for p in lg.pendientes(d["sistema"], "ana@x.test", "responsable"))
