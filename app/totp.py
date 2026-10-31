"""TOTP (RFC 6238) con la librería estándar: sin dependencias externas."""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote


def nuevo_secreto() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(secreto: str, contador: int, digitos=6) -> str:
    clave = base64.b32decode(secreto + "=" * (-len(secreto) % 8), casefold=True)
    h = hmac.new(clave, struct.pack(">Q", contador), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    return str((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** digitos).zfill(digitos)


def codigo(secreto: str, t=None, paso=30) -> str:
    return _hotp(secreto, int((t if t is not None else time.time()) // paso))


def verificar(secreto: str, ingresado: str, ultimo_contador: int = 0, t=None, paso=30, ventana=1):
    """Devuelve el contador aceptado (para guardarlo y evitar reutilización) o None."""
    if not (ingresado and ingresado.isdigit() and len(ingresado) == 6):
        return None
    actual = int((t if t is not None else time.time()) // paso)
    for c in range(actual - ventana, actual + ventana + 1):
        if c > ultimo_contador and hmac.compare_digest(_hotp(secreto, c), ingresado):
            return c
    return None


def uri_otpauth(secreto: str, usuario: str, emisor="Contax") -> str:
    return f"otpauth://totp/{quote(emisor)}:{quote(usuario)}?secret={secreto}&issuer={quote(emisor)}&digits=6&period=30"
