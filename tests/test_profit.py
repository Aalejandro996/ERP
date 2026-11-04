"""Integración con Profit Plus: firma del conector, validación de la foto, armado de la foto en el conector y búsqueda del bot."""
import gzip
import json
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app import profit as PF
from app import whatsapp_bot as B

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "conector_profit"))
import contax_profit as CP  # noqa: E402


def test_firma_del_conector_coincide_con_la_del_servidor_y_vence():
    cuerpo = gzip.compress(b'{"articulos":[],"clientes":[]}')
    f = CP.firmar("secreto", 1_800_000_000, cuerpo)
    assert f == PF.firmar("secreto", 1_800_000_000, cuerpo)
    assert PF.firma_valida("secreto", "1800000000", cuerpo, f, ahora=1_800_000_100)
    assert not PF.firma_valida("secreto", "1800000000", cuerpo, f, ahora=1_800_000_400)          # más de 5 min: no vale
    assert not PF.firma_valida("otro", "1800000000", cuerpo, f, ahora=1_800_000_000)
    assert not PF.firma_valida("secreto", "1800000000", cuerpo + b"x", f, ahora=1_800_000_000)   # cuerpo alterado
    assert not PF.firma_valida("secreto", "abc", cuerpo, f) and not PF.firma_valida("", "1800000000", cuerpo, f)
    assert PF.descomprimir(cuerpo, "gzip") == {"articulos": [], "clientes": []}
    with pytest.raises(PF.ErrorProfit): PF.descomprimir(b"no-gzip", "gzip")
    with pytest.raises(PF.ErrorProfit): PF.descomprimir(b"[1]", "")


def test_normaliza_rif_telefonos_y_monedas_de_profit():
    assert PF.rif("J123456789") == "J-12345678-9" and PF.rif(" j-12345678-9 ") == "J-12345678-9" and PF.rif("123") is None
    assert PF.whatsapp_de("0212-1234567 / 0414-777.88.99") == "584147778899"
    assert PF.whatsapp_de("+58 424 1112233") == "584241112233" and PF.whatsapp_de("0212-1234567") is None
    assert PF.moneda("US$") == "USD" and PF.moneda("BS") == "VES" and PF.moneda("") == "VES" and CP.moneda("USD") == "USD"


def test_validar_foto_limpia_y_rechaza():
    foto = PF.validar_foto({"version": "1.0.0", "equipo": "SRV", "articulos": [
        {"codigo": " A-1 ", "nombre": "Harina  PAN", "alicuota": "g", "precio": "45,5", "moneda": "VES", "existencia": "-3", "por_almacen": {"01": "-3"}},
        {"codigo": "A-1", "nombre": "duplicado"}, {"codigo": "CON ESPACIO", "nombre": "no se puede pedir por chat"},
        {"codigo": "B/2", "nombre": "Aceite", "alicuota": "X", "precio": "3.5", "moneda": "US$"}],
        "clientes": [{"codigo": "C1", "nombre": "Orituco", "rif": "J123456789", "telefonos": "0414-7778899"}, {"codigo": "C1"}],
        "disponibles": {"almacenes": [{"codigo": "01", "nombre": "Principal"}], "listas_precio": []}})
    a, b = foto["articulos"]
    assert (a["codigo"], a["nombre"], a["alicuota"], a["precio"], a["existencia"]) == ("A-1", "Harina PAN", "G", Decimal("45.5000"), Decimal(0))
    assert (b["codigo"], b["alicuota"], b["moneda"]) == ("B/2", "G", "USD")
    assert foto["clientes"] == [{"codigo": "C1", "nombre": "Orituco", "rif": "J-12345678-9", "direccion": None, "telefonos": "0414-7778899",
                                 "whatsapp": "584147778899", "email": None}]
    assert foto["disponibles"]["almacenes"] == [{"codigo": "01", "nombre": "Principal"}] and foto["version"] == "1.0.0"
    with pytest.raises(PF.ErrorProfit): PF.validar_foto({"articulos": "x", "clientes": []})
    with pytest.raises(PF.ErrorProfit): PF.validar_foto({"articulos": [{}] * 3, "clientes": []}, max_articulos=2)
    with pytest.raises(PF.ErrorProfit): PF.validar_foto({"articulos": [{"codigo": "A", "precio": "abc"}], "clientes": []})


def test_conector_arma_la_foto_con_existencia_por_almacen_y_precio_vigente():
    foto = CP.construir_foto(
        almacenes_rows=[("01", "PRINCIPAL", False), ("02", "DAÑADOS", True), ("03", "SUCURSAL", False)],
        listas_rows=[("01", "DETAL")],
        articulos_rows=[("HAR", "HARINA", "ALIM", "1"), ("LEC", "LECHE", "ALIM", "7"), ("SIN", "SIN PRECIO", None, None)],
        stock_rows=[("HAR", "01", "ACT", 50), ("HAR", "01", "COM", 10), ("HAR", "02", "ACT", 99), ("HAR", "03", "ACT", 5), ("LEC", "01", "ACT", 2), ("LEC", "01", "LLE", 100)],
        precios_rows=[("HAR", "03", Decimal("50"), "BS", datetime(2026, 5, 1)), ("HAR", "", Decimal("40"), "BS", datetime(2026, 1, 1)),
                      ("HAR", "", Decimal("45.5"), "BS", datetime(2026, 3, 1)), ("LEC", "01", Decimal("2.1"), "US$", datetime(2026, 1, 1))],
        clientes_rows=[("C1", "ORITUCO", "J123456789", "Altagracia", "0414-7778899", None)],
        config={"almacenes": [], "lista_precio": "01", "restar_comprometido": True}, alicuotas={"1": "G", "7": "E"})
    har, lec, sin = foto["articulos"]
    assert har == {"codigo": "HAR", "nombre": "HARINA", "linea": "ALIM", "alicuota": "G", "precio": "45.5", "moneda": "VES", "existencia": "45",
                   "por_almacen": {"01": "40", "03": "5"}}                        # el almacén «no venta» no cuenta; general y más reciente
    assert (lec["alicuota"], lec["precio"], lec["moneda"], lec["existencia"]) == ("E", "2.1", "USD", "2")   # «por llegar» no suma
    assert (sin["precio"], sin["existencia"], sin["alicuota"]) == ("0", "0", "G")
    assert foto["config_aplicada"]["almacenes"] == ["01", "03"] and foto["disponibles"]["listas_precio"] == [{"codigo": "01", "nombre": "DETAL"}]
    solo = CP.construir_foto([("01", "P", False), ("03", "S", False)], [], [("HAR", "HARINA", "", "1")], [("HAR", "01", "ACT", 50), ("HAR", "03", "ACT", 5)],
                             [], [], config={"almacenes": ["03"], "restar_comprometido": False}, alicuotas={})
    assert solo["articulos"][0]["existencia"] == "5"
    json.dumps(foto)                                                                   # serializable tal cual
    assert PF.validar_foto(foto)["articulos"][0]["existencia"] == Decimal("45")       # el servidor acepta lo que arma el conector


def test_conector_respeta_el_intervalo_que_fija_contax():
    assert CP.toca_sincronizar({}, 1000)
    assert not CP.toca_sincronizar({"ultima_sync": 1000, "config": {"intervalo_min": 15}}, 1000 + 600)
    assert CP.toca_sincronizar({"ultima_sync": 1000, "config": {"intervalo_min": 15}}, 1000 + 880)


def test_conector_exige_ini_completo(tmp_path):
    with pytest.raises(CP.ErrorConector): CP.leer_ini(tmp_path / "no.ini")
    p = tmp_path / "c.ini"
    p.write_text("[contax]\nurl = http://contax.test\nconector = pp_1\nclave = k\n[sqlserver]\nservidor = s\nbase_datos = b\n", encoding="utf-8")
    with pytest.raises(CP.ErrorConector, match="https"): CP.leer_ini(p)
    p.write_text("[contax]\nurl = https://contax.test\nconector = pp_1\nclave = k\n[sqlserver]\nservidor = s ; comentario\nbase_datos =\n", encoding="utf-8")
    with pytest.raises(CP.ErrorConector, match="base_datos"): CP.leer_ini(p)


CAT = {"HAR-01": ("HARINA PAN 1KG", Decimal("45.50"), Decimal("40")), "ARR-02": ("ARROZ MARY 1KG", Decimal("38"), Decimal("0")),
       "HAR-02": ("Harina de trigo Robin Hood", Decimal("52"), Decimal("3")), **{f"X{i:03d}": (f"Articulo {i}", Decimal(1), Decimal(1)) for i in range(60)}}


def test_bot_busca_por_nombre_en_catalogos_grandes():
    r, _, _ = B.responder("1", "cliente", {}, CAT, "Orituco")
    assert "Mostrando 40 de 63" in r[0]
    r, _, ev = B.responder("harina", "cliente", {}, CAT, "Orituco")
    assert "HAR-01" in r[0] and "HAR-02" in r[0] and "ARR-02" not in r[0] and ev == []
    r, _, _ = B.responder("buscar harina pan", "cliente", {}, CAT, "Orituco")
    assert "HAR-01" in r[0] and "HAR-02" not in r[0]
    assert "agotado" in B.responder("arroz", "cliente", {}, CAT, "Orituco")[0][0]
    assert "No encontramos productos con «pintura»" in B.responder("buscar pintura", "cliente", {}, CAT, "Orituco")[0][0]
    assert "No entendí" in B.responder("qwzx", "cliente", {}, CAT, "Orituco")[0][0]
    _, est, _ = B.responder("har-01 3", "cliente", {}, CAT, "Orituco")
    assert est["carrito"] == {"HAR-01": "3"}
    cat = {"B/2": ("Aceite", Decimal("10"), Decimal("5"))}
    assert B.responder("b/2 2", "cliente", {}, cat, "X")[1]["carrito"] == {"B/2": "2"}                    # códigos de Profit con «/»
