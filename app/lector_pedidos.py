"""Lectura de la orden de compra (PDF) que un cliente o proveedor envía por WhatsApp. Lógica pura y testeable.

  texto_pdf(bytes)            → texto del PDF (solo PDF digitales; un escaneo/imagen no trae texto: se pide otro formato)
  interpretar(texto, catálogo) → qué productos del inventario se piden y en qué cantidad, RIF y N° de la orden del cliente

Cómo reconoce cada línea: primero por CÓDIGO del producto (palabra exacta), si no por NOMBRE (coincidencia aproximada);
la cantidad es el primer número de la línea fuera del código y de la descripción. Lo que no reconoce se informa
(«no identificado») para que la empresa lo revise; nunca se inventa un producto."""
import difflib
import io
import re
import unicodedata
from decimal import Decimal, InvalidOperation

RE_RIF = re.compile(r"\b([VEJPGC])\s*-?\s*(\d{8})\s*-?\s*(\d)\b", re.I)
RE_ORDEN = re.compile(r"(?:orden\s+de\s+compra|o\.?\s*c\.?|pedido|purchase\s+order|p\.?o\.?)\s*(?:n(?:ro|°|º|o)?\.?|#|:)?\s*([A-Z0-9][A-Z0-9\-/]{0,24})", re.I)
RE_NUM = re.compile(r"(?<![\w.,])(\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:[.,]\d+)?)(?![\w])")


class ErrorLectura(ValueError):
    pass


def texto_pdf(contenido: bytes) -> str:
    if not contenido.startswith(b"%PDF"): raise ErrorLectura("El archivo no es un PDF")
    try:
        from pypdf import PdfReader
        r = PdfReader(io.BytesIO(contenido))
        if r.is_encrypted: raise ErrorLectura("El PDF está protegido con clave")
        texto = "\n".join((p.extract_text() or "") for p in r.pages[:20])
    except ErrorLectura:
        raise
    except Exception as e:  # noqa: BLE001 — PDF dañado
        raise ErrorLectura("No se pudo leer el PDF") from e
    if len(texto.strip()) < 10:
        raise ErrorLectura("El PDF parece una imagen escaneada (no tiene texto). Envíe el PDF original del sistema o escriba los productos en el chat")
    return texto


def _n(t) -> str:
    t = unicodedata.normalize("NFKD", str(t or "")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", t).strip().lower()


def _cantidad(tok):
    t = tok
    if "." in t and "," in t: t = t.replace(".", "").replace(",", ".")
    elif "," in t: t = t.replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", t): t = t.replace(".", "")
    try:
        d = Decimal(t)
    except InvalidOperation:
        return None
    return d if d > 0 else None


def interpretar(texto: str, catalogo: dict) -> dict:
    """catalogo: {codigo: (nombre, precio, existencia)}. Devuelve {lineas: [{codigo, descripcion, solicitado}], no_identificadas,
    rif, referencia}. Si un producto aparece en varias líneas, se suman."""
    rif = RE_RIF.search(texto)
    ref = RE_ORDEN.search(texto)
    por_cod = {_n(c): c for c in catalogo}
    nombres = {_n(v[0]): c for c, v in catalogo.items()}
    pedido, no_id = {}, []
    for linea in texto.splitlines():
        ln = linea.strip()
        if len(ln) < 3 or not RE_NUM.search(ln): continue
        nl = _n(ln)
        if re.search(r"\b(total|subtotal|iva|base imponible|rif|fecha|telefono|tlf|direccion|pagina|i\.v\.a)\b", nl) and not any(
                re.search(rf"(?<![\w]){re.escape(k)}(?![\w])", nl) for k in por_cod): continue
        cod = next((c for k, c in por_cod.items() if re.search(rf"(?<![\w-]){re.escape(k)}(?![\w-])", nl)), None)
        resto = nl
        if cod:
            resto = re.sub(rf"(?<![\w-]){re.escape(_n(cod))}(?![\w-])", " ", resto, count=1)
            nom = _n(catalogo[cod][0])
            resto = resto.replace(nom, " ")
        else:
            letras = re.sub(r"[\d.,$%]+", " ", nl)
            letras = re.sub(r"\b(und|unid|unidades|kg|kgs|lt|lts|caja|cajas|bulto|bultos|pza|pzas|x|bs|usd|ref)\b", " ", letras)
            letras = re.sub(r"\s+", " ", letras).strip()
            if len(letras) < 3: continue
            cerca = difflib.get_close_matches(letras, list(nombres), n=1, cutoff=0.72)
            if not cerca:
                cerca = [k for k in nombres if len(k) >= 5 and k in nl][:1]
            if cerca:
                cod = nombres[cerca[0]]
                resto = nl.replace(cerca[0], " ") if cerca[0] in nl else re.sub(r"[a-z]+", " ", nl)
        nums = [x for x in RE_NUM.findall(resto)]
        cant = next((c for c in (_cantidad(x) for x in nums) if c is not None), None)
        if cod and cant:
            pedido[cod] = pedido.get(cod, Decimal(0)) + cant
        elif re.search(r"[a-z]{3,}", nl) and nums:
            no_id.append(ln[:120])
    return dict(lineas=[dict(codigo=c, descripcion=catalogo[c][0], solicitado=format(q.normalize(), "f")) for c, q in pedido.items()],
                no_identificadas=no_id[:20], rif=f"{rif.group(1).upper()}-{rif.group(2)}-{rif.group(3)}" if rif else None,
                referencia=ref.group(1)[:25] if ref else None)


def despachable(lineas, catalogo):
    """Ajusta lo pedido a lo disponible en inventario: [{..., solicitado, servido, precio}], faltantes."""
    out, faltan = [], []
    for l in lineas:
        nom, precio, exist = catalogo[l["codigo"]]
        sol = Decimal(l["solicitado"])
        disp = sol if exist is None else max(min(sol, Decimal(str(exist))), Decimal(0))
        if disp < sol: faltan.append(f"{l['codigo']} {nom}: pidió {format(sol.normalize(), 'f')}, hay {format(disp.normalize(), 'f')}")
        if disp > 0:
            out.append(dict(codigo=l["codigo"], descripcion=nom, solicitado=l["solicitado"], cantidad=format(disp.normalize(), "f"), precio=str(precio)))
    return out, faltan
