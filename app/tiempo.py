"""Fecha y hora legales de Venezuela (America/Caracas, UTC-4), independientes de la zona horaria del servidor."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

ZONA = ZoneInfo("America/Caracas")


def ahora_ve() -> datetime:
    return datetime.now(ZONA)


def hoy_ve() -> date:
    return ahora_ve().date()


def a_hora_ve(dt: datetime | None) -> datetime | None:
    return dt.astimezone(ZONA) if dt else None
