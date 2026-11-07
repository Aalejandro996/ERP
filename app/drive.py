"""Google Drive de Gigadevs: cada documento legal aprobado, cada aceptación y cada factura se guarda en Drive en cuanto ocurre, ordenado:

    Gigadevs Corp · Contax/
      Legal/<año>/<Sistema>/Documentos aprobados/   Contrato … v3 (2026-11-07).
      Legal/<año>/<Sistema>/Aceptaciones/<RIF>/     Términos … v2 · ana · 2026-11-07 10.32.
      Facturas/<año>/<mes>/<Sistema>/               Factura 00000031 · Cliente (J-…).

Conexión: «Iniciar sesión con Google» (OAuth 2.0, código + secreto en el servidor, `state` de un solo uso). Se pide solo el permiso
drive.file: Contax ve únicamente las carpetas y archivos que él mismo crea, nunca el resto del Drive. El token de actualización se
guarda cifrado. Si Drive no está conectado o no responde, nada se detiene: se registra y se reintenta en la siguiente sincronización."""
import base64
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from flask import current_app

log = logging.getLogger(__name__)

AUTORIZAR = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
REVOCAR = "https://oauth2.googleapis.com/revoke"
API = "https://www.googleapis.com/drive/v3/files"
SUBIR = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id"
PERMISOS = "openid email https://www.googleapis.com/auth/drive.file"
CARPETA = "application/vnd.google-apps.folder"
DOC = "application/vnd.google-apps.document"
NOMBRES_SISTEMA = {"contax": "Contax", "criteriseg": "CriteriSeg", "bot_whatsapp": "Enlace MIT", "surtia": "Surtia",
                   "control_financiero": "Quincena VE"}
TRANSPORTE = None          # pruebas: fn(metodo, url, cabeceras, cuerpo) -> (estado, cuerpo)
_acceso = {}               # token de acceso en memoria: {"token", "vence"}


class ErrorDrive(RuntimeError):
    pass


class _SinRedirecciones(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None


_ABRIDOR = urllib.request.build_opener(_SinRedirecciones)


def _http(metodo, url, cab=None, cuerpo=None):
    if not url.startswith("https://"): raise ErrorDrive("Solo se habla con Google por https")
    if TRANSPORTE: return TRANSPORTE(metodo, url, cab or {}, cuerpo)
    req = urllib.request.Request(url, cuerpo, cab or {}, method=metodo)
    try:
        with _ABRIDOR.open(req, timeout=20) as r: return r.status, r.read(2 * 1024 * 1024)   # noqa: S310 (https fijo)
    except urllib.error.HTTPError as e: return e.code, e.read(65536)
    except (urllib.error.URLError, OSError) as e: raise ErrorDrive(f"Google Drive no respondió: {e}") from e


def _json(estado, cuerpo, que):
    try: d = json.loads(cuerpo or b"{}")
    except ValueError: d = {}
    if estado >= 400:
        err = d.get("error")
        msg = d.get("error_description") or (err.get("message") if isinstance(err, dict) else err)
        raise ErrorDrive(f"Google Drive rechazó {que} ({estado}): {msg or 'sin detalle'}")
    return d


# ------------------------------------------------------------------ conexión
def configurado() -> bool:
    c = current_app.config
    return bool(c.get("GOOGLE_OAUTH_CLIENT_ID") and c.get("GOOGLE_OAUTH_CLIENT_SECRET"))


def conexion():
    from .extensions import db
    from .models import DriveConexion
    return db.session.get(DriveConexion, 1)


def redireccion() -> str:
    return current_app.config["APP_BASE_URL"].rstrip("/") + "/plataforma/drive/google"


def url_autorizacion(state: str) -> str:
    q = {"client_id": current_app.config["GOOGLE_OAUTH_CLIENT_ID"], "redirect_uri": redireccion(), "response_type": "code",
         "scope": PERMISOS, "access_type": "offline", "prompt": "consent select_account", "include_granted_scopes": "false", "state": state}
    return AUTORIZAR + "?" + urllib.parse.urlencode(q)


def _correo_de(id_token: str) -> str:
    """Correo de la cuenta que autorizó. El id_token llega directo de Google por TLS en el intercambio del código (OIDC, flujo
    de código), así que basta leer su contenido; se valida que sea para nuestro cliente."""
    try:
        carga = id_token.split(".")[1]
        d = json.loads(base64.urlsafe_b64decode(carga + "=" * (-len(carga) % 4)))
    except (IndexError, ValueError) as e:
        raise ErrorDrive("Google no devolvió la identidad de la cuenta") from e
    if d.get("aud") != current_app.config["GOOGLE_OAUTH_CLIENT_ID"] or d.get("iss") not in ("https://accounts.google.com", "accounts.google.com"):
        raise ErrorDrive("La identidad devuelta por Google no corresponde a Contax")
    if not d.get("email") or not d.get("email_verified", True): raise ErrorDrive("La cuenta de Google no tiene un correo verificado")
    return d["email"].lower()


def completar(code: str, autor: str) -> str:
    """Canjea el código de Google por el token de actualización y lo guarda cifrado. Devuelve el correo de la cuenta."""
    from .extensions import db
    from .models import DriveConexion
    c = current_app.config
    cuerpo = urllib.parse.urlencode({"code": code, "client_id": c["GOOGLE_OAUTH_CLIENT_ID"], "client_secret": c["GOOGLE_OAUTH_CLIENT_SECRET"],
                                     "redirect_uri": redireccion(), "grant_type": "authorization_code"}).encode()
    d = _json(*_http("POST", TOKEN, {"Content-Type": "application/x-www-form-urlencoded"}, cuerpo), "el inicio de sesión")
    if "drive.file" not in (d.get("scope") or ""): raise ErrorDrive("Falta el permiso de Drive: vuelva a conectar y marque la casilla de Google Drive")
    if not d.get("refresh_token"): raise ErrorDrive("Google no entregó acceso permanente: quite Contax en myaccount.google.com/permissions y conecte de nuevo")
    correo = _correo_de(d.get("id_token") or "")
    cif = current_app.extensions["cifrador"]
    from .models import _ahora
    fila = conexion() or DriveConexion(id=1)
    fila.correo, fila.refresh_token_cifrado, fila.conectado_por, fila.conectado_en = correo, cif.cifrar(d["refresh_token"]), autor, _ahora()
    db.session.add(fila); db.session.commit()
    _acceso.clear(); _acceso.update(token=d.get("access_token"), vence=time.time() + int(d.get("expires_in") or 0) - 60, correo=correo)
    return correo


def desconectar(autor: str) -> None:
    """Revoca el acceso en Google (si responde) y borra el token. Lo ya subido queda en Drive."""
    from .extensions import db
    fila = conexion()
    if fila is None: return
    try:
        _http("POST", REVOCAR, {"Content-Type": "application/x-www-form-urlencoded"},
              urllib.parse.urlencode({"token": current_app.extensions["cifrador"].descifrar(fila.refresh_token_cifrado)}).encode())
    except ErrorDrive as e:
        log.warning("Drive: no se pudo revocar en Google: %s", e)
    db.session.delete(fila); db.session.commit(); _acceso.clear()
    log.info("Drive desconectado por %s", autor)


def _token() -> str:
    fila = conexion()
    if fila is None: raise ErrorDrive("Google Drive no está conectado")
    if _acceso.get("token") and _acceso.get("vence", 0) > time.time() and _acceso.get("correo") == fila.correo: return _acceso["token"]
    c = current_app.config
    cuerpo = urllib.parse.urlencode({"client_id": c["GOOGLE_OAUTH_CLIENT_ID"], "client_secret": c["GOOGLE_OAUTH_CLIENT_SECRET"], "grant_type": "refresh_token",
                                     "refresh_token": current_app.extensions["cifrador"].descifrar(fila.refresh_token_cifrado)}).encode()
    d = _json(*_http("POST", TOKEN, {"Content-Type": "application/x-www-form-urlencoded"}, cuerpo), "la renovación del acceso")
    _acceso.clear(); _acceso.update(token=d["access_token"], vence=time.time() + int(d.get("expires_in") or 3600) - 60, correo=fila.correo)
    return _acceso["token"]


# ------------------------------------------------------------------ carpetas y archivos
def _registrado(clave):
    from .extensions import db
    from .models import DriveArchivo
    return db.session.get(DriveArchivo, clave)


def _registrar(clave, archivo_id, ruta):
    from .extensions import db
    from .models import DriveArchivo
    db.session.merge(DriveArchivo(clave=clave, archivo_id=archivo_id, ruta=ruta)); db.session.commit()


def _seguro(nombre: str) -> str:
    return " ".join(str(nombre).replace("/", "-").replace("\\", "-").split())[:180] or "Sin nombre"


def carpeta(*ruta) -> str:
    """Id de la carpeta (la crea si falta, y también las de arriba). Cada carpeta se crea una sola vez."""
    partes = [current_app.config.get("DRIVE_CARPETA_RAIZ") or "Gigadevs Corp · Contax"] + [_seguro(p) for p in ruta]
    padre = None
    for i in range(1, len(partes) + 1):
        clave = "carpeta:" + "/".join(partes[:i])
        r = _registrado(clave)
        if r is not None: padre = r.archivo_id; continue
        meta = {"name": partes[i - 1], "mimeType": CARPETA, **({"parents": [padre]} if padre else {})}
        d = _json(*_http("POST", API + "?fields=id", {"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"},
                         json.dumps(meta).encode()), "la creación de la carpeta")
        _registrar(clave, d["id"], "/".join(partes[:i])); padre = d["id"]
    return padre


def subir(clave: str, ruta: tuple, nombre: str, html: str) -> str | None:
    """Sube `html` como documento de Google (legible y con búsqueda) a la carpeta `ruta`. Idempotente por `clave`.
    Devuelve el id del archivo, o None si Drive no está conectado."""
    if not configurado() or conexion() is None: return None
    hecho = _registrado(clave)
    if hecho is not None: return hecho.archivo_id
    padre = carpeta(*ruta)
    limite = "contax" + base64.urlsafe_b64encode(time.time_ns().to_bytes(8, "big")).decode().rstrip("=")
    meta = {"name": _seguro(nombre), "mimeType": DOC, "parents": [padre],
            "description": f"Guardado automáticamente por Contax · {clave}", "appProperties": {"contax": clave[:120]}}
    cuerpo = (f"--{limite}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(meta)}\r\n"
              f"--{limite}\r\nContent-Type: text/html; charset=UTF-8\r\n\r\n").encode() + html.encode() + f"\r\n--{limite}--\r\n".encode()
    d = _json(*_http("POST", SUBIR, {"Authorization": f"Bearer {_token()}", "Content-Type": f"multipart/related; boundary={limite}"}, cuerpo),
              "la subida del documento")
    _registrar(clave, d["id"], "/".join(ruta) + "/" + _seguro(nombre))
    return d["id"]


# ------------------------------------------------------------------ documentos de Gigadevs
def _anio(ts=None) -> str:
    from .tiempo import ahora_ve
    return time.strftime("%Y", time.localtime(ts)) if ts else str(ahora_ve().year)


def subir_documento_aprobado(doc) -> str | None:
    """Documento legal que acaba de entrar en vigencia (aprobado por la abogada y los dos Directores Generales)."""
    from .web_legal import documento_html
    sis = NOMBRES_SISTEMA.get(doc["sistema"], doc["sistema"])
    fecha = time.strftime("%Y-%m-%d", time.localtime(doc.get("aprobado_en") or time.time()))
    return subir(f"legal:{doc['id']}", ("Legal", _anio(doc.get("aprobado_en")), sis, "Documentos aprobados"),
                 f"{doc['titulo']} v{doc['version']} ({fecha})", documento_html(doc))


def subir_aceptacion(doc, usuario, rif, organizacion) -> str | None:
    """Constancia de que `usuario` aceptó `doc` (con el texto exacto que aceptó)."""
    from markupsafe import escape as e
    from .tiempo import ahora_ve
    from .web_legal import documento_html
    sis = NOMBRES_SISTEMA.get(doc["sistema"], doc["sistema"])
    ahora = ahora_ve()
    constancia = (f'<h1>Constancia de aceptación</h1><p><b>{e(usuario)}</b> de <b>{e(organizacion or "—")}</b> ({e(rif or "sin RIF")}) aceptó '
                  f'«{e(doc["titulo"])}» versión {int(doc["version"])} el {ahora:%d/%m/%Y a las %H:%M} (hora de Venezuela).</p>'
                  f'<p>Huella SHA-256 del texto aceptado: <code>{e(doc["huella"])}</code>. La evidencia completa (IP, navegador y cadena de '
                  f'integridad) queda en el registro de aceptaciones de la consola de Gigadevs.</p><hr>')
    base = documento_html(doc)
    html = base.replace("<body>", "<body>" + constancia, 1) if "<body>" in base else constancia + base
    return subir(f"aceptacion:{doc['id']}:{rif}:{usuario}", ("Legal", str(ahora.year), sis, "Aceptaciones", rif or "Sin RIF"),
                 f"{doc['titulo']} v{doc['version']} · {usuario} · {ahora:%Y-%m-%d %H.%M}", html)


def subir_factura(f) -> str | None:
    from . import plantilla_correo as pc
    sis = NOMBRES_SISTEMA.get(f.get("sistema") or "contax", "Contax")
    anio, mes = (f.get("fecha") or "")[:4] or _anio(), (f.get("fecha") or "")[5:7] or "00"
    estado = " (ANULADA)" if f.get("estado") == "Anulada" else ""
    clave = f"factura:{f['id']}" + (":anulada" if estado else "")
    return subir(clave, ("Facturas", anio, mes, sis), f"Factura {f.get('numero_completo') or f['numero']} · {f['cliente_nombre']} ({f['cliente_rif']}){estado}",
                 pc.factura_html(f, current_app.config["APP_BASE_URL"]))


def subir_facturas(facturas) -> tuple[int, list[str]]:
    """Sube las facturas que falten. (cuántas subió, errores). Se detiene al primer fallo de conexión."""
    if not configurado() or conexion() is None: return 0, []
    n, errores = 0, []
    for f in sorted(facturas, key=lambda x: x["id"]):
        clave = f"factura:{f['id']}" + (":anulada" if f.get("estado") == "Anulada" else "")
        if _registrado(clave) is not None: continue
        try:
            subir_factura(f); n += 1
        except ErrorDrive as e:
            errores.append(str(e)); break
    return n, errores
