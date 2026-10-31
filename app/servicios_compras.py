"""Servicios de compras (órdenes de compra, proveedores) y del bot de WhatsApp (contactos, mensajes, pedidos)."""
from decimal import ROUND_HALF_UP, Decimal

from flask import current_app
from sqlalchemy import func, select

from . import whatsapp as WA
from .auditoria import registrar
from .contabilidad.validadores import a_decimal, parsear_fecha
from .extensions import db
from .models import Empresa, _ahora
from .models2 import Producto
from .models_compras import (LineaOrdenCompra, OrdenCompra, Pedido, Proveedor, WhatsappConfig, WhatsappContacto,
                             WhatsappMensaje)
from .tiempo import hoy_ve

C2 = Decimal("0.01")


def _siguiente(eid, Modelo) -> int:
    """Consecutivo por empresa sin saltos ni duplicados: se bloquea la fila de la empresa mientras se numera.
    Las órdenes de compra arrancan en el número inicial que eligió la empresa (tutorial / datos de la empresa)."""
    db.session.execute(select(Empresa.id).where(Empresa.id == eid).with_for_update())
    ultimo = db.session.execute(select(func.max(Modelo.numero)).where(Modelo.empresa_id == eid)).scalar()
    if ultimo is None and Modelo is OrdenCompra: return max(db.session.get(Empresa, eid).oc_numero_inicial or 1, 1)
    return (ultimo or 0) + 1


def numero_oc(n) -> str:
    return f"OC-{int(n):06d}"


# ====================================================================== órdenes de compra
def crear_orden_compra(eid, u, datos: dict, lineas: list[dict], origen="manual", pedido_id=None) -> OrdenCompra:
    from .servicios_fact import alicuotas_vigentes, tasa_del_dia
    if not lineas: raise ValueError("Agregue al menos una línea")
    fecha = parsear_fecha(datos.get("fecha") or hoy_ve().isoformat())
    moneda = datos.get("moneda") or "VES"
    if moneda not in ("VES", "USD", "EUR", "USDT"): raise ValueError("Moneda inválida")
    tasa = Decimal(1) if moneda == "VES" else Decimal(str(a_decimal(datos.get("tasa")) if datos.get("tasa") else (tasa_del_dia(fecha, "EUR" if moneda == "EUR" else "USD") or 0)))
    if tasa <= 0: raise ValueError("No hay tasa BCV para esa moneda; indíquela a mano")
    if not (datos.get("dest_nombre") or "").strip() or not (datos.get("dest_rif") or "").strip(): raise ValueError("Indique el nombre y el RIF del destinatario")
    al = alicuotas_vigentes(fecha)
    pct = lambda a: Decimal(0) if a == "E" else (Decimal(str(al["G"])) + Decimal(str(al["A"])) if a == "A" else Decimal(str(al[a])))
    exento = base = iva = Decimal(0)
    oc = OrdenCompra(empresa_id=eid, numero=_siguiente(eid, OrdenCompra), fecha=fecha,
                     fecha_entrega=parsear_fecha(datos["fecha_entrega"]) if datos.get("fecha_entrega") else None,
                     tipo_destinatario=datos.get("tipo_destinatario") or "proveedor", moneda=moneda, tasa_bcv=tasa,
                     **{k: (str(datos.get(k) or "").strip()[:300] or None) for k in ("dest_direccion", "dest_telefono", "dest_email", "condicion_pago", "lugar_entrega")},
                     dest_rif=datos["dest_rif"].strip().upper()[:20], dest_nombre=datos["dest_nombre"].strip()[:200],
                     dest_whatsapp=WA.telefono(datos["dest_whatsapp"]) if datos.get("dest_whatsapp") else None,
                     observaciones=(datos.get("observaciones") or "").strip()[:2000] or None, origen=origen, pedido_id=pedido_id, creado_por=u.username)
    emp = db.session.get(Empresa, eid)   # valores por defecto de la empresa si la orden no los trae
    oc.condicion_pago = oc.condicion_pago or emp.oc_condicion_pago
    oc.lugar_entrega = oc.lugar_entrega or emp.oc_lugar_entrega
    if emp.oc_observaciones: oc.observaciones = ((oc.observaciones + "\n") if oc.observaciones else "") + emp.oc_observaciones
    if oc.tipo_destinatario not in ("proveedor", "cliente"): raise ValueError("Destinatario inválido")
    for l in lineas:
        cant, precio = a_decimal(l["cantidad"]), a_decimal(l.get("precio") or 0)
        if cant <= 0: raise ValueError(f"Cantidad inválida en «{l['descripcion']}»")
        a = l.get("alicuota") or "G"
        if a not in ("G", "R", "A", "E"): raise ValueError("Alícuota inválida")
        monto = (cant * precio).quantize(C2, ROUND_HALF_UP)
        if a == "E": exento += monto
        else: base += monto; iva += (monto * pct(a) / 100).quantize(C2, ROUND_HALF_UP)
        oc.lineas.append(LineaOrdenCompra(empresa_id=eid, codigo=(l.get("codigo") or "").strip()[:40] or None, descripcion=str(l["descripcion"]).strip()[:300],
                                          unidad=(l.get("unidad") or "").strip()[:20] or None, cantidad=cant, precio=precio, alicuota=a, monto=monto))
    oc.exento, oc.base, oc.iva = exento, base, iva
    oc.total = exento + base + iva
    oc.total_bs = (oc.total * tasa).quantize(C2, ROUND_HALF_UP)
    db.session.add(oc); db.session.flush()
    registrar(u.username, "OC_EMITIDA", f"empresa={eid} {numero_oc(oc.numero)} {oc.dest_rif} {moneda} {oc.total}")
    return oc


def orden(eid, oid, bloquear=False) -> OrdenCompra:
    q = select(OrdenCompra).where(OrdenCompra.id == oid, OrdenCompra.empresa_id == eid)
    oc = db.session.execute(q.with_for_update() if bloquear else q).scalar_one_or_none()
    if oc is None: raise ValueError("Orden de compra inexistente")
    return oc


def cambiar_estado_oc(eid, autor, oc: OrdenCompra, nuevo, motivo=None):
    validos = {"Enviada": ("Emitida", "Enviada"), "Confirmada": ("Emitida", "Enviada"), "Rechazada": ("Emitida", "Enviada"),
               "Anulada": ("Emitida", "Enviada", "Confirmada", "Rechazada"), "Recibida": ("Emitida", "Enviada", "Confirmada")}
    if oc.estado not in validos[nuevo]: raise ValueError(f"Una orden {oc.estado.lower()} no puede pasar a {nuevo.lower()}")
    oc.estado = nuevo
    if motivo: oc.motivo = str(motivo)[:300]
    registrar(autor, "OC_" + nuevo.upper(), f"empresa={eid} {numero_oc(oc.numero)}" + (f" motivo={motivo[:80]}" if motivo else ""))


def recibir_orden(eid, u, oc: OrdenCompra, fecha, factura=None, control=None, fecha_factura=None) -> int:
    """Marca la orden como recibida y da entrada al inventario de cada línea con código de producto (costo en Bs).
    Con la factura del proveedor (N° y control) registra además la cuenta por pagar con sus datos fiscales: así la compra
    entra al LIBRO DE COMPRAS y, si las cuentas están configuradas, a la contabilidad con UN asiento cuadrado
    (inventario + compras/gastos + IVA crédito fiscal contra CxP). Sin factura, la recepción queda pendiente en el cuadre."""
    from .servicios import entrada_inventario
    cambiar_estado_oc(eid, u.username, oc, "Recibida")
    n = 0
    for l in oc.lineas:
        if l.codigo and db.session.get(Producto, (eid, l.codigo)):
            entrada_inventario(eid, u, l.codigo, l.cantidad, (l.precio * oc.tasa_bcv).quantize(Decimal("0.0001")), fecha,
                               f"{numero_oc(oc.numero)} {oc.dest_nombre}"[:120], commit=False, contabilizar=False)
            n += 1
    if factura: registrar_factura_proveedor(eid, u, oc, factura, control, fecha_factura or fecha)
    return n


def registrar_factura_proveedor(eid, u, oc: OrdenCompra, factura, control, fecha) -> int:
    from .contabilidad import service as contab
    from .models2 import ConfigAsientos, DocumentoCxP
    if oc.cxp_id: raise ValueError("Esa orden ya tiene registrada la factura del proveedor")
    if oc.estado != "Recibida": raise ValueError("Registre la factura al recibir la mercancía")
    factura, control = str(factura or "").strip()[:40], str(control or "").strip()[:30]
    if not factura or not control: raise ValueError("Indique el N° de factura y el N° de control del proveedor")
    f, t = parsear_fecha(fecha), oc.tasa_bcv
    bs = lambda v: (Decimal(v) * t).quantize(C2, ROUND_HALF_UP)
    inv = sum((bs(l.monto) for l in oc.lineas if l.codigo and l.alicuota != "E" and db.session.get(Producto, (eid, l.codigo))), Decimal(0))
    inv_e = sum((bs(l.monto) for l in oc.lineas if l.codigo and l.alicuota == "E" and db.session.get(Producto, (eid, l.codigo))), Decimal(0))
    exento, base, iva = bs(oc.exento), bs(oc.base), bs(oc.iva)
    total = exento + base + iva
    alic = (iva * 100 / base).quantize(C2) if base else Decimal(0)
    d = DocumentoCxP(empresa_id=eid, numero_documento=factura, numero_control=control, tipo_documento="FAC", fecha_emision=f,
                     fecha_vencimiento=f, monto=total, saldo_pendiente=total, proveedor_nombre=oc.dest_nombre, proveedor_rif=oc.dest_rif,
                     exento=exento, base_imponible=base, alicuota=alic, monto_iva=iva, origen="orden_compra")
    db.session.add(d); db.session.flush()
    cfg = db.session.get(ConfigAsientos, eid)
    c = cfg.como_dict() if cfg else {}
    otros = exento + base - inv - inv_e                  # fletes, servicios y líneas sin producto
    necesarias = ["cuenta_cxp"] + (["cuenta_inventario"] if inv + inv_e else []) + (["cuenta_compras_gastos"] if otros else []) + (["cuenta_iva_credito_fiscal"] if iva else [])
    if all(c.get(k) for k in necesarias):
        lineas = [(c["cuenta_inventario"], inv + inv_e, 0)] if inv + inv_e else []
        if otros: lineas.append((c["cuenta_compras_gastos"], otros, 0))
        if iva: lineas.append((c["cuenta_iva_credito_fiscal"], iva, 0))
        lineas.append((c["cuenta_cxp"], 0, total))
        d.asiento_id = contab.crear_asiento(eid, u, f, f"Compra {factura} — {oc.dest_nombre} ({numero_oc(oc.numero)})", lineas, commit=False)
    oc.cxp_id = d.id
    registrar(u.username, "OC_FACTURA_PROVEEDOR", f"empresa={eid} {numero_oc(oc.numero)} factura={factura} control={control} total={total}")
    return d.id


def oc_dict(oc: OrdenCompra) -> dict:
    e = db.session.get(Empresa, oc.empresa_id)
    return {"numero": oc.numero, "numero_txt": numero_oc(oc.numero), "fecha": str(oc.fecha), "fecha_entrega": str(oc.fecha_entrega) if oc.fecha_entrega else None,
            "estado": oc.estado, "tipo_destinatario": oc.tipo_destinatario, "moneda": oc.moneda, "tasa_bcv": str(oc.tasa_bcv),
            "condicion_pago": oc.condicion_pago, "lugar_entrega": oc.lugar_entrega, "observaciones": oc.observaciones, "creado_por": oc.creado_por,
            "exento": str(oc.exento), "base": str(oc.base), "iva": str(oc.iva), "total": str(oc.total), "total_bs": str(oc.total_bs),
            "emisor": {"razon_social": e.razon_social, "rif": e.rif, "direccion": e.direccion, "telefono": e.telefono, "contribuyente_especial": e.es_sujeto_pasivo_especial,
                       "email": e.correo, "logo": (f"data:{e.logo_mime};base64,{__import__('base64').b64encode(e.logo).decode()}" if e.logo else None)},
            "destinatario": {"nombre": oc.dest_nombre, "rif": oc.dest_rif, "direccion": oc.dest_direccion, "telefono": oc.dest_telefono, "email": oc.dest_email},
            "lineas": [{"codigo": l.codigo, "descripcion": l.descripcion, "unidad": l.unidad, "cantidad": format(l.cantidad.normalize(), "f"),
                        "precio": str(l.precio), "alicuota": l.alicuota, "monto": str(l.monto)} for l in oc.lineas]}


def enlace_publico(oc: OrdenCompra) -> str:
    """Enlace firmado (60 días) para que el proveedor vea la orden sin usuario del ERP."""
    from itsdangerous import URLSafeTimedSerializer
    org = db.session.get(Empresa, oc.empresa_id).organizacion_id
    tok = URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="orden-compra").dumps([org, oc.empresa_id, oc.id])
    return current_app.config["APP_BASE_URL"].rstrip("/") + "/oc/" + tok


def leer_enlace(token):
    from itsdangerous import BadSignature, URLSafeTimedSerializer
    try:
        return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="orden-compra").loads(token, max_age=60 * 86400)
    except BadSignature:
        return None


# ====================================================================== WhatsApp
def config_wa(eid) -> WhatsappConfig | None:
    return db.session.get(WhatsappConfig, eid)


def enviar_wa(eid, destino, texto, contacto=None) -> str:
    """Envía un texto por el número de la empresa y lo deja en la conversación. Lanza ErrorWhatsapp si Meta lo rechaza."""
    cfg = config_wa(eid)
    if cfg is None or not cfg.activo: raise WA.ErrorWhatsapp("La empresa no tiene el bot de WhatsApp configurado y activo")
    cif = current_app.extensions["cifrador"]
    tel = WA.telefono(destino)
    try:
        if cfg.proveedor == "wasender":
            from . import wasender as WS
            wamid = WS.enviar_texto(cfg.api_url, cif.descifrar(cfg.token_cifrado), tel, texto, transporte=current_app.extensions.get("wasender_transporte"))
        else:
            wamid = WA.enviar_texto(cfg.phone_number_id, cif.descifrar(cfg.token_cifrado), tel, texto,
                                    transporte=current_app.extensions.get("whatsapp_transporte") or WA._http)
        wamid = ("ws." + wamid) if wamid and cfg.proveedor == "wasender" else wamid
        res = "ok"
    except WA.ErrorWhatsapp as e:
        wamid, res = None, str(e)[:300]
    if contacto is None:
        contacto = db.session.execute(select(WhatsappContacto).where(WhatsappContacto.empresa_id == eid, WhatsappContacto.telefono == tel)).scalar_one_or_none()
    db.session.add(WhatsappMensaje(empresa_id=eid, contacto_id=contacto.id if contacto else None, telefono=tel, direccion="out", texto=str(texto)[:4000],
                                   wamid=wamid or None, resultado=res))
    if res != "ok": raise WA.ErrorWhatsapp(res)
    return wamid


def catalogo_bot(eid) -> dict:
    return {p.codigo: (p.nombre, p.precio_venta or Decimal(0), p.cantidad_actual)
            for p in db.session.execute(select(Producto).where(Producto.empresa_id == eid, Producto.precio_venta > 0).order_by(Producto.codigo).limit(200)).scalars()}


def procesar_mensaje(eid, telefono, nombre, texto, wamid, adjunto=None, modulos=frozenset()) -> list[str]:
    """Un mensaje entrante: registra, conversa (whatsapp_bot), aplica los eventos y responde. Idempotente por wamid.
    Si trae un PDF (orden de compra del cliente) se lee y se responde con la factura u orden de venta."""
    from . import whatsapp_bot as BOT
    if db.session.execute(select(WhatsappMensaje.id).where(WhatsappMensaje.wamid == wamid)).first(): return []
    tel = WA.telefono(telefono)
    c = db.session.execute(select(WhatsappContacto).where(WhatsappContacto.empresa_id == eid, WhatsappContacto.telefono == tel).with_for_update()).scalar_one_or_none()
    if c is None:
        prov = db.session.execute(select(Proveedor).where(Proveedor.empresa_id == eid, Proveedor.whatsapp == tel)).scalars().first()
        c = WhatsappContacto(empresa_id=eid, telefono=tel, nombre=(prov.razon_social if prov else nombre) or None, tipo="proveedor" if prov else "nuevo",
                             rif=prov.rif if prov else None, estado={})
        db.session.add(c); db.session.flush()
    elif nombre and not c.nombre: c.nombre = nombre[:120]
    c.ultimo_mensaje = _ahora()
    db.session.add(WhatsappMensaje(empresa_id=eid, contacto_id=c.id, telefono=tel, direccion="in", texto=texto, wamid=wamid))
    e, cfg = db.session.get(Empresa, eid), config_wa(eid)
    if adjunto and c.tipo != "bloqueado":
        es_pdf = "pdf" in (adjunto.get("mime") or "") or adjunto.get("nombre", "").lower().endswith(".pdf")
        if es_pdf: return procesar_orden_pdf(eid, c, adjunto, modulos)
        try:
            enviar_wa(eid, tel, "Recibimos su archivo. Para procesar un pedido automáticamente envíe la orden de compra en *PDF* "
                                "o escriba los productos (código y cantidad). Escriba *MENÚ* para ver las opciones.", c)
        except WA.ErrorWhatsapp: pass
        return []
    import re as _re
    m_rif = _re.fullmatch(r"\s*(?:rif[:\s]*)?([VEJPGCvejpgc])\s*-?\s*(\d{8})\s*-?\s*(\d)\s*", texto or "")
    if m_rif and c.tipo != "bloqueado":       # el cliente responde con su RIF (p. ej. para su factura)
        c.rif = f"{m_rif.group(1).upper()}-{m_rif.group(2)}-{m_rif.group(3)}"
        try: enviar_wa(eid, tel, f"Gracias. Registramos su RIF {c.rif} para sus facturas.", c)
        except WA.ErrorWhatsapp: pass
        return []
    peds = [(p.numero, p.estado, p.total) for p in db.session.execute(select(Pedido).where(Pedido.empresa_id == eid, Pedido.contacto_id == c.id)
                                                                      .order_by(Pedido.id.desc()).limit(10)).scalars()]
    ocs = [(o.numero, o.estado, o.total, o.moneda) for o in db.session.execute(select(OrdenCompra).where(OrdenCompra.empresa_id == eid, OrdenCompra.dest_whatsapp == tel)
                                                                                .order_by(OrdenCompra.id.desc()).limit(30)).scalars()] if c.tipo == "proveedor" else []
    respuestas, estado, eventos = BOT.responder(texto, c.tipo, c.estado or {}, catalogo_bot(eid), e.razon_social, peds, ocs, cfg.bienvenida if cfg else None)
    c.estado = estado
    for ev in eventos:
        if "tipo_contacto" in ev: c.tipo = ev["tipo_contacto"]
        if "crear_pedido" in ev:
            d = ev["crear_pedido"]
            total = Decimal(str(d.get("total") or sum((Decimal(l["cantidad"]) * Decimal(l["precio"]) for l in d["lineas"]), Decimal(0)))).quantize(C2)
            p = Pedido(empresa_id=eid, numero=_siguiente(eid, Pedido), tipo=d["tipo"], contacto_id=c.id, telefono=tel, nombre=c.nombre, lineas=d["lineas"],
                       total=total, nota=d.get("nota") or None)
            db.session.add(p); db.session.flush()
            respuestas = [r + f"\nN° de {'pedido' if p.tipo == 'venta' else 'oferta'}: *{p.numero}*" if i == 0 else r for i, r in enumerate(respuestas)]
            registrar("whatsapp", "PEDIDO_WHATSAPP", f"empresa={eid} {p.tipo} N°{p.numero} tel=…{tel[-4:]} total={total}")
        if "oc_confirmada" in ev or "oc_rechazada" in ev:
            num = ev.get("oc_confirmada") or ev.get("oc_rechazada")
            oc = db.session.execute(select(OrdenCompra).where(OrdenCompra.empresa_id == eid, OrdenCompra.numero == num, OrdenCompra.dest_whatsapp == tel)
                                    .with_for_update()).scalar_one_or_none()
            if oc is not None:
                cambiar_estado_oc(eid, f"whatsapp:{tel[-4:]}", oc, "Confirmada" if "oc_confirmada" in ev else "Rechazada", ev.get("motivo"))
    enviadas = []
    for r in respuestas:
        try:
            enviar_wa(eid, tel, r, c); enviadas.append(r)
        except WA.ErrorWhatsapp as err:
            current_app.logger.warning("WhatsApp no respondió a …%s: %s", tel[-4:], err)
    return enviadas


def notificar_pedido(eid, p: Pedido, texto):
    if p.telefono:
        try:
            enviar_wa(eid, p.telefono, texto)
        except WA.ErrorWhatsapp as e:
            return str(e)
    return None



# ====================================================================== orden de compra en PDF → factura / orden de venta
def _enlace(salt, datos, dias=60):
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=salt).dumps(datos)


def enlace_pedido(p: Pedido) -> str:
    org = db.session.get(Empresa, p.empresa_id).organizacion_id
    return current_app.config["APP_BASE_URL"].rstrip("/") + "/ov/" + _enlace("orden-venta", [org, p.empresa_id, p.id])


def leer_enlace_pedido(token):
    from itsdangerous import BadSignature, URLSafeTimedSerializer
    try:
        return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="orden-venta").loads(token, max_age=60 * 86400)
    except BadSignature:
        return None


def pedido_dict(p: Pedido) -> dict:
    """Orden de venta con el mismo modelo de documento (emisor = la empresa, destinatario = el cliente), IVA por producto."""
    from .servicios_fact import alicuotas_vigentes
    e = db.session.get(Empresa, p.empresa_id)
    try: al = alicuotas_vigentes(p.creado_en.date())
    except ValueError: al = {"G": 16, "R": 8, "A": 15}
    exento = base = iva = Decimal(0); lineas = []
    for l in p.lineas:
        prod = db.session.get(Producto, (p.empresa_id, l.get("codigo"))) if l.get("codigo") else None
        a = prod.alicuota_iva if prod else "G"
        monto = (Decimal(str(l.get("cantidad") or 0)) * Decimal(str(l.get("precio") or 0))).quantize(C2)
        pct = Decimal(0) if a == "E" else Decimal(str(al["G"])) + (Decimal(str(al["A"])) if a == "A" else 0) if a in ("G", "A") else Decimal(str(al["R"]))
        if a == "E": exento += monto
        else: base += monto; iva += (monto * pct / 100).quantize(C2)
        lineas.append(dict(codigo=l.get("codigo"), descripcion=l.get("descripcion") or "", unidad=None, cantidad=str(l.get("cantidad")), precio=str(l.get("precio") or 0), alicuota=a, monto=str(monto)))
    total = exento + base + iva
    nota = " ".join(x for x in (f"Su orden de compra: {p.referencia_cliente}." if p.referencia_cliente else "", p.nota or "") if x)
    return {"numero": p.numero, "numero_txt": f"OV-{p.numero:06d}", "fecha": str(p.creado_en.date()), "fecha_entrega": None, "estado": p.estado,
            "tipo_destinatario": "cliente", "moneda": "VES", "tasa_bcv": "1", "condicion_pago": "Pago en oficina / transferencia", "lugar_entrega": e.direccion,
            "observaciones": nota or None, "creado_por": "Bot de WhatsApp", "exento": str(exento), "base": str(base), "iva": str(iva), "total": str(total), "total_bs": str(total),
            "emisor": {"razon_social": e.razon_social, "rif": e.rif, "direccion": e.direccion, "telefono": e.telefono, "email": e.correo,
                       "contribuyente_especial": e.es_sujeto_pasivo_especial, "logo": (f"data:{e.logo_mime};base64,{__import__('base64').b64encode(e.logo).decode()}" if e.logo else None)},
            "destinatario": {"nombre": p.nombre or ("+" + (p.telefono or "")), "rif": p.rif or "—", "direccion": None, "telefono": ("+" + p.telefono) if p.telefono else None, "email": None},
            "lineas": lineas}


def procesar_orden_pdf(eid, c: WhatsappContacto, adjunto, modulos) -> list[str]:
    """PDF de orden de compra → lee lo pedido → lo ajusta a la existencia → emite la FACTURA (si el bot tiene facturación
    automática, serie y el RIF del cliente) o una ORDEN DE VENTA, y le responde con el detalle, el total, el enlace al
    documento y cómo pagar o retirar en la oficina. La factura usa el mismo servicio que la pantalla de facturación: descarga
    inventario, genera CxC, asiento y entra al libro de ventas."""
    from . import lector_pedidos as LP
    from .models import Usuario
    from .models_fact import ClienteFact
    cfg, e = config_wa(eid), db.session.get(Empresa, eid)
    cif = current_app.extensions["cifrador"]
    descarga = current_app.extensions.get("whatsapp_descarga")
    responder = lambda t: enviar_wa(eid, c.telefono, t, c)
    try:
        if descarga: datos, _ = descarga(adjunto["id"])
        elif cfg.proveedor == "wasender":
            from . import wasender as WS
            datos, _ = WS.descargar_media(cfg.api_url, cif.descifrar(cfg.token_cifrado), adjunto["raw"], transporte=current_app.extensions.get("wasender_transporte"))
        else: datos, _ = WA.descargar_media(adjunto["id"], cif.descifrar(cfg.token_cifrado))
        leido = LP.interpretar(LP.texto_pdf(datos), catalogo_bot(eid) | {p.codigo: (p.nombre, p.precio_venta, p.cantidad_actual) for p in
                                                                        db.session.execute(select(Producto).where(Producto.empresa_id == eid)).scalars()})
    except (LP.ErrorLectura, WA.ErrorWhatsapp) as err:
        responder(f"No pudimos leer su archivo: {err}."); return []
    if not leido["lineas"]:
        responder("Recibimos su orden de compra, pero no identificamos productos de nuestro catálogo. Un asesor la revisará y le escribirá. "
                  "Si prefiere, escriba *1* para ver el catálogo con los códigos.")
        db.session.add(Pedido(empresa_id=eid, numero=_siguiente(eid, Pedido), tipo="venta", origen="whatsapp_pdf", contacto_id=c.id, telefono=c.telefono,
                              nombre=c.nombre, lineas=[], total=0, nota=("No identificado: " + "; ".join(leido["no_identificadas"]))[:2000] or "PDF sin productos reconocidos",
                              referencia_cliente=leido["referencia"], rif=leido["rif"]))
        return []
    cat = {p.codigo: (p.nombre, p.precio_venta, p.cantidad_actual) for p in db.session.execute(select(Producto).where(Producto.empresa_id == eid)).scalars()}
    lineas, faltan = LP.despachable(leido["lineas"], cat)
    rif = leido["rif"] or c.rif
    cli = db.session.execute(select(ClienteFact).where(ClienteFact.empresa_id == eid, ClienteFact.rif == rif)).scalar_one_or_none() if rif else None
    if rif and not c.rif: c.rif = rif
    nombre = (cli.razon_social if cli else None) or c.nombre or ("+" + c.telefono)
    total = sum((Decimal(l["cantidad"]) * Decimal(l["precio"]) for l in lineas), Decimal(0)).quantize(C2)
    nota = "; ".join(([f"Sin existencia suficiente: {', '.join(faltan)}"] if faltan else []) + ([f"No identificado: {'; '.join(leido['no_identificadas'][:8])}"] if leido["no_identificadas"] else []))
    p = Pedido(empresa_id=eid, numero=_siguiente(eid, Pedido), tipo="venta", origen="whatsapp_pdf", contacto_id=c.id, telefono=c.telefono, nombre=nombre,
               lineas=lineas, total=total, nota=nota[:2000] or None, referencia_cliente=leido["referencia"], rif=rif)
    db.session.add(p); db.session.flush()
    registrar("whatsapp", "ORDEN_PDF_RECIBIDA", f"empresa={eid} pedido N°{p.numero} lineas={len(lineas)} faltantes={len(faltan)} ref={leido['referencia'] or ''}")
    factura = None
    if lineas and cfg.factura_automatica and cfg.serie_id and rif and cfg.usuario_id and "facturacion" in modulos:
        from datetime import timedelta
        from .servicios_fact import emitir_documento, numero_completo
        db.session.commit()                       # lo recibido queda guardado aunque la factura no se pueda emitir
        u, pid = db.session.get(Usuario, cfg.usuario_id), p.id
        hoy = hoy_ve()
        try:
            d = emitir_documento(eid, u, dict(tipo="FAC", serie_id=cfg.serie_id, fecha=hoy.isoformat(), moneda="VES", condicion="Credito",
                                              vencimiento=(hoy + timedelta(days=cfg.dias_credito or 0)).isoformat(), pagos=[],
                                              lineas=[dict(producto=l["codigo"] if "inventario" in modulos else "", descripcion=l["descripcion"], cantidad=l["cantidad"],
                                                           precio=l["precio"], descuento_pct="0", alicuota=db.session.get(Producto, (eid, l["codigo"])).alicuota_iva)
                                                      for l in lineas],
                                              cliente=dict(rif=rif, nombre=nombre, direccion=cli.direccion if cli else None, telefono=cli.telefono if cli else ("+" + c.telefono)),
                                              observaciones=f"Pedido por WhatsApp N° {p.numero}" + (f" · OC del cliente {leido['referencia']}" if leido["referencia"] else "")), modulos)
            p = db.session.get(Pedido, pid)
            p.estado, p.documento_venta_id = "Facturado", d.id
            factura = (numero_completo(d), d.numero_control, d.total_a_pagar)
        except Exception as err:  # noqa: BLE001 — sin rango de control, alícuotas, período cerrado…: queda como orden de venta
            db.session.rollback()
            p = db.session.get(Pedido, pid); c = db.session.get(WhatsappContacto, c.id)
            p.nota = ((p.nota + " · ") if p.nota else "") + f"Factura automática no emitida: {str(err)[:200]}"
            current_app.logger.warning("Bot: factura automática no emitida (empresa %s): %s", eid, err)
    detalle = "\n".join(f"• {l['codigo']} {l['descripcion']} × {l['cantidad']}" + (f" (pidió {l['solicitado']})" if l["solicitado"] != l["cantidad"] else "") for l in lineas)
    partes = [f"✅ Recibimos su orden de compra{(' N° ' + leido['referencia']) if leido['referencia'] else ''}."]
    if lineas:
        partes.append(("🧾 *Factura N° " + factura[0] + f"* (control {factura[1]})" if factura else f"📄 *Orden de venta N° {p.numero}*") + f"\n{detalle}")
        partes.append(f"*Total: Bs {pc_bs(factura[2] if factura else Decimal(pedido_dict(p)['total']))}*" + ("" if factura else " (IVA incluido)"))
    if faltan: partes.append("⚠️ No tenemos todo lo solicitado: " + "; ".join(faltan) + ".")
    if leido["no_identificadas"]: partes.append("❓ No identificamos: " + "; ".join(leido["no_identificadas"][:5]) + ". Un asesor le confirmará.")
    if lineas:
        partes.append(f"Ver documento: {enlace_pedido(p)}")
        partes.append(cfg.instrucciones_pago or f"Puede pagar y retirar en nuestra oficina: {e.direccion or e.razon_social}" + (f" · Tel. {e.telefono}" if e.telefono else "") + ".")
        if not rif: partes.append("Para emitir su factura fiscal responda con su *RIF* (J-12345678-9).")
    try:
        responder("\n\n".join(partes))
    except WA.ErrorWhatsapp as err:
        current_app.logger.warning("Bot: no se pudo responder la orden PDF: %s", err)
    return partes


def pc_bs(v) -> str:
    from .plantilla_correo import _bs
    return _bs(v)
