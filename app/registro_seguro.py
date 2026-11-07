"""Registros sin secretos: antes de escribir una URL en el log (acceso de gunicorn) se ocultan los parámetros y los tramos
de ruta que son credenciales: el código y el `state` de Google al conectar Drive, y los enlaces de un solo uso (invitación,
recuperación de clave, confirmación de correo, contratos, autorización de los Directores, órdenes y presupuestos públicos,
WhatsApp). Aunque esos valores ya no sirvan una vez usados, no deben quedar copiados en los registros de Render.
Sin dependencias de Flask: lo usa gunicorn.conf.py y se prueba de forma aislada."""
import re
from urllib.parse import parse_qsl, urlencode

OCULTO = "[oculto]"
PARAMETROS = {"code", "state", "token", "access_token", "refresh_token", "id_token", "clave", "password", "contrasena", "secret",
              "client_secret", "api_key", "apikey", "key", "firma", "signature", "sig", "codigo", "otp"}
_RUTAS = re.compile(r"(/(?:contrato|invitacion|recuperar|legal/autorizar|correo/confirmar|oc|ov|pv|wa|admin/v1/legal/autorizaciones)/)[^/?#\s]+")


def ocultar_consulta(qs: str) -> str:
    if not qs: return qs
    try:
        pares = parse_qsl(qs, keep_blank_values=True)
    except ValueError:
        return OCULTO
    return urlencode([(k, OCULTO if k.lower() in PARAMETROS else v) for k, v in pares], safe="[]/:@")


def ocultar_ruta(ruta: str) -> str:
    return _RUTAS.sub(lambda m: m.group(1) + OCULTO, ruta or "")


def ocultar_url(uri: str) -> str:
    """«/ruta?consulta» con las credenciales reemplazadas por [oculto]."""
    ruta, sep, qs = (uri or "").partition("?")
    return ocultar_ruta(ruta) + (sep + ocultar_consulta(qs) if sep else "")
