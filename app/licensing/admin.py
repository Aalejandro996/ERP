"""Cliente de la API de administración del servidor de licencias (lo usa la consola del Maestro y la compra de licencias).
Cada petición va firmada con HMAC-SHA256 sobre: marca de tiempo, nonce, método, ruta y SHA-256 del cuerpo.
Un atacante que intercepte una petición no puede repetirla (nonce de un uso, ventana de 5 min) ni alterarla."""
import hashlib
import hmac
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request


class ErrorAdmin(Exception):
    pass


def firma(secreto: str, metodo: str, ruta: str, cuerpo: bytes, t=None, nonce=None) -> str:
    t = str(int(t if t is not None else time.time()))
    nonce = nonce or secrets.token_hex(16)
    base = "\n".join([t, nonce, metodo.upper(), ruta, hashlib.sha256(cuerpo).hexdigest()])
    return f"t={t},n={nonce},f={hmac.new(secreto.encode(), base.encode(), hashlib.sha256).hexdigest()}"


def _http(metodo, url, cuerpo, cabeceras, timeout=20):
    req = urllib.request.Request(url, cuerpo or None, cabeceras, method=metodo)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (URL de configuración)
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class ClienteAdmin:
    def __init__(self, url_base: str, secreto: str, transporte=_http, reloj=time.time):
        u = urllib.parse.urlparse(url_base)
        privado = u.scheme == "http" and u.hostname and ("." not in u.hostname or u.hostname in ("localhost", "127.0.0.1"))
        if u.scheme != "https" and not privado:
            raise ErrorAdmin("El servidor de licencias debe usar HTTPS (o la red privada del proveedor)")
        if not secreto or len(secreto) < 32:
            raise ErrorAdmin("LICENSE_ADMIN_SECRET no configurado")
        self.base, self.secreto, self.http, self.reloj = url_base.rstrip("/"), secreto, transporte, reloj

    def _req(self, metodo, ruta, datos=None, params=None):
        q = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        if q: ruta += "?" + q
        cuerpo = json.dumps(datos).encode() if datos is not None else b""
        cab = {"X-ERP-Firma": firma(self.secreto, metodo, ruta, cuerpo, t=self.reloj()), "Accept": "application/json"}
        if datos is not None: cab["Content-Type"] = "application/json"
        estado, resp = self.http(metodo, self.base + ruta, cuerpo, cab)
        try:
            r = json.loads(resp or b"{}")
        except ValueError:
            r = {}
        if estado >= 400:
            raise ErrorAdmin(r.get("message") or f"Servidor de licencias: error {estado}")
        return r

    # licencias
    def licencias(self, org=None): return self._req("GET", "/admin/v1/licencias", params={"org": org})["licencias"]
    def crear_licencia(self, **d): return self._req("POST", "/admin/v1/licencias", d)
    def modificar(self, lid, **d): return self._req("PATCH", f"/admin/v1/licencias/{int(lid)}", d)
    def liberar(self, lid, autor): return self._req("POST", f"/admin/v1/licencias/{int(lid)}/liberar", {"autor": autor})
    def reenviar(self, lid, correo=None): return self._req("POST", f"/admin/v1/licencias/{int(lid)}/reenviar", {"correo": correo})
    def eventos(self, lid): return self._req("GET", f"/admin/v1/licencias/{int(lid)}/eventos")["eventos"]
    # precios y órdenes
    def precios(self): return self._req("GET", "/admin/v1/precios")["precios"]
    def fijar_precio(self, **d): return self._req("PUT", "/admin/v1/precios", d)
    def crear_orden(self, **d): return self._req("POST", "/admin/v1/ordenes", d)
    def ordenes(self, org=None): return self._req("GET", "/admin/v1/ordenes", params={"org": org})["ordenes"]
    def informar_pago(self, ref, **d): return self._req("POST", f"/admin/v1/ordenes/{ref}/referencia", d)
    def confirmar(self, ref, autor): return self._req("POST", f"/admin/v1/ordenes/{ref}/confirmar", {"autor": autor})
    def rechazar(self, ref, motivo): return self._req("POST", f"/admin/v1/ordenes/{ref}/rechazar", {"motivo": motivo})
    def verificar(self, ref): return self._req("POST", f"/admin/v1/ordenes/{ref}/verificar", {})
    # facturación de Gigadevs
    def fx_config(self): return self._req("GET", "/admin/v1/facturacion/config")
    def fx_guardar(self, **d): return self._req("PUT", "/admin/v1/facturacion/config", d)
    def fx_rango(self, **d): return self._req("POST", "/admin/v1/facturacion/rango", d)
    def facturas(self): return self._req("GET", "/admin/v1/facturas")["facturas"]
    def factura(self, fid): return self._req("GET", f"/admin/v1/facturas/{int(fid)}")["factura"]
    def crear_factura(self, **d): return self._req("POST", "/admin/v1/facturas", d)
    def reenviar_factura(self, fid): return self._req("POST", f"/admin/v1/facturas/{int(fid)}/reenviar", {})
    def anular_factura(self, fid, motivo): return self._req("POST", f"/admin/v1/facturas/{int(fid)}/anular", {"motivo": motivo})
    def facturar_orden(self, ref): return self._req("POST", f"/admin/v1/ordenes/{ref}/facturar", {})
    def pendientes_factura(self, org=None): return self._req("GET", "/admin/v1/facturas/pendientes", params={"org": org})["pendientes"]
    def facturar_cliente(self, org, autor): return self._req("POST", "/admin/v1/facturas/cliente", {"organizacion_ref": str(org), "autor": autor})
