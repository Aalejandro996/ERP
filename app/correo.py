"""Envío de correo (verificación, invitaciones, recuperación y alertas de seguridad).

MAIL_BACKEND=brevo (API transaccional de Brevo, BREVO_API_KEY), smtp (STARTTLS o SSL) o console (solo
desarrollo: el correo se escribe en el log). Los enlaces se construyen con APP_BASE_URL, NUNCA con la cabecera Host
de la petición (evita el envenenamiento de enlaces de recuperación)."""
import logging
import smtplib
import ssl
from email.message import EmailMessage

from flask import current_app

from . import brevo, plantilla_correo

log = logging.getLogger("erp.correo")
ENVIADOS = []  # backend "console": últimos mensajes (lo usan las pruebas)


def url_absoluta(ruta: str) -> str:
    return current_app.config["APP_BASE_URL"].rstrip("/") + ruta


def enviar(destino: str, asunto: str, cuerpo: str, boton=None, titulo=None, destacado=None, filas=(), nota=None, html=None):
    """cuerpo: párrafos separados por una línea en blanco. boton: (texto, url). Se envía en HTML (plantilla con el estilo
    de la aplicación) y en texto plano equivalente."""
    cfg = current_app.config
    parrafos = [p.strip() for p in cuerpo.split("\n\n") if p.strip()]
    titulo = titulo or asunto
    pie = "Mensaje automático: no responda a este correo. Si no reconoce esta acción, contacte de inmediato al administrador de su organización."
    txt = plantilla_correo.texto(titulo, parrafos, boton, destacado, filas, nota, cfg["APP_NOMBRE"]) + "\n" + pie
    htm = html or plantilla_correo.html(titulo, parrafos, cfg["APP_BASE_URL"], cfg["APP_NOMBRE"], cfg.get("APP_LEMA", ""), boton, destacado, filas, nota, pie)
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = cfg["MAIL_FROM"], destino, f"{asunto} · {cfg['APP_NOMBRE']}"
    msg.set_content(txt)
    if cfg["MAIL_BACKEND"] == "console":
        ENVIADOS.append(msg); del ENVIADOS[:-50]
        log.warning("CORREO (console) para %s: %s\n%s", destino, asunto, txt)
        return
    if cfg["MAIL_BACKEND"] == "brevo":
        try:
            brevo.enviar(cfg["BREVO_API_KEY"], cfg["MAIL_FROM"], destino, msg["Subject"], txt, html=htm)
        except brevo.ErrorCorreo as e:
            raise OSError(str(e)) from e
        return
    msg.add_alternative(htm, subtype="html")
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


def enviar_seguro(destino, asunto, cuerpo, **kw) -> bool:
    """Para alertas no críticas: un fallo del correo no debe tumbar el inicio de sesión."""
    try:
        enviar(destino, asunto, cuerpo, **kw); return True
    except (OSError, smtplib.SMTPException):
        log.exception("No se pudo enviar el correo a %s", destino); return False
