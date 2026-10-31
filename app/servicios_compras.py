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
    """Consecutivo por empresa sin saltos ni duplicados: se bloquea la fila de la empresa mientras se numera."""
    db.session.execute(select(Empresa.id).where(Empresa.id == eid).with_for_update())
    return (db.session.execute(select(func.max(Modelo.numero)).where(Modelo.empresa_id == eid)).scalar() or 0) + 1


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


def recibir_orden(eid, u, oc: OrdenCompra, fecha, credito=True) -> int:
    """Marca la orden como recibida y da entrada al inventario de cada línea con código de producto (costo en Bs)."""
    from .servicios import entrada_inventario
    cambiar_estado_oc(eid, u.username, oc, "Recibida")
    n = 0
    for l in oc.lineas:
        if l.codigo and db.session.get(Producto, (eid, l.codigo)):
            entrada_inventario(eid, u, l.codigo, l.cantidad, (l.precio * oc.tasa_bcv).quantize(Decimal("0.0001")), fecha,
                               f"{numero_oc(oc.numero)} {oc.dest_nombre}"[:120], credito=credito, commit=False)
            n += 1
    return n


def oc_dict(oc: OrdenCompra) -> dict:
    e = db.session.get(Empresa, oc.empresa_id)
    return {"numero": oc.numero, "numero_txt": numero_oc(oc.numero), "fecha": str(oc.fecha), "fecha_entrega": str(oc.fecha_entrega) if oc.fecha_entrega else None,
            "estado": oc.estado, "tipo_destinatario": oc.tipo_destinatario, "moneda": oc.moneda, "tasa_bcv": str(oc.tasa_bcv),
            "condicion_pago": oc.condicion_pago, "lugar_entrega": oc.lugar_entrega, "observaciones": oc.observaciones, "creado_por": oc.creado_por,
            "exento": str(oc.exento), "base": str(oc.base), "iva": str(oc.iva), "total": str(oc.total), "total_bs": str(oc.total_bs),
            "emisor": {"razon_social": e.razon_social, "rif": e.rif, "direccion": e.direccion, "telefono": e.telefono, "contribuyente_especial": e.es_sujeto_pasivo_especial},
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
    transporte = current_app.extensions.get("whatsapp_transporte") or WA._http
    try:
        wamid = WA.enviar_texto(cfg.phone_number_id, cif.descifrar(cfg.token_cifrado), tel, texto, transporte=transporte); res = "ok"
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


def procesar_mensaje(eid, telefono, nombre, texto, wamid) -> list[str]:
    """Un mensaje entrante: registra, conversa (whatsapp_bot), aplica los eventos y responde. Idempotente por wamid."""
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

