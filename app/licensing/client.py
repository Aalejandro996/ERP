"""Lado cliente (instalación del ERP): activación, renovación y verificación.

Reglas:
  * Falla CERRADO: sin token válido y vigente, el sistema no opera.
  * La vigencia sale de `exp` FIRMADO por el servidor, no de un contador local.
  * Detección de retroceso de reloj: si la hora actual es menor que la última vista, se bloquea.
  * El token está atado a la huella de ESTA instalación; copiarlo a otro servidor no sirve.
"""
import hashlib
import json
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

from .token import TokenInvalido, verificar

TOLERANCIA_RELOJ_S = 300  # 5 min de deriva permitida


def huella_instalacion(instance_secret: str, db_system_id: str = "") -> str:
    """Huella estable de la instalación (no depende de wmic ni de la MAC).

    instance_secret: secreto aleatorio generado una vez en el primer arranque.
    db_system_id: identificador del clúster PostgreSQL (pg_control_system().system_identifier).
    """
    try:
        machine = Path("/etc/machine-id").read_text().strip()
    except OSError:
        machine = f"{uuid.getnode():x}"
    return hashlib.sha256(f"{instance_secret}|{db_system_id}|{machine}".encode()).hexdigest()


@dataclass
class EstadoLicencia:
    valida: bool
    motivo: str = ""
    claims: dict | None = None


def _post_json(url: str, data: dict, timeout: int = 10) -> tuple[int, dict]:
    from urllib.parse import urlparse
    u = urlparse(url)
    privado = u.scheme == "http" and u.hostname and ("." not in u.hostname or u.hostname in ("localhost", "127.0.0.1"))
    if u.scheme != "https" and not privado:  # http solo dentro de la red privada del proveedor (p. ej. Render)
        raise ValueError("El servidor de licencias debe usar HTTPS")
    req = urllib.request.Request(url, json.dumps(data).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class GestorLicencia:
    """`almacen` expone obtener() -> dict|None y guardar(dict) (guardado en BD, no en JSON plano)."""

    def __init__(self, url_servidor, clave_publica, almacen, huella, transporte=_post_json, reloj=time.time):
        self.url, self.pub, self.almacen = url_servidor.rstrip("/"), clave_publica, almacen
        self.huella, self.post, self.ahora = huella, transporte, reloj

    def activar(self, clave_licencia: str) -> EstadoLicencia:
        nonce = uuid.uuid4().hex
        estado, resp = self.post(
            f"{self.url}/v1/activate",
            {"key": clave_licencia.strip(), "fingerprint": self.huella, "nonce": nonce},
        )
        return self._procesar(estado, resp, nonce, clave_licencia.strip())

    def renovar(self) -> EstadoLicencia:
        datos = self.almacen.obtener() or {}
        if not datos.get("key"):
            return EstadoLicencia(False, "Sin licencia activada")
        nonce = uuid.uuid4().hex
        estado, resp = self.post(
            f"{self.url}/v1/refresh",
            {"key": datos["key"], "fingerprint": self.huella, "nonce": nonce},
        )
        return self._procesar(estado, resp, nonce, datos["key"])

    def _procesar(self, estado, resp, nonce, clave) -> EstadoLicencia:
        if estado != 200:
            if estado in (401, 403, 404, 409):  # rechazo explícito: se invalida lo guardado
                self.almacen.guardar({})
            return EstadoLicencia(False, resp.get("message", "Licencia rechazada por el servidor"))
        try:
            claims = verificar(resp["token"], self.pub)
        except (KeyError, TokenInvalido):
            return EstadoLicencia(False, "Respuesta del servidor con firma inválida")
        if claims.get("fp") != self.huella or claims.get("nonce") != nonce:
            return EstadoLicencia(False, "Token no corresponde a esta instalación")
        self.almacen.guardar({"key": clave, "token": resp["token"], "last_seen": self.ahora()})
        return EstadoLicencia(True, claims=claims)

    def estado(self) -> EstadoLicencia:
        """Verificación local en cada petición, sin red."""
        datos = self.almacen.obtener() or {}
        if not datos.get("token"):
            return EstadoLicencia(False, "Licencia no activada")
        try:
            claims = verificar(datos["token"], self.pub)
        except TokenInvalido:
            return EstadoLicencia(False, "Licencia corrupta o alterada")
        ahora = self.ahora()
        if claims.get("fp") != self.huella:
            return EstadoLicencia(False, "Licencia atada a otra instalación")
        if ahora + TOLERANCIA_RELOJ_S < datos.get("last_seen", 0):
            return EstadoLicencia(False, "Retroceso de reloj detectado")
        if ahora > claims["exp"]:
            return EstadoLicencia(False, "Licencia vencida: conecte con el servidor para renovar")
        if ahora - datos.get("last_seen", 0) > 60:
            datos["last_seen"] = ahora
            self.almacen.guardar(datos)
        return EstadoLicencia(True, claims=claims)
