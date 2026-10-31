"""Conexión del bot por WasenderAPI (WhatsApp por código QR, no oficial), alternativa a la Cloud API de Meta.

Cada empresa conecta SU WhatsApp (el mismo de la app WhatsApp Business del teléfono) escaneando el QR en su panel de
WasenderAPI, y carga en Contax la clave de API de esa sesión y el secreto de webhook. La conversación, los pedidos, la
lectura de PDF y la facturación son los mismos que con Meta: solo cambia por dónde entran y salen los mensajes.

⚠ Se escribió sin acceso a la documentación de WasenderAPI (la red de desarrollo no llegaba a wasenderapi.com):
los nombres de rutas y campos son los conocidos de su API y el lector de avisos acepta sus variantes. La URL base es
configurable. Verificar con una sesión real antes de vender el servicio. Riesgo propio de cualquier conexión no oficial:
WhatsApp puede bloquear el número si detecta uso automatizado o masivo.
"""
import hashlib
import hmac
import json
import re
import urllib.error
import urllib.request

from .whatsapp import ErrorWhatsapp, telefono

URL_BASE = "https://www.wasenderapi.com/api"


def _http(metodo, url, cuerpo: bytes | None, cabeceras: dict, timeout=20):
    req = urllib.request.Request(url, cuerpo, cabeceras, method=metodo)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (URL de configuración)
            return r.status, r.read(8 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _base(url) -> str:
    u = (url or URL_BASE).strip().rstrip("/")
    if not u.startswith("https://"): raise ErrorWhatsapp("La URL de WasenderAPI debe ser https://")
    return u


def _json(cuerpo):
    try:
        return json.loads(cuerpo or b"{}")
    except ValueError:
        return {}


def _error(estado, r) -> ErrorWhatsapp:
    msg = r.get("message") or r.get("error") or f"error {estado}"
    if isinstance(msg, dict): msg = msg.get("message") or json.dumps(msg)[:200]
    if estado == 429: msg = f"{msg} — se superó el límite de mensajes de su plan de WasenderAPI; espere un momento"
    if estado in (401, 403): msg = f"{msg} — revise la clave de API de la sesión"
    return ErrorWhatsapp(f"WasenderAPI: {str(msg)[:250]}")


def enviar_texto(api_url, api_key, destino, texto, transporte=None) -> str:
    """POST /send-message {to, text}. Devuelve el id del mensaje si la API lo informa."""
    transporte = transporte or _http
    estado, resp = transporte("POST", f"{_base(api_url)}/send-message", json.dumps({"to": "+" + telefono(destino), "text": str(texto)[:4000]}).encode(),
                              {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept": "application/json"})
    r = _json(resp)
    if estado >= 400 or r.get("success") is False: raise _error(estado, r)
    d = r.get("data") or {}
    return str(d.get("msgId") or d.get("messageId") or d.get("id") or "")[:200] if isinstance(d, dict) else ""


def estado_sesion(api_url, api_key, transporte=None) -> str:
    """Comprueba la clave y si el WhatsApp está conectado (GET /status)."""
    transporte = transporte or _http
    estado, resp = transporte("GET", f"{_base(api_url)}/status", None, {"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
    r = _json(resp)
    if estado >= 400: raise _error(estado, r)
    return str(r.get("status") or (r.get("data") or {}).get("status") or "desconocido")


def descargar_media(api_url, api_key, mensaje: dict, transporte=None) -> tuple[bytes, str]:
    """Los adjuntos llegan cifrados: POST /decrypt-media con el mensaje recibido devuelve una URL pública temporal."""
    transporte = transporte or _http
    estado, resp = transporte("POST", f"{_base(api_url)}/decrypt-media", json.dumps({"data": {"messages": mensaje}}).encode(),
                              {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept": "application/json"})
    r = _json(resp)
    if estado >= 400 or r.get("success") is False: raise _error(estado, r)
    url = r.get("publicUrl") or (r.get("data") or {}).get("publicUrl") or r.get("url") or ""
    if not str(url).startswith("https://"): raise ErrorWhatsapp("WasenderAPI no devolvió el archivo")
    estado, datos = transporte("GET", url, None, {})
    if estado >= 400: raise ErrorWhatsapp("No se pudo descargar el adjunto")
    if len(datos) > 8 * 1024 * 1024: raise ErrorWhatsapp("El adjunto supera 8 MB")
    doc = ((mensaje.get("message") or {}).get("documentMessage") or {})
    return datos, doc.get("mimetype") or "application/octet-stream"


def firma_valida(secreto: str, cuerpo: bytes, cabecera: str) -> bool:
    """X-Webhook-Signature: WasenderAPI envía el secreto de webhook de la sesión. Se acepta también su HMAC-SHA256 del cuerpo
    (hex), por si la cuenta firma los avisos. Comparación en tiempo constante; sin secreto no se acepta nada."""
    if not secreto or not cabecera: return False
    c = cabecera.strip()
    if c.startswith("sha256="): c = c[7:]
    return hmac.compare_digest(c, secreto) or hmac.compare_digest(c, hmac.new(secreto.encode(), cuerpo, hashlib.sha256).hexdigest())


def _texto(m: dict) -> str:
    msg = m.get("message") or {}
    for v in (msg.get("conversation"), (msg.get("extendedTextMessage") or {}).get("text"), (msg.get("documentMessage") or {}).get("caption"),
              (msg.get("imageMessage") or {}).get("caption"), (msg.get("buttonsResponseMessage") or {}).get("selectedDisplayText"),
              (msg.get("listResponseMessage") or {}).get("title"), m.get("messageBody"), m.get("body")):
        if v: return str(v)
    return ""


def mensajes_entrantes(aviso: dict):
    """Igual que whatsapp.mensajes_entrantes: (telefono, nombre, texto, id, adjunto). Ignora lo enviado por la propia
    empresa (fromMe), los grupos, los estados y los avisos que no son mensajes nuevos."""
    evento = str(aviso.get("event") or "")
    if evento and (not evento.startswith("messages") or "update" in evento or "delete" in evento or "reaction" in evento): return
    datos = aviso.get("data") or {}
    lote = datos.get("messages") if isinstance(datos, dict) else datos
    for m in (lote if isinstance(lote, list) else [lote]):
        if not isinstance(m, dict): continue
        k = m.get("key") or {}
        jid = str(k.get("cleanedSenderPn") or k.get("senderPn") or k.get("remoteJid") or m.get("remoteJid") or "")
        if k.get("fromMe") or "@g.us" in jid or "status@broadcast" in jid or "@newsletter" in jid: continue
        tel = re.sub(r"\D", "", jid.split("@")[0].split(":")[0])
        mid = str(k.get("id") or m.get("id") or "")
        if not (tel and mid): continue
        msg = m.get("message") or {}
        adj = None
        for tipo in ("documentMessage", "documentWithCaptionMessage", "imageMessage"):
            x = msg.get(tipo)
            if tipo == "documentWithCaptionMessage" and x: x = ((x.get("message") or {}).get("documentMessage"))
            if x:
                adj = {"id": mid, "mime": x.get("mimetype", ""), "nombre": str(x.get("fileName") or x.get("title") or "archivo")[:120], "raw": m}
                break
        texto = _texto(m) or (f"[documento: {adj['nombre']}]" if adj else "[mensaje no textual]")
        yield tel, m.get("pushName"), texto[:4000], ("ws." + mid)[:200], adj
