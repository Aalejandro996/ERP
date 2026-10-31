"""Conversación del bot de WhatsApp de la empresa. Lógica PURA (sin BD ni red): recibe el texto, el tipo de contacto,
su estado y el catálogo; devuelve las respuestas, el estado nuevo y los EVENTOS que la capa web persiste
(crear pedido, confirmar/rechazar una orden de compra, clasificar al contacto).

CLIENTES: ver catálogo, armar un pedido («COD 3»), revisarlo, confirmarlo y consultar el estado de sus pedidos.
PROVEEDORES: ver las órdenes de compra que la empresa les emitió, confirmarlas o rechazarlas, y enviar ofertas o
cotizaciones (líneas «COD cantidad precio» o texto libre) que llegan al ERP como pedido de compra."""
import re
import unicodedata
from decimal import Decimal, InvalidOperation

MAX_CARRITO = 30
MAX_CATALOGO = 40


def _n(t) -> str:
    t = unicodedata.normalize("NFKD", str(t or "")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", t).strip().lower()


def _dec(v):
    try:
        d = Decimal(str(v).replace(",", "."))
        return d if d.is_finite() and d > 0 else None
    except (InvalidOperation, ValueError):
        return None


def bs(v) -> str:
    return f"{Decimal(str(v)):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def menu(tipo, empresa) -> str:
    if tipo == "proveedor":
        return (f"*{empresa}* · Menú de proveedores\n"
                "1️⃣ Órdenes de compra pendientes\n2️⃣ Enviar oferta o cotización\n3️⃣ Hablar con una persona\n"
                "Para responder una orden: *CONFIRMAR 12* o *RECHAZAR 12 motivo*.")
    return (f"*{empresa}* · ¿Qué desea hacer?\n"
            "1️⃣ Ver catálogo\n2️⃣ Ver mi pedido\n3️⃣ Confirmar pedido\n4️⃣ Estado de mis pedidos\n5️⃣ Hablar con una persona\n"
            "Para pedir escriba el código y la cantidad, p. ej. *A001 3*. Para quitar: *QUITAR A001*. *VACIAR* borra el pedido.")


def _carrito_texto(carrito, catalogo):
    if not carrito: return "Su pedido está vacío. Escriba *1* para ver el catálogo.", Decimal(0)
    lineas, total = [], Decimal(0)
    for cod, cant in carrito.items():
        nom, precio, _ = catalogo.get(cod, (cod, Decimal(0), None))
        sub = Decimal(str(precio)) * Decimal(str(cant)); total += sub
        lineas.append(f"• {cod} {nom} × {cant} = Bs {bs(sub)}")
    return "🛒 *Su pedido*\n" + "\n".join(lineas) + f"\n*Total referencial: Bs {bs(total)}* (IVA no incluido)\nEscriba *3* para confirmarlo.", total


def responder(texto, tipo, estado, catalogo, empresa, pedidos=(), ordenes=(), bienvenida=None):
    """texto: mensaje recibido. tipo: nuevo|cliente|proveedor. estado: dict persistido del contacto.
    catalogo: {codigo: (nombre, precio_bs, existencia)}. pedidos: [(numero, estado, total)] del contacto.
    ordenes: [(numero, estado, total, moneda)] de órdenes de compra dirigidas a este proveedor.
    Devuelve (respuestas: list[str], estado: dict, eventos: list[dict])."""
    t, est, ev = _n(texto), dict(estado or {}), []
    carrito = {k: v for k, v in (est.get("carrito") or {}).items()}

    if tipo == "bloqueado": return [], est, ev
    if tipo == "nuevo":
        if t in ("1", "cliente", "soy cliente"):
            ev.append({"tipo_contacto": "cliente"}); return [menu("cliente", empresa)], {}, ev
        if t in ("2", "proveedor", "soy proveedor"):
            ev.append({"tipo_contacto": "proveedor"}); return [menu("proveedor", empresa)], {}, ev
        saludo = bienvenida or f"¡Hola! Gracias por escribir a *{empresa}*."
        return [saludo + "\nPara atenderle mejor responda:\n1️⃣ si es *cliente*\n2️⃣ si es *proveedor*"], est, ev

    if t in ("hola", "menu", "inicio", "0", "buenas", "buenos dias", "buenas tardes", "buenas noches", "ayuda"):
        est.pop("paso", None)
        return [menu(tipo, empresa)], est, ev

    # ------------------------------------------------------------ proveedores
    if tipo == "proveedor":
        if est.get("paso") == "oferta":
            if t in ("fin", "enviar", "listo"):
                lineas, notas = [], []
                for ln in est.get("oferta", []):
                    m = re.fullmatch(r"([A-Za-z0-9.\-_]{1,40})\s+(\d+(?:[.,]\d+)?)\s+(\d+(?:[.,]\d+)?)", ln.strip())
                    if m and _dec(m.group(2)) and _dec(m.group(3)):
                        cod = m.group(1).upper()
                        lineas.append({"codigo": cod, "descripcion": catalogo.get(cod, (cod,))[0], "cantidad": str(_dec(m.group(2))), "precio": str(_dec(m.group(3)))})
                    else: notas.append(ln)
                if not lineas and not notas: return ["No recibimos ninguna línea. Escriba su oferta o *CANCELAR*."], est, ev
                ev.append({"crear_pedido": {"tipo": "compra", "lineas": lineas, "nota": "\n".join(notas)[:2000]}})
                return ["✅ Recibimos su oferta. Nuestro equipo de compras la revisará y le responderá por aquí."], {}, ev
            if t == "cancelar": return ["Oferta cancelada.", menu("proveedor", empresa)], {}, ev
            of = (est.get("oferta") or []) + [ln for ln in str(texto).splitlines() if ln.strip()][:50]
            return ["Anotado. Siga escribiendo o envíe *FIN* para remitir la oferta."], {**est, "oferta": of[:200]}, ev
        m = re.fullmatch(r"(confirmar|aceptar|rechazar)\s*(?:oc)?\s*#?\s*(\d{1,9})\s*(.*)", t)
        if m:
            num, mias = int(m.group(2)), {o[0]: o for o in ordenes}
            if num not in mias: return [f"No encontramos la orden de compra N° {num} dirigida a usted."], est, ev
            if mias[num][1] not in ("Emitida", "Enviada"): return [f"La orden N° {num} ya está {mias[num][1].lower()}."], est, ev
            if m.group(1) == "rechazar":
                ev.append({"oc_rechazada": num, "motivo": str(texto).split(None, 2)[2][:300] if len(str(texto).split(None, 2)) > 2 else "Sin motivo"})
                return [f"Registramos el rechazo de la orden N° {num}. Gracias por avisar."], est, ev
            ev.append({"oc_confirmada": num})
            return [f"✅ Orden de compra N° {num} confirmada. ¡Gracias! Le esperamos según las condiciones de entrega."], est, ev
        if t == "1":
            pend = [o for o in ordenes if o[1] in ("Emitida", "Enviada", "Confirmada")]
            if not pend: return ["No tiene órdenes de compra pendientes con nosotros."], est, ev
            return ["📄 *Órdenes de compra*\n" + "\n".join(f"• N° {o[0]} · {o[1]} · {o[3]} {bs(o[2])}" for o in pend[:20])
                    + "\nResponda *CONFIRMAR N°* o *RECHAZAR N° motivo*."], est, ev
        if t == "2":
            return ["Escriba su oferta. Una línea por producto: *CÓDIGO CANTIDAD PRECIO* (p. ej. A001 50 2,35) o texto libre. "
                    "Cuando termine envíe *FIN*."], {"paso": "oferta", "oferta": []}, ev
        if t == "3":
            ev.append({"atencion": True}); return ["Un miembro del equipo le responderá en breve por este chat."], est, ev
        return ["No entendí su mensaje. Escriba *MENÚ* para ver las opciones."], est, ev

    # ------------------------------------------------------------ clientes
    if t == "1":
        if not catalogo: return ["Por ahora no tenemos productos publicados. Escriba *5* para hablar con una persona."], est, ev
        lin = [f"• *{c}* {v[0]} — Bs {bs(v[1])}" + ("" if v[2] is None else (" ✅" if Decimal(str(v[2])) > 0 else " (agotado)"))
               for c, v in list(catalogo.items())[:MAX_CATALOGO]]
        return ["📦 *Catálogo*\n" + "\n".join(lin) + "\nPara pedir: código y cantidad, p. ej. *" + next(iter(catalogo)) + " 2*"], est, ev
    if t == "2":
        return [_carrito_texto(carrito, catalogo)[0]], est, ev
    if t in ("3", "confirmar", "confirmar pedido"):
        if not carrito: return ["Su pedido está vacío. Escriba *1* para ver el catálogo."], est, ev
        txt, total = _carrito_texto(carrito, catalogo)
        lineas = [{"codigo": c, "descripcion": catalogo.get(c, (c,))[0], "cantidad": str(q), "precio": str(catalogo.get(c, (c, 0))[1])} for c, q in carrito.items()]
        ev.append({"crear_pedido": {"tipo": "venta", "lineas": lineas, "total": str(total)}})
        return ["✅ ¡Pedido recibido! Le avisaremos por aquí cuando sea confirmado.\n" + txt.split("\nEscriba")[0]], {}, ev
    if t == "4":
        if not pedidos: return ["Aún no tiene pedidos con nosotros."], est, ev
        return ["📋 *Sus pedidos*\n" + "\n".join(f"• N° {p[0]} · {p[1]} · Bs {bs(p[2])}" for p in pedidos[:10])], est, ev
    if t == "5":
        ev.append({"atencion": True}); return ["Un miembro del equipo le responderá en breve por este chat."], est, ev
    if t == "vaciar":
        return ["Su pedido quedó vacío."], {k: v for k, v in est.items() if k != "carrito"}, ev
    m = re.fullmatch(r"(?:quitar|eliminar|borrar)\s+([a-z0-9.\-_]{1,40})", t)
    if m:
        cod = next((c for c in carrito if c.lower() == m.group(1)), None)
        if cod is None: return [f"{m.group(1).upper()} no está en su pedido."], est, ev
        carrito.pop(cod)
        return [f"Quitado {cod}.\n" + _carrito_texto(carrito, catalogo)[0]], {**est, "carrito": carrito}, ev
    m = re.fullmatch(r"(?:pedir\s+)?([a-z0-9.\-_]{1,40})\s*(?:x|\*|×)?\s*(\d+(?:[.,]\d+)?)", t)
    if m:
        cod = next((c for c in catalogo if c.lower() == m.group(1)), None)
        cant = _dec(m.group(2))
        if cod is None: return [f"No encontramos el código {m.group(1).upper()}. Escriba *1* para ver el catálogo."], est, ev
        if cant is None: return ["La cantidad debe ser mayor que cero."], est, ev
        exist = catalogo[cod][2]
        if exist is not None and Decimal(str(exist)) < cant:
            return [f"Solo tenemos {Decimal(str(exist)).normalize():f} unidades de {cod}."], est, ev
        if cod not in carrito and len(carrito) >= MAX_CARRITO: return ["Su pedido alcanzó el máximo de productos."], est, ev
        carrito[cod] = format(cant.normalize(), "f")
        return [f"Agregado: {cod} {catalogo[cod][0]} × {carrito[cod]}.\nEscriba *2* para ver su pedido o *3* para confirmarlo."], {**est, "carrito": carrito}, ev
    return ["No entendí su mensaje. Escriba *MENÚ* para ver las opciones."], est, ev
