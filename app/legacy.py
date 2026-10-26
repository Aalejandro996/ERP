"""Compatibilidad con las contraseñas del sistema original (PBKDF2-SHA256, 200.000 iteraciones, sal hexadecimal).
Formato guardado por la migración: legacy$pbkdf2$<iteraciones>$<sal_hex>$<hash_hex>. Sin dependencias externas."""
import hashlib
import hmac

PREFIJO = "legacy$pbkdf2$"


def es_legacy(guardado: str | None) -> bool:
    return bool(guardado) and guardado.startswith(PREFIJO)


def verificar_legacy(clave: str, guardado: str) -> bool:
    try:
        _, _, iters, sal, h = guardado.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", clave.encode(), bytes.fromhex(sal), int(iters)).hex()
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(calc, h)


def envolver(sal_hex: str, hash_hex: str, iteraciones: int = 200_000) -> str:
    return f"{PREFIJO}{iteraciones}${sal_hex}${hash_hex}"
