from app.publico import RASTREOS, FiltroRastreos


def _wsgi(ruta):
    llamado = {}
    def app(environ, sr): llamado["si"] = True; sr("200 OK", []); return [b"ok"]
    estado = {}
    cuerpo = FiltroRastreos(app)({"PATH_INFO": ruta}, lambda s, h: estado.setdefault("s", s))
    return estado["s"], llamado.get("si", False), b"".join(cuerpo)


def test_rastreos_se_cortan_sin_llegar_a_flask():
    for r in ("/.env", "/.git/config", "/wp-admin/install.php", "/wp-login.php", "/xmlrpc.php", "/phpmyadmin/", "/index.php",
              "/cgi-bin/luci", "/backup.sql", "/config.yml", "/vendor/phpunit/x", "/.aws/credentials", "/a\x00b"):
        s, paso, _ = _wsgi(r)
        assert s.startswith("404") and not paso, r


def test_rutas_legitimas_pasan():
    for r in ("/", "/login", "/legal/terminos", "/robots.txt", "/sitemap.xml", "/.well-known/security.txt", "/favicon.ico",
              "/static/estilo.css", "/e/1/fiscal/retenciones-iva.txt", "/e/3/facturas/12", "/oc/abc.def"):
        s, paso, _ = _wsgi(r)
        assert paso and s.startswith("200"), r
        assert not RASTREOS.search(r) or r.startswith("/.well-known/")
