"""Costeo de inventario: Promedio Ponderado y PEPS (FIFO). Lógica pura con Decimal."""
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

D4, D2 = Decimal("0.0001"), Decimal("0.01")


class ErrorInventario(ValueError):
    pass


@dataclass
class Lote:
    fecha: str
    cantidad: Decimal
    costo: Decimal
    id: int | None = None


def _pos(v, nombre):
    v = Decimal(str(v))
    if v <= 0:
        raise ErrorInventario(f"{nombre} debe ser mayor que cero")
    return v


def entrada_promedio(cant_act, costo_act, cant, costo):
    cant, costo = _pos(cant, "Cantidad"), Decimal(str(costo))
    if costo < 0:
        raise ErrorInventario("El costo no puede ser negativo")
    cant_act, costo_act = Decimal(str(cant_act)), Decimal(str(costo_act))
    nueva = cant_act + cant
    nuevo_costo = ((cant_act * costo_act + cant * costo) / nueva).quantize(D4, ROUND_HALF_UP)
    return nueva, nuevo_costo


def salida_promedio(cant_act, costo_act, cant):
    cant = _pos(cant, "Cantidad")
    if cant > Decimal(str(cant_act)):
        raise ErrorInventario(f"Stock insuficiente. Disponible: {cant_act}")
    costo_u = Decimal(str(costo_act))
    return (cant * costo_u).quantize(D2, ROUND_HALF_UP), costo_u


def salida_peps(lotes: list[Lote], cant):
    """Consume lotes del más antiguo al más nuevo. Devuelve (costo_total, costo_unitario, consumos).
    consumos = [(lote, cantidad_consumida)] para que el servicio actualice cada lote."""
    cant = _pos(cant, "Cantidad")
    disponible = sum(l.cantidad for l in lotes)
    if cant > disponible:
        raise ErrorInventario(f"Stock insuficiente. Disponible: {disponible}")
    falta, total, consumos = cant, Decimal(0), []
    for l in sorted((l for l in lotes if l.cantidad > 0), key=lambda x: (x.fecha, x.id or 0)):
        if falta <= 0:
            break
        usa = min(l.cantidad, falta)
        total += usa * l.costo
        consumos.append((l, usa))
        falta -= usa
    total = total.quantize(D2, ROUND_HALF_UP)
    return total, (total / cant).quantize(D4, ROUND_HALF_UP), consumos


def valor_peps(lotes):
    return sum((l.cantidad * l.costo for l in lotes), Decimal(0)).quantize(D2, ROUND_HALF_UP)
