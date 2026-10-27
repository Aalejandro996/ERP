"""Lectura de la tasa oficial BCV (USD/EUR) desde DolarAPI, sin red."""
from datetime import date
from decimal import Decimal as D

from app import tasas


def _api(usd="887.302", eur="994.03555758", fecha="2026-10-27T00:00:00-04:00", moneda_ok=True):
    datos = {tasas.URLS["USD"]: {"moneda": "USD", "promedio": usd, "fechaActualizacion": fecha},
             tasas.URLS["EUR"]: {"moneda": "EUR" if moneda_ok else "USD", "promedio": eur, "fechaActualizacion": fecha}}
    return lambda url: datos[url]


def lanza(f):
    try: f()
    except tasas.ErrorTasa: return True
    return False


def test_lee_dolar_y_euro_con_su_fecha_de_vigencia():
    t = tasas.consultar(_api())
    assert t == {"USD": D("887.302000"), "EUR": D("994.035558"), "fecha": date(2026, 10, 27)}


def test_rechaza_respuestas_dudosas():
    assert lanza(lambda: tasas.consultar(_api(usd="0")))
    assert lanza(lambda: tasas.consultar(_api(usd="-5")))
    assert lanza(lambda: tasas.consultar(_api(eur="abc")))
    assert lanza(lambda: tasas.consultar(_api(moneda_ok=False)))
    assert lanza(lambda: tasas.consultar(_api(fecha="2099-01-01")))
    def caida(_url): raise OSError("sin red")
    assert lanza(lambda: tasas.consultar(caida))
