"""Cliente mínimo de la WhatsApp Cloud API (Meta) y verificación de los webhooks.

Envío: POST https://graph.facebook.com/<versión>/<phone_number_id>/messages con el token de la empresa.
Recepción: Meta firma cada aviso con HMAC-SHA256 del cuerpo usando el «App Secret» (cabecera X-Hub-Signature-256);
un aviso sin firma válida se descarta. Nota de Meta: fuera de la ventana de 24 h desde el último mensaje del contacto
solo se pueden enviar PLANTILLAS aprobadas; dentro de la ventana, texto libre."""
import hashlib
import hmac
import json
import re
import urllib.error
import urllib.request

API = "https://graph.facebook.com/v21.0"


class ErrorWhatsapp(Exception):
    pass


def telefono(valor) -> str:
    """Normaliza a solo dígitos con código de país; 0412… (Venezuela) → 58412…"""
    d = re.sub(r"\D", "", str(valor or ""))
    if d.startswith("00"): d = d[2:]
    if len(d) == 11 and d.startswith("0"): d = "58" + d[1:]
    if not re.fullmatch(r"\d{8,15}", d): raise ValueError(f"Número de WhatsApp inválido: «{valor}» (use el formato 58412…)")
    return d


def firma_valida(app_secret: str, cuerpo: bytes, cabecera: str) -> bool:
    if not app_secret or not cabecera or not cabecera.startswith("sha256="): return False
    esperado = hmac.new(app_secret.encode(), cuerpo, hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperado, cabecera[7:])


def _http(url, cuerpo: bytes, cabeceras: dict, timeout=15):
    req = urllib.request.Request(url, cuerpo, cabeceras, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (URL fija de Meta)
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def enviar_texto(phone_number_id: str, token: str, destino: str, texto: str, transporte=_http) -> str:
    """Devuelve el id del mensaje (wamid). Lanza ErrorWhatsapp con el motivo que da Meta."""
    datos = {"messaging_product": "whatsapp", "to": telefono(destino), "type": "text", "text": {"preview_url": True, "body": str(texto)[:4000]}}
    estado, resp = transporte(f"{API}/{phone_number_id}/messages", json.dumps(datos).encode(),
                              {"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        r = json.loads(resp or b"{}")
    except ValueError:
        r = {}
    if estado >= 400:
        msg = (r.get("error") or {}).get("message") or f"error {estado}"
        if (r.get("error") or {}).get("code") in (131047, 131026):
            msg += " — pasaron más de 24 h desde el último mensaje del contacto: Meta solo permite plantillas aprobadas"
        raise ErrorWhatsapp(f"WhatsApp: {msg}")
    return ((r.get("messages") or [{}])[0]).get("id", "")


def mensajes_entrantes(aviso: dict):
    """Extrae (telefono, nombre, texto, wamid) de un aviso de Meta. Los mensajes no textuales llegan como «[tipo]»."""
    for entrada in aviso.get("entry") or []:
        for cambio in entrada.get("changes") or []:
            v = cambio.get("value") or {}
            nombres = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in v.get("contacts") or []}
            for m in v.get("messages") or []:
                tipo = m.get("type")
                if tipo == "text": txt = (m.get("text") or {}).get("body", "")
                elif tipo == "button": txt = (m.get("button") or {}).get("text", "")
                elif tipo == "interactive":
                    i = m.get("interactive") or {}
                    txt = (i.get("button_reply") or i.get("list_reply") or {}).get("title", "")
                else: txt = f"[{tipo}]"
                if m.get("from") and m.get("id"):
                    yield str(m["from"]), nombres.get(m["from"]), str(txt)[:4000], str(m["id"])[:200]
