"""Configuración de gunicorn (la carga sola desde la raíz del proyecto, en erp-web y erp-licencias).
El registro de accesos oculta credenciales de las URL (ver app/registro_seguro.py)."""
from gunicorn.glogging import Logger

from app.registro_seguro import ocultar_consulta, ocultar_ruta, ocultar_url


class RegistroSinSecretos(Logger):
    def access(self, resp, req, environ, request_time):
        env = dict(environ)
        env["RAW_URI"] = ocultar_url(env.get("RAW_URI") or "")
        env["PATH_INFO"] = ocultar_ruta(env.get("PATH_INFO") or "")
        env["QUERY_STRING"] = ocultar_consulta(env.get("QUERY_STRING") or "")
        if env.get("HTTP_REFERER"): env["HTTP_REFERER"] = ocultar_url(env["HTTP_REFERER"])
        try:
            req.uri, req.path, req.query = env["RAW_URI"], env["PATH_INFO"], env["QUERY_STRING"]
        except AttributeError:
            pass
        super().access(resp, req, env, request_time)


logger_class = RegistroSinSecretos
