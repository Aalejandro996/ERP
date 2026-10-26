import time
from datetime import date
from decimal import Decimal as D

from app import cifrado, totp
from app.calculos import cartera, estados, fiscal, inventario
from app.calculos.inventario import ErrorInventario, Lote
from app.calculos.fiscal import ErrorFiscal


def lanza(exc, f, *a, **k):
    try: f(*a, **k)
    except exc: return True
    return False


# ---------- inventario ----------
def test_promedio_ponderado():
    c, p = inventario.entrada_promedio(10, "5", 10, "7")
    assert (c, p) == (D(20), D("6.0000"))
    total, u = inventario.salida_promedio(c, p, 5)
    assert total == D("30.00") and u == D("6.0000")
    assert lanza(ErrorInventario, inventario.salida_promedio, c, p, 21)


def test_peps_consume_lotes_antiguos_primero():
    lotes = [Lote("2026-02-01", D(10), D(8), 2), Lote("2026-01-01", D(10), D(5), 1)]
    total, u, cons = inventario.salida_peps(lotes, 15)
    assert total == D("90.00")          # 10*5 + 5*8
    assert u == D("6.0000")
    assert [(l.id, q) for l, q in cons] == [(1, D(10)), (2, D(5))]
    assert lanza(ErrorInventario, inventario.salida_peps, lotes, 21)


# ---------- fiscal ----------
TARIFA = [(0, 2000, 15, 0), (2000.0001, 3000, 22, 140), (3000.0001, 10**9, 34, 500)]


def test_tarifa_islr():
    r = fiscal.calcular_tarifa_islr(D("100000"), D("9"), TARIFA)   # 11111.11 UT -> 34%
    assert r["porcentaje_aplicado"] == 34
    assert r["islr_ut"] == (D("100000") / 9 * D("0.34") - 500).quantize(D("0.01"))
    assert lanza(ErrorFiscal, fiscal.calcular_tarifa_islr, 100, 0, TARIFA)


def test_retencion_islr_minimo_y_sustraendo():
    bajo = fiscal.calcular_retencion_islr(80, "Juridica", 2, 3, 0, 10, 9)
    assert bajo["monto_retenido"] == 0
    ok = fiscal.calcular_retencion_islr(1000, "Natural", 2, 3, 1, 10, 9)
    assert ok["monto_retenido"] == D("21.00")                      # 1000*3% - 1*9
    assert lanza(ErrorFiscal, fiscal.calcular_retencion_islr, 1000, "X", 2, 3, 1, 10, 9)


def test_inflacion_y_spe():
    r = fiscal.reajuste_inflacion(100, 125, 1000, 400)
    assert r["ajuste_activos_no_monetarios"] == D("250.00")
    assert r["ajuste_patrimonio"] == D("100.00")
    assert r["reajuste_regular_por_inflacion"] == D("150.00")
    assert lanza(ErrorFiscal, fiscal.reajuste_inflacion, 100, 125, 1000, 400, True)
    assert lanza(ErrorFiscal, fiscal.reajuste_inflacion, 0, 125, 1000, 400)


def test_islr_completo_y_perdida():
    perdida = fiscal.islr_completo(-500, 0, 0, 0, 9, TARIFA, [])
    assert perdida["islr_neto_a_pagar"] == 0
    r = fiscal.islr_completo(90000, 5000, 1000, 6000, 9, TARIFA, [100])
    assert r["enriquecimiento_neto_gravable"] == D("100000.00")
    assert r["islr_neto_a_pagar"] == r["islr_determinado_bs"] - 100


def test_txt_iva_16_columnas():
    f = dict(rif_contribuyente="J-1", periodo_impositivo="202610", fecha_documento="2026-10-01", tipo_operacion="C",
             tipo_documento="01", rif_proveedor="J-2", numero_documento="1", numero_control="00-1",
             monto_documento="116", base_imponible=100, monto_iva="16", numero_documento_afectado="0",
             numero_comprobante="2026100000001", monto_exento="0", alicuota="16", numero_expediente="0")
    linea = fiscal.linea_txt_retencion_iva(f)
    cols = linea.split("\t")
    assert len(cols) == 16 and cols[8] == "116.00" and cols[14] == "16.00"
    f["rif_proveedor"] = "J-2\tX"
    assert lanza(ErrorFiscal, fiscal.linea_txt_retencion_iva, f)    # inyección de columnas


# ---------- cartera ----------
def test_provision_incobrables():
    docs = [(1, "A", "F1", date(2026, 9, 1), "1000"), (2, "B", "F2", date(2026, 5, 1), "500")]
    tramos = [(0, 30, 1), (31, 90, 5), (91, 10**6, 50)]
    det, tot = cartera.provision(docs, tramos, date(2026, 10, 1))   # 30 días y 153 días
    assert det[0][5] == 1 and det[1][5] == 50 and tot == D("260.00")


def test_asientos_cuadran():
    cfg = dict(cuenta_cxc="1.2", cuenta_ventas="4.1", cuenta_iva_debito_fiscal="2.1", cuenta_cxp="2.2",
               cuenta_compras_gastos="5.1", cuenta_iva_credito_fiscal="1.3", cuenta_caja_banco="1.1")
    for ls in (cartera.asiento_venta(cfg, "116", "100", "16"), cartera.asiento_compra(cfg, "116", "100", "16"),
               cartera.asiento_cobro(cfg, "50"), cartera.asiento_pago(cfg, "50")):
        assert sum(D(str(l[1])) for l in ls) == sum(D(str(l[2])) for l in ls)
    assert lanza(ValueError, cartera.asiento_venta, cfg, "116", "100", "10")   # base+IVA != total


# ---------- estados financieros ----------
def _rows():
    f = lambda c, n, t, d, h, cat="Operacion", m=False: dict(codigo=c, nombre=n, tipo=t, debe=d, haber=h, categoria=cat, monetaria=m)
    return [f("1.1", "Caja", "Activo", 2000, 300, m=True), f("1.5", "Mobiliario", "Activo", 400, 0),
            f("2.1", "Proveedores", "Pasivo", 0, 600), f("3.1", "Capital", "Patrimonio", 0, 1000),
            f("4.1", "Ventas", "Ingreso", 0, 800), f("5.1", "Gastos", "Gasto", 200, 0),
            f("5.9", "Intereses", "Gasto", 100, 0, "Financiamiento")]


def test_balance_cuadra_y_resultados():
    b = estados.balance(_rows())
    assert b["cuadra"] and b["utilidad"] == D(500)
    nic = estados.estado_resultados(_rows(), "2026-12-31")
    assert nic["normativa"] == "NIC 1" and nic["utilidad_del_ejercicio"] == D(500)
    n18 = estados.estado_resultados(_rows(), "2027-03-01")
    assert n18["normativa"] == "NIIF 18" and n18["resultado_operativo"] == D(600) and n18["resultado_del_periodo"] == D(500)


def test_cierre_y_bases_inflacion():
    ls = estados.lineas_cierre(_rows(), "3.2")
    assert sum(l[1] for l in ls) == sum(l[2] for l in ls)
    assert estados.lineas_cierre([r for r in _rows() if r["tipo"] == "Activo"], "3.2") == []
    anm, pat = estados.bases_inflacion(_rows())
    assert anm == D(400) and pat == D(1000)


def test_flujo():
    fl = estados.flujo_efectivo({"Operacion": "100", "Inversion": "-40"})
    assert fl["flujo_neto_periodo"] == D(60)


# ---------- MFA y cifrado ----------
def test_totp_rfc6238_vector():
    # Vector oficial RFC 6238 (SHA-1, secreto ASCII "12345678901234567890", T=59 -> 94287082)
    import base64
    sec = base64.b32encode(b"12345678901234567890").decode()
    assert totp._hotp(sec, 59 // 30, 8) == "94287082"


def test_totp_ventana_y_reuso():
    s = totp.nuevo_secreto(); t = 1_700_000_000
    c = totp.codigo(s, t)
    aceptado = totp.verificar(s, c, 0, t)
    assert aceptado is not None
    assert totp.verificar(s, c, aceptado, t) is None            # no se puede reutilizar
    assert totp.verificar(s, c, 0, t + 300) is None             # fuera de ventana
    assert totp.verificar(s, "abc123", 0, t) is None


def test_cifrado_campos():
    from cryptography.fernet import Fernet
    k = Fernet.generate_key().decode()
    c = cifrado.CifradorCampos(k)
    tok = c.cifrar("SECRETO")
    assert tok != "SECRETO" and c.descifrar(tok) == "SECRETO"
    assert lanza(ValueError, cifrado.CifradorCampos(Fernet.generate_key().decode()).descifrar, tok)
