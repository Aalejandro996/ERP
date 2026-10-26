"""Envío de correo (verificación, invitaciones, recuperación y alertas de seguridad).

MAIL_BACKEND=brevo (API transaccional de Brevo, BREVO_API_KEY), smtp (STARTTLS o SSL) o console (solo
desarrollo: el correo se escribe en el log). Los enlaces se construyen con APP_BASE_URL, NUNCA con la cabecera Host
de la petición (evita el envenenamiento de enlaces de recuperación)."""
import logging
import smtplib
import ssl
from email.message import EmailMessage

from flask import current_app

from . import brevo

log = logging.getLogger("erp.correo")
ENVIADOS = []  # backend "console": últimos mensajes (lo usan las pruebas)


def url_absoluta(ruta: str) -> str:
    return current_app.config["APP_BASE_URL"].rstrip("/") + ruta


def enviar(destino: str, asunto: str, cuerpo: str):
    cfg = current_app.config
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = cfg["MAIL_FROM"], destino, f"[{cfg['APP_NOMBRE']}] {asunto}"
    msg.set_content(cuerpo + "\n\n—\nMensaje automático. Si no reconoce esta acción, contacte de inmediato al administrador de su organización.")
    if cfg["MAIL_BACKEND"] == "console":
        ENVIADOS.append(msg); del ENVIADOS[:-50]
        log.warning("CORREO (console) para %s: %s\n%s", destino, asunto, cuerpo)
        return
    if cfg["MAIL_BACKEND"] == "brevo":
        try:
            brevo.enviar(cfg["BREVO_API_KEY"], cfg["MAIL_FROM"], destino, msg["Subject"], msg.get_content())
        except brevo.ErrorCorreo as e:
            raise OSError(str(e)) from e
        return
    ctx = ssl.create_default_context()
    if cfg["SMTP_SSL"]:
        with smtplib.SMTP_SSL(cfg["SMTP_HOST"], cfg["SMTP_PORT"], context=ctx, timeout=15) as s:
            if cfg["SMTP_USER"]: s.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=15) as s:
            s.starttls(context=ctx)  # nunca credenciales en claro
            if cfg["SMTP_USER"]: s.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
            s.send_message(msg)


def enviar_seguro(destino, asunto, cuerpo) -> bool:
    """Para alertas no críticas: un fallo del SMTP no debe tumbar el inicio de sesión."""
    try:
        enviar(destino, asunto, cuerpo); return True
    except (OSError, smtplib.SMTPException):
        log.exception("No se pudo enviar el correo a %s", destino); return False
