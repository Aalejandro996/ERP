"""Conexión del bot por OpenWA (open-wa.org): pasarela de WhatsApp autoalojada, multi-sesión, vinculada por código QR
como WhatsApp Web. Una sola pasarela para toda la plataforma (URL y clave de API en la consola Maestro); cada empresa
tiene SU sesión, que vincula escaneando el QR con el teléfono de su número. La empresa no ve ni configura credenciales.

API (docs/06-api-specification.md de OpenWA): base <url>/api, clave en la cabecera X-API-Key (nunca en la URL).
  POST /sessions {name} → {id}       POST /sessions/:id/start       GET /sessions/:id → {status, phone, pushName}
  GET /sessions/:id/qr → {qrCode: data:image/png;base64,…}         POST /sessions/:id/logout · DELETE /sessions/:id
  POST /sessions/:id/webhooks {url, events, secret}                POST /sessions/:id/messages/send-text {chatId, text}
  GET /sessions/:id/messages/:chatId/:messageId/media (adjuntos grandes, que no vienen dentro del aviso)
Avisos: POST con {event, sessionId, data}; firma «X-OpenWA-Signature: sha256=<hex>» = HMAC-SHA256 del cuerpo crudo.
Riesgo propio de toda conexión no oficial: WhatsApp puede limitar un número que envíe en masa; el bot atiende, no publicita.
"""
import base64
import hashlib
import hmac
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from .whatsapp import ErrorWhatsapp, telefono

EVENTOS = ["message.received", "session.status", "session.authenticated", "session.disconnected"]
ESTADOS = {"created": "Sin vincular", "initializing": "Iniciando…", "qr_ready": "Esperando el escaneo del QR", "authenticating": "Vinculando…",
           "ready": "Conectado", "disconnected": "Desconectado", "action_required": "Requiere atención en el teléfono", "failed": "Falló la conexión"}
MAX_ADJUNTO = 8 * 1024 * 1024


def validar_url(url) -> str:
    """https siempre; http solo dentro de la red privada (nombre sin punto, p. ej. http://openwa:2785) o en localhost."""
    u = str(url or "").strip().rstrip("/")
    if u.endswith("/api"): u = u[:-4]
    p = urllib.parse.urlsplit(u)
    host = p.hostname or ""
    if p.scheme == "https" and host: return u
    if p.scheme == "http" and host and ("." not in host or host in ("localhost", "127.0.0.1")): return u
    raise ErrorWhatsapp("La URL de la pasarela debe ser https://… (o http:// dentro de la red privada, p. ej. http://openwa:2785)")


def _http(metodo, url, cuerpo: bytes | None, cabeceras: dict, timeout=40):
    req = urllib.request.Request(url, cuerpo, cabeceras, method=metodo)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (URL fijada por el Maestro y validada)
            return r.status, r.read(MAX_ADJUNTO + 1)
    except urllib.error.HTTPError as e:
        return e.code, e.read(64 * 1024)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ErrorWhatsapp(f"No se pudo contactar la pasarela de WhatsApp ({getattr(e, 'reason', e)})") from e


class SesionInexistente(ErrorWhatsapp):
    """La pasarela no conoce la sesión (404): se borró o se reinstaló la pasarela."""


class Pasarela:
    """Cliente de la API. `transporte(metodo, url, cuerpo, cabeceras) -> (estado, bytes)` se reemplaza en las pruebas."""

    def __init__(self, url, api_key, transporte=None):
        self.base, self.key, self.t = validar_url(url) + "/api", str(api_key or ""), transporte or _http
        if not self.key: raise ErrorWhatsapp("Falta la clave de API de la pasarela")

    def _req(self, metodo, ruta, cuerpo=None, binario=False):
        cab = {"X-API-Key": self.key, "Accept": "application/json"}
        if cuerpo is not None: cab["Content-Type"] = "application/json"
        estado, resp = self.t(metodo, self.base + ruta, None if cuerpo is None else json.dumps(cuerpo).encode(), cab)
        if binario and estado < 400: return resp
        try:
            r = json.loads(resp or b"null")
        except ValueError:
            r = None
        if estado >= 400:
            msg = (r or {}).get("message") if isinstance(r, dict) else None
            if isinstance(msg, list): msg = "; ".join(map(str, msg))
            if estado == 401: msg = "la clave de API de la pasarela no es válida"
            if estado == 404: raise SesionInexistente(str(msg or "no existe"))
            raise ErrorWhatsapp(f"Pasarela de WhatsApp: {str(msg or f'error {estado}')[:250]}")
        return r

    # -------------------------------------------------------------- sesiones
    def salud(self) -> int:
        """Comprueba la URL y la clave: devuelve cuántas sesiones hay en la pasarela."""
        r = self._req("GET", "/sessions")
        return len(r) if isinstance(r, list) else 0

    def sesiones(self) -> list:
        r = self._req("GET", "/sessions")
        return r if isinstance(r, list) else []

    def crear_sesion(self, nombre) -> str:
        return str(self._req("POST", "/sessions", {"name": nombre})["id"])

    def iniciar(self, sid):
        try:
            self._req("POST", f"/sessions/{_sid(sid)}/start")
        except SesionInexistente:
            raise
        except ErrorWhatsapp as e:
            if "already" not in str(e).lower(): raise          # «already started»: ya estaba en marcha, no es un error

    def estado(self, sid) -> dict:
        r = self._req("GET", f"/sessions/{_sid(sid)}")
        return {"estado": str(r.get("status") or "desconocido"), "telefono": r.get("phone"), "nombre": r.get("pushName")}

    def qr(self, sid) -> str | None:
        """Data URL PNG del QR, o None si todavía no hay (iniciando) o ya está vinculado."""
        try:
            r = self._req("GET", f"/sessions/{_sid(sid)}/qr")
        except SesionInexistente:
            raise
        except ErrorWhatsapp:
            return None
        q = str((r or {}).get("qrCode") or "")
        return q if q.startswith("data:image/png;base64,") else None

    def registrar_webhook(self, sid, url, secreto) -> str:
        r = self._req("POST", f"/sessions/{_sid(sid)}/webhooks", {"url": url, "events": EVENTOS, "secret": secreto, "retryCount": 3})
        return str(r.get("id") or "")

    def cerrar(self, sid):
        """Desvincula el teléfono (logout) y borra la sesión con sus credenciales. Lo que ya no exista no es error."""
        try:
            self._req("POST", f"/sessions/{_sid(sid)}/logout")
        except SesionInexistente:
            return
        except ErrorWhatsapp:
            pass                    # no estaba en marcha: igual se borra; el teléfono puede quitarla en «Dispositivos vinculados»
        try:
            self._req("DELETE", f"/sessions/{_sid(sid)}")
        except SesionInexistente:
            pass

    # -------------------------------------------------------------- mensajes
    def enviar_texto(self, sid, destino, texto) -> str:
        r = self._req("POST", f"/sessions/{_sid(sid)}/messages/send-text", {"chatId": telefono(destino) + "@c.us", "text": str(texto)[:4096]})
        return str((r or {}).get("messageId") or "")[:200]

    def descargar_media(self, sid, chat_id, mensaje_id) -> bytes:
        datos = self._req("GET", f"/sessions/{_sid(sid)}/messages/{urllib.parse.quote(chat_id, safe='@.')}/{urllib.parse.quote(mensaje_id, safe='')}/media",
                          binario=True)
        if len(datos) > MAX_ADJUNTO: raise ErrorWhatsapp("El adjunto supera 8 MB")
        return datos


def _sid(sid) -> str:
    s = str(sid or "")
    if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", s): raise ErrorWhatsapp("Sesión de WhatsApp inválida")
    return s


def nombre_sesion(eid, sufijo) -> str:
    """Nombre único en la pasarela (letras, números y guiones): identifica a la empresa en la consola del Maestro."""
    return f"contax-e{int(eid)}-{re.sub(r'[^a-z0-9]', '', sufijo.lower())[:8]}"


def firma_valida(secreto: str, cuerpo: bytes, cabecera: str) -> bool:
    if not secreto or not cabecera or not cabecera.startswith("sha256="): return False
    return hmac.compare_digest(cabecera[7:], hmac.new(secreto.encode(), cuerpo, hashlib.sha256).hexdigest())


def estado_del_aviso(aviso: dict):
    """(estado, teléfono) si el aviso informa un cambio de la sesión; None si es otro evento."""
    ev, d = str(aviso.get("event") or ""), aviso.get("data") or {}
    if ev == "session.authenticated": return "ready", d.get("phone")
    if ev == "session.disconnected": return "disconnected", None
    if ev == "session.status": return str(d.get("status") or d.get("newStatus") or d.get("to") or "desconocido"), d.get("phone")
    return None


def _tel(jid) -> str:
    j = str(jid or "")
    return re.sub(r"\D", "", j.split("@")[0].split(":")[0]) if j.endswith(("@c.us", "@s.whatsapp.net")) else ""


def mensajes_entrantes(aviso: dict):
    """Igual que whatsapp.mensajes_entrantes: (telefono, nombre, texto, id, adjunto). Solo chats individuales recibidos;
    ignora lo enviado por la propia empresa, grupos, estados y canales. Un remitente con id de privacidad (@lid) se
    atiende si la pasarela informa su número (RESOLVE_LID_TO_PHONE=true)."""
    if aviso.get("event") != "message.received": return
    m = aviso.get("data") or {}
    if not isinstance(m, dict) or m.get("fromMe") or m.get("isGroup") or (m.get("kind") or "individual") != "individual": return
    tel = re.sub(r"\D", "", str(m.get("senderPhone") or "")) or _tel(m.get("from")) or _tel(m.get("chatId"))
    mid = str(m.get("id") or "")
    if not (tel and mid): return
    contacto = m.get("contact") or {}
    adj, media = None, m.get("media") if isinstance(m.get("media"), dict) else None
    if media and m.get("type") in ("document", "image"):
        adj = {"id": mid, "mime": str(media.get("mimetype") or ""), "nombre": str(media.get("filename") or m.get("type"))[:120],
               "chat": str(m.get("chatId") or m.get("from") or "")}
        if media.get("data") and not media.get("omitted"):
            try:
                adj["datos"] = base64.b64decode(media["data"], validate=False)[:MAX_ADJUNTO + 1]
            except (ValueError, TypeError):
                pass
    texto = str(m.get("body") or "").strip() or (f"[{m.get('type')}: {adj['nombre']}]" if adj else f"[{m.get('type') or 'mensaje'}]")
    yield tel, contacto.get("pushName") or contacto.get("name"), texto[:4000], ("ow." + mid)[:200], adj
