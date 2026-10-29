"""Tasa oficial del BCV (dólar y euro) obtenida automáticamente de DolarAPI (https://ve.dolarapi.com).

- Se consulta como mucho una vez cada 30 minutos por proceso, solo si falta la tasa de hoy, y nunca bloquea la operación:
  si DolarAPI no responde se usa la última tasa registrada (y el Maestro puede cargarla a mano).
- La fecha que se guarda es la de vigencia que publica el BCV (fechaActualizacion), no la hora de la consulta.
- Una tasa cargada a mano por el Maestro para una fecha tiene prioridad: la consulta automática no la sobrescribe.
- Las URL son fijas (HTTPS) y los valores se validan antes de guardarse."""
import json
import logging
import time
import urllib.request
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import select

from .extensions import db
from .tiempo import hoy_ve

log = logging.getLogger("erp.tasas")
URLS = {"USD": "https://ve.dolarapi.com/v1/dolares/oficial", "EUR": "https://ve.dolarapi.com/v1/euros/oficial"}
FUENTE = "BCV vía DolarAPI"
_ultimo_intento = [0.0]
ESPERA_S = 1800


class ErrorTasa(Exception):
    pass


def _get(url, timeout=6):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "ERP-Gigadevs/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (URL fija HTTPS)
        if r.status != 200: raise ErrorTasa(f"DolarAPI respondió {r.status}")
        return json.loads(r.read(64 * 1024))


def consultar(obtener=_get) -> dict:
    """Devuelve {"USD": Decimal, "EUR": Decimal, "fecha": date}. Lanza ErrorTasa si algo no cuadra."""
    out, fechas = {}, set()
    for moneda, url in URLS.items():
        try:
            d = obtener(url)
            valor = Decimal(str(d["promedio"]))
            fecha = date.fromisoformat(str(d["fechaActualizacion"])[:10])
        except (OSError, ValueError, KeyError, TypeError, InvalidOperation) as e:
            raise ErrorTasa(f"No se pudo leer la tasa {moneda}: {type(e).__name__}") from e
        if d.get("moneda") not in (None, moneda) or not (Decimal("0.0001") < valor < Decimal("100000000")):
            raise ErrorTasa(f"Tasa {moneda} fuera de rango")
        out[moneda] = valor.quantize(Decimal("0.000001"))
        fechas.add(fecha)
    out["fecha"] = max(fechas)
    if out["fecha"] > hoy_ve() + timedelta(days=7):
        raise ErrorTasa("Fecha de vigencia inválida")
    return out


def guardar(t: dict, forzar=False) -> str:
    """Inserta o completa la tasa de la fecha de vigencia. No pisa una tasa cargada a mano salvo forzar=True."""
    from .models2 import TasaCambio
    fila = db.session.execute(select(TasaCambio).where(TasaCambio.fecha == t["fecha"]).with_for_update()).scalar_one_or_none()
    if fila is None:
        db.session.add(TasaCambio(fecha=t["fecha"], tasa_usd_ves=t["USD"], tasa_eur_ves=t["EUR"], fuente=FUENTE))
        return "registrada"
    if fila.fuente not in (None, FUENTE) and not forzar:
        if fila.tasa_eur_ves is None: fila.tasa_eur_ves = t["EUR"]
        return "conservada (cargada a mano)"
    fila.tasa_usd_ves, fila.tasa_eur_ves, fila.fuente = t["USD"], t["EUR"], FUENTE
    return "actualizada"


def actualizar_si_falta(obtener=_get) -> bool:
    """Consulta DolarAPI solo si no hay tasa con vigencia de hoy (o posterior). Nunca lanza excepciones."""
    from flask import current_app
    from .models2 import TasaCambio
    if not current_app.config.get("TASAS_AUTOMATICAS", True):
        return False
    try:
        hoy = hoy_ve()
        ultima = db.session.execute(select(TasaCambio.fecha).where(TasaCambio.tasa_eur_ves.is_not(None))
                                    .order_by(TasaCambio.fecha.desc()).limit(1)).scalar()
        if (ultima and ultima >= hoy) or time.time() - _ultimo_intento[0] < ESPERA_S:
            return False
        _ultimo_intento[0] = time.time()
        guardar(consultar(obtener))
        db.session.commit()
        return True
    except Exception as e:  # noqa: BLE001 — la facturación nunca debe caerse por la tasa
        db.session.rollback()
        log.warning("Tasa BCV automática no disponible: %s", e)
        return False


def tasa_vigente(fecha, moneda="USD"):
    """Última tasa con vigencia ≤ fecha para USD o EUR (None si no hay)."""
    from .models2 import TasaCambio
    if moneda == "VES": return Decimal(1)
    if moneda == "USDT": moneda = "USD"   # el BCV no publica USDT: se usa la tasa oficial del dólar
    if fecha >= hoy_ve(): actualizar_si_falta()
    col = TasaCambio.tasa_eur_ves if moneda == "EUR" else TasaCambio.tasa_usd_ves
    return db.session.execute(select(col).where(TasaCambio.fecha <= fecha, col.is_not(None)).order_by(TasaCambio.fecha.desc()).limit(1)).scalar()
