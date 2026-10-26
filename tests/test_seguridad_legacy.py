import base64
import hashlib

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import legacy
from app.licensing.token import clave_publica_desde_b64, firmar, verificar


def _hash_original(password, salt):  # copia literal de db._hash_password del sistema original
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()


def test_contrasena_del_sistema_original_sigue_valida():
    sal = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    g = legacy.envolver(sal, _hash_original("MiClave#2026", sal))
    assert legacy.es_legacy(g)
    assert legacy.verificar_legacy("MiClave#2026", g)
    assert not legacy.verificar_legacy("otra", g)
    assert not legacy.verificar_legacy("x", "legacy$pbkdf2$basura")     # formato dañado: rechaza sin excepción
    assert not legacy.es_legacy("!")                                      # usuario sin clave utilizable


def test_formato_de_claves_del_cli_es_compatible():
    k = Ed25519PrivateKey.generate()
    pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    pub_b64 = base64.urlsafe_b64encode(pub).rstrip(b"=").decode()
    tok = firmar({"x": 1}, k)
    assert verificar(tok, clave_publica_desde_b64(pub_b64)) == {"x": 1}
