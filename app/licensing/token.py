"""Token de licencia firmado con Ed25519.

El servidor de licencias (del proveedor) es el ÚNICO que posee la clave privada.
La instalación del cliente solo conoce la clave pública: puede VERIFICAR un token,
pero no fabricar uno. Formato: base64url(payload_json) + "." + base64url(firma).
"""
import base64
import json

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


class TokenInvalido(Exception):
    pass


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def firmar(claims: dict, clave_privada: Ed25519PrivateKey) -> str:
    payload = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
    return f"{_b64e(payload)}.{_b64e(clave_privada.sign(payload))}"


def verificar(token: str, clave_publica: Ed25519PublicKey) -> dict:
    try:
        payload_b64, firma_b64 = token.split(".")
        payload = _b64d(payload_b64)
        clave_publica.verify(_b64d(firma_b64), payload)
        return json.loads(payload)
    except (ValueError, InvalidSignature) as exc:
        raise TokenInvalido("Firma o formato de licencia inválido") from exc


def clave_publica_desde_b64(b64: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(_b64d(b64))
