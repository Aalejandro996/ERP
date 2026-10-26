"""Envío transaccional por la API de Brevo (sin dependencias). Lo usan el ERP y el servidor de licencias.
La clave (BREVO_API_KEY) solo vive en variables de entorno; nunca en el código ni en la base de datos."""
import json
import re
import urllib.error
import urllib.request

URL = "https://api.brevo.com/v3/smtp/email"


class ErrorCorreo(Exception):
    pass


def remitente(texto: str) -> dict:
    """'Gigadevs Corp <aalejandro996@icloud.com>' → {'name': 'Gigadevs Corp', 'email': 'aalejandro996@icloud.com'}"""
    m = re.fullmatch(r"\s*(.*?)\s*<([^<>\s]+@[^<>\s]+)>\s*", texto or "")
    return {"name": m.group(1) or m.group(2), "email": m.group(2)} if m else {"email": (texto or "").strip()}


def _post(url, datos, cabeceras, timeout=15):
    req = urllib.request.Request(url, json.dumps(datos).encode(), {**cabeceras, "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (URL fija, HTTPS)
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def enviar(api_key, de, para, asunto, texto, html=None, etiquetas=(), transporte=_post):
    if not api_key:
        raise ErrorCorreo("Falta BREVO_API_KEY")
    if not re.fullmatch(r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+", para or ""):
        raise ErrorCorreo("Destinatario inválido")
    cuerpo = {"sender": remitente(de), "to": [{"email": para}], "subject": asunto[:200], "textContent": texto}
    if html: cuerpo["htmlContent"] = html
    if etiquetas: cuerpo["tags"] = list(etiquetas)
    estado, resp = transporte(URL, cuerpo, {"api-key": api_key})
    if estado not in (200, 201, 202):
        raise ErrorCorreo(f"Brevo respondió {estado}")
    try:
        return json.loads(resp or b"{}").get("messageId")
    except ValueError:
        return None
