"""Integración con Profit Plus — lógica PURA (sin BD ni red): firma de las peticiones del conector y validación de la
«foto» que envía (artículos, existencias por almacén, precios y clientes).

Protocolo del conector (conector_profit/contax_profit.py), siempre de SALIDA desde la red del cliente por HTTPS:
  POST <url>/api/profit/v1/ping   cuerpo JSON pequeño: prueba la conexión y trae la configuración
  POST <url>/api/profit/v1/sync   cuerpo JSON (gzip opcional, Content-Encoding: gzip) con la foto completa
Cabeceras: X-Contax-Conector: <id público> · X-Contax-Fecha: <segundos Unix> ·
           X-Contax-Firma: sha256=<hex HMAC-SHA256(secreto, "<fecha>." + sha256_hex(cuerpo tal cual viaja))>
Una firma vale 5 minutos. La foto reemplaza completa a la anterior: repetirla no altera nada.
Contax NUNCA escribe en Profit Plus: el conector solo hace SELECT con un usuario de lectura."""
import gzip
import hashlib
import hmac
import json
import re
import time
from decimal import Decimal, InvalidOperation

VENTANA_S = 300
MAX_DESCOMPRIMIDO = 40 * 1024 * 1024
ALICUOTAS = ("G", "R", "A", "E")
COD_RE = re.compile(r"^[A-Za-z0-9.\-_/#]{1,40}$")


class ErrorProfit(ValueError):
    pass


# ====================================================================== firma
def firmar(secreto: str, fecha: int, cuerpo: bytes) -> str:
    msg = f"{int(fecha)}.{hashlib.sha256(cuerpo).hexdigest()}".encode()
    return "sha256=" + hmac.new(str(secreto).encode(), msg, hashlib.sha256).hexdigest()


def firma_valida(secreto: str, fecha: str, cuerpo: bytes, firma: str, ahora=None) -> bool:
    if not secreto or not firma or not re.fullmatch(r"\d{9,11}", str(fecha or "")): return False
    if abs((ahora if ahora is not None else time.time()) - int(fecha)) > VENTANA_S: return False
    return hmac.compare_digest(firmar(secreto, int(fecha), cuerpo), str(firma))


def descomprimir(cuerpo: bytes, codificacion: str) -> dict:
    if (codificacion or "").lower() == "gzip":
        try:
            with gzip.GzipFile(fileobj=__import__("io").BytesIO(cuerpo)) as f:
                cuerpo = f.read(MAX_DESCOMPRIMIDO + 1)
        except (OSError, EOFError) as e:
            raise ErrorProfit("Cuerpo gzip inválido") from e
        if len(cuerpo) > MAX_DESCOMPRIMIDO: raise ErrorProfit("La foto supera el tamaño máximo")
    try:
        d = json.loads(cuerpo or b"{}")
    except ValueError as e:
        raise ErrorProfit("JSON inválido") from e
    if not isinstance(d, dict): raise ErrorProfit("Se esperaba un objeto JSON")
    return d


# ====================================================================== normalización
def _txt(v, n) -> str:
    return re.sub(r"\s+", " ", str(v if v is not None else "")).strip()[:n]


def _dec(v, nombre) -> Decimal:
    try:
        d = Decimal(str(v if v not in (None, "") else "0").replace(",", "."))
    except (InvalidOperation, ValueError) as e:
        raise ErrorProfit(f"{nombre} no es un número") from e
    if not d.is_finite() or abs(d) >= Decimal("1e13"): raise ErrorProfit(f"{nombre} fuera de rango")
    return d.quantize(Decimal("0.0001"))


def rif(valor) -> str | None:
    """«J123456789», «j-12345678-9», «J-12345678-9 » → «J-12345678-9». None si no parece un RIF."""
    m = re.fullmatch(r"([VEJPGC])\s*-?\s*(\d{8})\s*-?\s*(\d)", _txt(valor, 20).upper())
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def whatsapp_de(telefonos) -> str | None:
    """Primer celular venezolano (04xx) o número internacional de la lista de teléfonos de Profit («0414-1234567 / 0212…»)."""
    for parte in re.split(r"[/;,|]|\s{2,}|\s+y\s+", str(telefonos or "")):
        d = re.sub(r"\D", "", parte)
        if d.startswith("00"): d = d[2:]
        if len(d) == 11 and d.startswith("04"): return "58" + d[1:]
        if len(d) == 12 and d.startswith("584"): return d
    return None


def moneda(co_mone) -> str:
    m = _txt(co_mone, 10).upper().replace("$", "S")
    if m.startswith(("US", "DOL")): return "USD"
    if m.startswith(("EU",)): return "EUR"
    return "VES"


# ====================================================================== foto
def validar_foto(d: dict, max_articulos=20000, max_clientes=50000) -> dict:
    """Valida y limpia la foto. Devuelve {articulos, clientes, disponibles, version, equipo}. Lanza ErrorProfit."""
    arts, clis = d.get("articulos"), d.get("clientes")
    if not isinstance(arts, list) or not isinstance(clis, list): raise ErrorProfit("Faltan «articulos» o «clientes»")
    if len(arts) > max_articulos: raise ErrorProfit(f"Demasiados artículos ({len(arts)}; máximo {max_articulos})")
    if len(clis) > max_clientes: raise ErrorProfit(f"Demasiados clientes ({len(clis)}; máximo {max_clientes})")
    articulos, vistos = [], set()
    for a in arts:
        if not isinstance(a, dict): raise ErrorProfit("Artículo inválido")
        cod = _txt(a.get("codigo"), 40)
        if not COD_RE.fullmatch(cod) or cod.upper() in vistos: continue      # códigos con espacios u otros símbolos no se pueden pedir por chat
        vistos.add(cod.upper())
        al = str(a.get("alicuota") or "G").upper()
        pa = a.get("por_almacen") if isinstance(a.get("por_almacen"), dict) else {}
        articulos.append(dict(codigo=cod, nombre=_txt(a.get("nombre"), 200) or cod, linea=_txt(a.get("linea"), 40) or None,
                              alicuota=al if al in ALICUOTAS else "G", precio=max(_dec(a.get("precio"), f"Precio de {cod}"), Decimal(0)),
                              moneda=a.get("moneda") if a.get("moneda") in ("VES", "USD", "EUR") else moneda(a.get("moneda")),
                              existencia=max(_dec(a.get("existencia"), f"Existencia de {cod}"), Decimal(0)),
                              por_almacen={_txt(k, 10): str(_dec(v, f"Existencia de {cod}")) for k, v in list(pa.items())[:50]}))
    clientes, vistos = [], set()
    for c in clis:
        if not isinstance(c, dict): raise ErrorProfit("Cliente inválido")
        cod = _txt(c.get("codigo"), 40)
        if not cod or cod in vistos: continue
        vistos.add(cod)
        tels = _txt(c.get("telefonos"), 120) or None
        clientes.append(dict(codigo=cod, nombre=_txt(c.get("nombre"), 200) or cod, rif=rif(c.get("rif")), direccion=_txt(c.get("direccion"), 300) or None,
                             telefonos=tels, whatsapp=whatsapp_de(tels), email=_txt(c.get("email"), 120) or None))
    disp = d.get("disponibles") if isinstance(d.get("disponibles"), dict) else {}
    lista = lambda k: [dict(codigo=_txt(x.get("codigo"), 10), nombre=_txt(x.get("nombre"), 80)) for x in (disp.get(k) or [])[:100]
                       if isinstance(x, dict) and _txt(x.get("codigo"), 10)]
    return dict(articulos=articulos, clientes=clientes, disponibles=dict(almacenes=lista("almacenes"), listas_precio=lista("listas_precio")),
                version=_txt(d.get("version"), 20) or None, equipo=_txt(d.get("equipo"), 60) or None)


def config_para_conector(conexion, intervalo_min) -> dict:
    """Lo que el conector debe aplicar en su próxima lectura (lo elige la empresa en Contax, no se edita en el equipo)."""
    return dict(activo=bool(conexion.activo), almacenes=list(conexion.almacenes or []), lista_precio=conexion.lista_precio or "01",
                restar_comprometido=bool(conexion.restar_comprometido), intervalo_min=int(intervalo_min))
