"""Los registros de acceso no guardan credenciales que viajan en la URL."""
import io
import logging
from types import SimpleNamespace

from app.registro_seguro import ocultar_url


def test_oculta_codigo_de_google_y_enlaces_de_un_solo_uso():
    u = ocultar_url("/plataforma/drive/google?state=O7kDZ9&iss=https://accounts.google.com&code=4/0AUn6Hd-MTmy&scope=openid&authuser=2")
    assert "O7kDZ9" not in u and "4/0AUn6Hd" not in u and "code=[oculto]" in u and "state=[oculto]" in u and "authuser=2" in u
    for ruta in ("/invitacion/abc123", "/recuperar/abc123", "/correo/confirmar/abc123", "/contrato/abc123", "/legal/autorizar/abc123",
                 "/oc/abc123", "/ov/abc123", "/pv/abc123", "/wa/abc123", "/admin/v1/legal/autorizaciones/abc123"):
        assert "abc123" not in ocultar_url(ruta) and ocultar_url(ruta).endswith("/[oculto]"), ruta
    assert ocultar_url("/plataforma/legal/12?x=1") == "/plataforma/legal/12?x=1"          # lo demás queda igual
    assert ocultar_url("/salud") == "/salud"


def test_el_log_de_gunicorn_no_escribe_el_codigo():
    import importlib.util, os
    spec = importlib.util.spec_from_file_location("gconf", os.path.join(os.path.dirname(__file__), "..", "gunicorn.conf.py"))
    gconf = importlib.util.module_from_spec(spec); spec.loader.exec_module(gconf)
    from gunicorn.config import Config
    cfg = Config(); cfg.set("accesslog", "-")
    lg = gconf.RegistroSinSecretos(cfg)
    salida = io.StringIO(); h = logging.StreamHandler(salida); lg.access_log.handlers = [h]
    uri = "/plataforma/drive/google?state=SECRETO1&code=SECRETO2"
    environ = {"REQUEST_METHOD": "GET", "RAW_URI": uri, "PATH_INFO": "/plataforma/drive/google", "QUERY_STRING": uri.split("?")[1],
               "SERVER_PROTOCOL": "HTTP/1.1", "REMOTE_ADDR": "1.2.3.4", "HTTP_REFERER": "https://erp/invitacion/SECRETO3"}
    req = SimpleNamespace(uri=uri, path="/plataforma/drive/google", query=uri.split("?")[1], headers=[], method="GET", version=(1, 1))
    resp = SimpleNamespace(status="302 FOUND", response_length=10, sent=10, headers=[])
    import datetime
    lg.access(resp, req, environ, datetime.timedelta(seconds=0.01))
    linea = salida.getvalue()
    assert "/plataforma/drive/google" in linea and "302" in linea, linea
    assert "SECRETO" not in linea, linea
