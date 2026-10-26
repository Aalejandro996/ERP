"""Cifrado de campos sensibles en reposo (p. ej. secreto TOTP) con Fernet (AES-128-CBC + HMAC)."""
from cryptography.fernet import Fernet, InvalidToken


class CifradorCampos:
    def __init__(self, clave: str):
        self._f = Fernet(clave)

    def cifrar(self, texto: str) -> str:
        return self._f.encrypt(texto.encode()).decode()

    def descifrar(self, token: str) -> str:
        try:
            return self._f.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("Campo cifrado ilegible o clave incorrecta") from exc
