import io
from decimal import Decimal

import pytest

from app import lector_pedidos as LP
from app.calculos import migracion as MG


def _xlsx(hojas):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook(); wb.remove(wb.active)
    for nombre, filas in hojas.items():
        ws = wb.create_sheet(nombre)
        for f in filas: ws.append(f)
    b = io.BytesIO(); wb.save(b); return b.getvalue()


def test_profit_plus_varias_hojas_con_titulo_arriba():
    datos = _xlsx({"Clientes": [["Listado de clientes"], [], ["co_cli", "cli_des", "rif", "direc1", "telefonos"],
                                ["C001", "Bodega La Esquina", "j123456789", "Valencia", "0414-1112233"], ["C002", "Ana Pérez", "V-14.555.666", "", ""],
                                ["C003", "Sin rif", "xx", "", ""]],
                   "Articulos": [["co_art", "art_des", "stock_act", "cos_pro_un", "prec_vta1", "tipo_imp"], ["A001", "Harina PAN", "1.250,5", "40,10", "45,50", "1"]]})
    hojas = MG.leer_hojas(datos, "export_profit.xlsx")
    det = MG.detectar(hojas[0][1], hojas[0][0])
    assert det["tipo"] == "clientes" and det["sistema"] == "Profit Plus" and det["fila_cab"] == 1
    filas = MG.normalizar("clientes", hojas[0][1], det["fila_cab"], det["columnas"])
    assert [f["estado"] for f in filas] == ["ok", "ok", "error"]
    assert filas[0]["datos"]["rif"] == "J-12345678-9" and filas[1]["datos"]["rif"] == "V-14555666" and "RIF" in filas[2]["obs"]
    det = MG.detectar(hojas[1][1], hojas[1][0])
    assert det["tipo"] == "productos"
    p = MG.normalizar("productos", hojas[1][1], det["fila_cab"], det["columnas"])[0]["datos"]
    assert p == dict(codigo="A001", nombre="Harina PAN", existencia="1250.5", costo="40.10", precio="45.50", alicuota="G")


def test_galac_proveedores_y_saldos_por_cobrar_csv():
    csv_ = "Código del Proveedor;Nombre del Proveedor;Número de RIF;Dirección\nP1;Distribuidora Uno;J-98765432-1;Caracas\n".encode("cp1252")
    hojas = MG.leer_hojas(csv_, "Proveedores.csv")
    det = MG.detectar(hojas[0][1], "Proveedores")
    assert det["tipo"] == "proveedores" and det["sistema"] == "Galac"
    cxc = "Documento;Cliente;RIF;Fecha;Vencimiento;Monto;Saldo\nF-10;Bodega;J-12345678-9;05/09/2026;05/10/2026;1.000,00;400,00\nF-11;Otro;;01/09/2026;;50;0\n"
    filas = MG.leer_hojas(cxc.encode(), "cuentas por cobrar.csv")[0][1]
    det = MG.detectar(filas, "cuentas por cobrar")
    assert det["tipo"] == "cxc"
    r = MG.normalizar("cxc", filas, det["fila_cab"], det["columnas"])
    assert r[0]["datos"]["saldo"] == "400.00" and r[0]["datos"]["emision"] == "2026-09-05" and r[1]["estado"] == "error"


def _pdf(lineas):
    canvas = pytest.importorskip("reportlab.pdfgen.canvas")
    b = io.BytesIO(); c = canvas.Canvas(b); y = 800
    for l in lineas: c.drawString(40, y, l); y -= 18
    c.save(); return b.getvalue()


CAT = {"A001": ("Harina PAN 1kg", Decimal("45.50"), Decimal("10")), "B-77": ("Aceite Vatel 1L", Decimal("120"), Decimal("2")),
       "C9": ("Arroz Mary 1kg", Decimal("30"), Decimal("0"))}


def test_lee_la_orden_de_compra_en_pdf_y_ajusta_a_la_existencia():
    pdf = _pdf(["ORDEN DE COMPRA N° OC-778", "Comercial Beta, C.A.  RIF: J-12345678-9", "Código  Descripción  Cant.  Precio",
                "A001 Harina PAN 1kg 12 UND 45,00", "Aceite vatel 1l   5   118,00", "Arroz Mary 1kg 3", "Servilletas finas 4 paquetes", "TOTAL 1.200,00"])
    r = LP.interpretar(LP.texto_pdf(pdf), CAT)
    assert r["rif"] == "J-12345678-9" and r["referencia"] == "OC-778"
    assert {l["codigo"]: l["solicitado"] for l in r["lineas"]} == {"A001": "12", "B-77": "5", "C9": "3"}
    assert any("Servilletas" in x for x in r["no_identificadas"])
    lineas, faltan = LP.despachable(r["lineas"], CAT)
    assert [(l["codigo"], l["cantidad"]) for l in lineas] == [("A001", "10"), ("B-77", "2")]
    assert len(faltan) == 3 and any("C9" in f for f in faltan)
    with pytest.raises(LP.ErrorLectura): LP.texto_pdf(b"no es pdf")
