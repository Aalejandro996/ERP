"""Cálculo de facturas (Venezuela) y planes de licencia: lógica pura, sin base de datos."""
from decimal import Decimal as D

from app.calculos import facturacion as F
from app.calculos.facturacion import ErrorFactura
from app.planes import MODULOS_LEGADO, PLANES, modulos_de, permiso_habilitado

PCT = {"G": D("16"), "R": D("8"), "A": D("15")}


def lanza(exc, f, *a, **k):
    try: f(*a, **k)
    except exc: return True
    return False


def L(desc="Item", cant="1", precio="0", alic="G", dpct="0"):
    return dict(descripcion=desc, cantidad=cant, precio=precio, alicuota=alic, descuento_pct=dpct)


# ---------- planes ----------
def test_planes_segun_licencia():
    assert modulos_de({"plan": "ERP_CONTABLE"}) == {"contabilidad", "administrativo", "auditoria"}
    assert modulos_de({"plan": "CASH"}) == {"facturacion", "inventario"}
    assert modulos_de({"plan": "ERP_ADMIN"}) == {"administrativo", "inventario"}
    assert modulos_de({"plan": "ALL_IN_ONE"}) == {"contabilidad", "administrativo", "auditoria", "inventario", "facturacion"}
    assert modulos_de({"cliente": "x"}) == MODULOS_LEGADO          # token anterior a los planes: no pierde funciones
    assert modulos_de({"plan": "INVENTADO"}) == frozenset()        # plan desconocido: falla cerrado
    assert modulos_de(None) == frozenset()
    assert set(PLANES) == {"ERP_CONTABLE", "CASH", "ERP_ADMIN", "ALL_IN_ONE", "PERSONALIZADO"}
    assert "auditoria" in PLANES["ERP_CONTABLE"]["modulos"] and "auditoria" not in PLANES["ERP_ADMIN"]["modulos"]


def test_permisos_filtrados_por_modulo():
    cash = modulos_de({"plan": "CASH"})
    assert permiso_habilitado("facturacion.emitir", cash) and permiso_habilitado("inventario.ver", cash)
    assert not permiso_habilitado("asientos.ver", cash) and not permiso_habilitado("auditoria.ver", cash)
    assert not permiso_habilitado("cartera.ver", cash)
    assert permiso_habilitado("admin", cash)                        # administración de usuarios: núcleo, siempre


# ---------- cálculo ----------
def test_factura_mixta_en_bolivares():
    r = F.calcular([L("A", "2", "100", "G"), L("B", "1", "50", "R"), L("C", "1", "30", "E"), L("D", "1", "200", "A", "10")], PCT)
    assert (r["base_g"], r["iva_g"]) == (D("200.00"), D("32.00"))
    assert (r["base_r"], r["iva_r"]) == (D("50.00"), D("4.00"))
    assert r["exento"] == D("30.00")
    assert (r["base_a"], r["iva_a"]) == (D("180.00"), D("55.80"))   # lujo: 16 % + 15 % = 31 % sobre 180 (10 % de descuento)
    assert r["descuento"] == D("20.00")
    assert r["total"] == D("551.80")
    assert r["total_divisa"] is None


def test_factura_en_divisas_convierte_a_bs():
    r = F.calcular([L("Servicio", "1", "100", "G")], PCT, "USD", "36.5")
    assert r["base_g"] == D("3650.00") and r["iva_g"] == D("584.00") and r["total"] == D("4234.00")
    assert r["total_divisa"] == D("116.00")


def test_validaciones_de_lineas():
    assert lanza(ErrorFactura, F.calcular, [], PCT)
    assert lanza(ErrorFactura, F.calcular, [L(cant="0", precio="1")], PCT)
    assert lanza(ErrorFactura, F.calcular, [L(precio="-1")], PCT)
    assert lanza(ErrorFactura, F.calcular, [L(precio="1", alic="X")], PCT)
    assert lanza(ErrorFactura, F.calcular, [L(desc="", precio="1")], PCT)
    assert lanza(ErrorFactura, F.calcular, [L(precio="1", dpct="101")], PCT)
    assert lanza(ErrorFactura, F.calcular, [L(precio=1.5)], PCT)          # float prohibido
    assert lanza(ErrorFactura, F.calcular, [L(precio="1")], PCT, "USD", "0")
    assert lanza(ErrorFactura, F.calcular, [L(precio="1")], {"G": 16})   # faltan alícuotas


def test_pagos_e_igtf():
    total = D("4234.00")
    pagos, base, igtf = F.aplicar_pagos(total, [dict(forma="Zelle", moneda="USD", monto="100"), dict(forma="Pago móvil", moneda="VES", monto="584")], "36.5", "3", True)
    assert base == D("3650.00") and igtf == D("109.50")
    assert sum(p["monto_bs"] for p in pagos) == total
    _, base, igtf = F.aplicar_pagos(total, [dict(forma="Efectivo", moneda="USD", monto="116")], "36.5", "3", False)
    assert igtf == 0                                                      # no es SPE: no percibe IGTF
    assert lanza(ErrorFactura, F.aplicar_pagos, total, [dict(forma="Efectivo", moneda="VES", monto="4000")], "1")
    assert lanza(ErrorFactura, F.aplicar_pagos, total, [], "1")
    assert lanza(ErrorFactura, F.aplicar_pagos, total, [dict(forma="Trueque", moneda="VES", monto="4234")], "1")


def test_redondeo_de_conversion_se_absorbe():
    # 33.33 USD × 36.51 = 1216.8783 → 1216.88; el total en Bs puede diferir 1 céntimo
    pagos, _, _ = F.aplicar_pagos(D("1216.87"), [dict(forma="Zelle", moneda="USD", monto="33.33")], "36.51")
    assert pagos[0]["monto_bs"] == D("1216.87")


def test_asientos_de_documento():
    cfg = {"cuenta_caja_banco": "1.1", "cuenta_cxc": "1.2", "cuenta_ventas": "4.1", "cuenta_iva_debito_fiscal": "2.1", "cuenta_igtf_por_pagar": "2.2"}
    fac = F.asiento_documento(cfg, "FAC", "Contado", D("100"), D("16"), D("3.48"))
    assert fac == [("1.1", D("119.48"), 0), ("4.1", 0, D("100.00")), ("2.1", 0, D("16.00")), ("2.2", 0, D("3.48"))]
    assert sum(d for _, d, _ in fac) == sum(h for _, _, h in fac)
    nc = F.asiento_documento(cfg, "NC", "Credito", D("50"), D("8"))
    assert nc == [("1.2", 0, D("58.00")), ("4.1", D("50.00"), 0), ("2.1", D("8.00"), 0)]
    assert F.claves_asiento("Credito", D("8"), 0) == ["ventas", "cxc", "iva_debito_fiscal"]


def test_cadena_y_formatos():
    d = dict(tipo="FAC", serie="A", numero=1, numero_control="00-00000001", fecha_emision="2026-10-26", cliente_rif="J-12345678-9", total=D("10"), igtf=D("0"))
    h1 = F.hash_documento("0" * 64, d)
    assert h1 != F.hash_documento("0" * 64, {**d, "total": D("10.01")}) and len(h1) == 64
    assert F.numero_fmt(12) == "00000012" and F.control_fmt("00", 7) == "00-00000007"


def test_usdt_y_pagos_en_otra_divisa_usan_su_propia_tasa():
    r = F.calcular([L("Servicio", "1", "100", "G")], PCT, "USDT", "36.5")
    assert r["moneda"] == "USDT" and r["total"] == D("4234.00") and r["total_divisa"] == D("116.00")
    # factura en bolívares pagada en dólares y USDT: cada pago con SU tasa, e IGTF sobre ambos
    pagos, base, igtf = F.aplicar_pagos(D("4234.00"), [dict(forma="Zelle", moneda="USD", monto="100"), dict(forma="Otro", moneda="USDT", monto="16")],
                                        "1", "3", True, {"USD": "36.5", "USDT": "36.5"})
    assert [p["monto_bs"] for p in pagos] == [D("3650.00"), D("584.00")] and igtf == D("127.02")
    assert lanza(ErrorFactura, F.aplicar_pagos, D("100"), [dict(forma="Zelle", moneda="EUR", monto="10")], "1", 0, False, {"USD": "36.5"})  # sin tasa del euro


def test_paginacion_de_tablas():
    from flask import Flask
    from app.web import POR_PAGINA, paginar
    app = Flask(__name__)
    app.add_url_rule("/lista", "lista", lambda: "")
    filas = [dict(cells=[i]) for i in range(60)] + [dict(cells=["Total"], clase="total")]
    with app.test_request_context("/lista?pag1=3&q=x"):
        t = paginar(dict(cols=["N"], filas=filas), "pag1")
        assert [f["cells"][0] for f in t["filas"]] == list(range(50, 60)) + ["Total"]     # la fila de total se mantiene al pie
        assert t["pag"]["texto"] == "51–60 de 60" and t["pag"]["sig"] is None and "q=x" in t["pag"]["ant"] and "pag1=2" in t["pag"]["ant"]
    with app.test_request_context("/lista?pag1=todo"):
        assert len(paginar(dict(cols=["N"], filas=filas), "pag1")["filas"]) == 61
    with app.test_request_context("/lista?pag1=999"):
        assert paginar(dict(cols=["N"], filas=filas), "pag1")["pag"]["texto"] == "51–60 de 60"   # fuera de rango: última
    with app.test_request_context("/lista"):
        assert "pag" not in paginar(dict(cols=["N"], filas=filas[:POR_PAGINA]), "pag1")
