import io

import pytest

from app.calculos import plan_cuentas as PC


def test_csv_con_encabezado_en_cualquier_orden():
    txt = "Descripción;Código;Naturaleza;Monetaria\nCaja principal;1.1.01;Activo;Si\nProveedores;2.1.01;Pasivo;\nIntereses bancarios;6.2.01;Gasto;No\n"
    cs, err = PC.interpretar(PC.leer_filas(txt.encode("cp1252")))
    assert err == [] and [c["codigo"] for c in cs] == ["1.1.01", "2.1.01", "6.2.01"]
    assert cs[0] == dict(codigo="1.1.01", nombre="Caja principal", tipo="Activo", categoria_niif18="Operacion", es_partida_monetaria=True)
    assert cs[1]["es_partida_monetaria"] is False and cs[2]["categoria_niif18"] == "Financiamiento"


def test_sin_encabezado_el_tipo_se_deduce_del_codigo():
    cs, err = PC.interpretar(PC.leer_filas("1,ACTIVO\n3.1.01,Capital social\n4.1.01,Ventas\n5.1.01,Costo de ventas\n6.3.01,Impuesto sobre la renta\n"))
    assert [c["tipo"] for c in cs] == ["Activo", "Patrimonio", "Ingreso", "Gasto", "Gasto"] and err == []
    assert cs[-1]["categoria_niif18"] == "Impuestos"


def test_lineas_sin_delimitador_y_errores_por_linea():
    cs, err = PC.interpretar(PC.leer_filas("1.1.01 Caja\n1.1.01 Caja repetida\n¿? Basura\nA.1 Cuenta sin tipo\n"))
    assert [c["codigo"] for c in cs] == ["1.1.01"]
    assert any("repetido" in e for e in err) and any("código inválido" in e for e in err) and any("tipo" in e for e in err)


def test_excel():
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["Cuenta", "Nombre de la cuenta", "Tipo"]); ws.append([1101, "Banco Venezuela", "ACTIVO"]); ws.append(["2101", "IVA débito fiscal", "pasivo"])
    buf = io.BytesIO(); wb.save(buf)
    cs, err = PC.interpretar(PC.leer_filas(buf.getvalue(), "plan.xlsx"))
    assert err == [] and cs[0]["codigo"] == "1101" and cs[0]["es_partida_monetaria"] and cs[1]["tipo"] == "Pasivo"


def test_plan_modelo_y_vacio():
    cs, err = PC.interpretar(PC.leer_filas(PC.MODELO_VEN_NIF))
    assert err == [] and len(cs) > 50 and {c["tipo"] for c in cs} == set(PC.TIPOS)
    with pytest.raises(PC.ErrorImportacion): PC.interpretar(PC.leer_filas("\n\n"))
