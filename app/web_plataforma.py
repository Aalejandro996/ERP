"""Consola de PLATAFORMA (Maestro del proveedor): alta de clientes (organizaciones) con su primer administrador,
suspensión, estado de licencias y parámetros legales GLOBALES (UT, tarifa ISLR, retenciones, INPC, tasa BCV, alícuotas).
El Maestro no tiene acceso a la contabilidad ni a la facturación de ningún cliente (RLS lo impide en la BD)."""
import secrets

from flask import current_app, flash, g, redirect, request, url_for
from sqlalchemy import func, select

from .auditoria import registrar, verificar_cadena
from .auth.routes import enviar_invitacion
from .contabilidad.validadores import a_decimal, parsear_fecha
from .extensions import db
from .models import AuditLog, Organizacion, Usuario
from .models2 import AlicuotaImpuesto, ConceptoRetencionISLR, IndiceINPC, TarifaISLR, TasaCambio, UnidadTributaria
from .planes import nombre_plan
from .security import hashear
from .web import F, campo, hoy, intentar, pagina, ruta, validar_rif

h = lambda v: campo("accion", "", "hidden", valor=v)


@ruta("/plataforma", "plataforma", ("GET", "POST"))
def plataforma():
    if request.method == "POST":
        a = F["accion"]
        def crear():
            rif = validar_rif(F["rif"])
            if db.session.execute(select(Organizacion.id).where(Organizacion.rif == rif)).first(): raise ValueError("Ya existe un cliente con ese RIF")
            email, nombre = F["email"].strip().lower(), F["username"].strip()
            if "@" not in email: raise ValueError("Correo inválido")
            if db.session.execute(select(Usuario.id).where((Usuario.username == nombre) | (Usuario.email == email))).first():
                raise ValueError("El usuario o el correo del administrador no están disponibles")
            org = Organizacion(nombre=F["nombre"].strip(), rif=rif, correo_contacto=email)
            db.session.add(org); db.session.flush()
            u = Usuario(organizacion_id=org.id, username=nombre, email=email, rol="Administrador", password_hash=hashear(secrets.token_urlsafe(32)), debe_cambiar_clave=False)
            db.session.add(u); db.session.flush()
            enviar_invitacion(u, org.nombre, "El proveedor de la plataforma")
            registrar(g.usuario.username, "ORGANIZACION_CREADA", f"{rif} {org.nombre} admin={nombre}", org=None)
            registrar(g.usuario.username, "ORGANIZACION_CREADA", f"admin={nombre}", org=org.id)  # génesis de la cadena del cliente
            db.session.commit()
        def estado():
            org = db.session.get(Organizacion, int(F["oid"]))
            if org is None: raise ValueError("Organización inexistente")
            org.estado = "suspendida" if org.estado == "activa" else "activa"
            registrar(g.usuario.username, "ORGANIZACION_" + org.estado.upper(), org.rif, org=None); db.session.commit()
        if a not in ("crear", "estado"): flash("Acción inválida"); return redirect(request.url)
        intentar({"crear": crear, "estado": estado}[a], "Hecho"); return redirect(request.url)
    gestor = current_app.extensions["gestor_licencia"]
    orgs = db.session.execute(select(Organizacion).order_by(Organizacion.nombre)).scalars().all()
    n_us = dict(db.session.execute(select(Usuario.organizacion_id, func.count()).where(Usuario.activo.is_(True)).group_by(Usuario.organizacion_id)).all())
    filas = []
    for o in orgs:
        est = gestor(o.id).estado()
        filas.append(dict(cells=[o.nombre, o.rif, o.estado.capitalize(), nombre_plan(est.claims) if est.valida else f"Sin licencia ({est.motivo})", n_us.get(o.id, 0), o.correo_contacto or ""]))
    return pagina("Clientes de la plataforma", None, [
        dict(kpis=[("Clientes", len(orgs)), ("Activos", sum(o.estado == "activa" for o in orgs)), ("Usuarios activos", sum(v for k, v in n_us.items() if k))]),
        dict(tabla=dict(cols=["Organización", "RIF", "Estado", "Plan / licencia", "Usuarios", "Contacto"], filas=filas)),
        dict(titulo="Nuevo cliente", nota="Se crea la organización y se envía una invitación a su administrador. El plan lo define la clave de licencia que usted emite (licensing_server.cli crear --plan).",
             form=dict(action=request.path, boton="Crear e invitar", campos=[h("crear"), campo("nombre", "Nombre del cliente"), campo("rif", "RIF"),
                                                                              campo("username", "Usuario administrador"), campo("email", "Correo del administrador", "email")])),
        dict(titulo="Suspender / reactivar cliente", nota="Suspender cierra de inmediato las sesiones de todos sus usuarios.",
             form=dict(action=request.path, boton="Cambiar estado", campos=[h("estado"), campo("oid", "Organización", "select", opciones=[(o.id, f"{o.nombre} ({o.estado})") for o in orgs])]))])


@ruta("/plataforma/fiscal", "plataforma", ("GET", "POST"))
def admin_fiscal():
    if request.method == "POST":
        a = F["accion"]
        def guardar():
            if a == "ut": db.session.add(UnidadTributaria(valor_bs=a_decimal(F["valor"]), fecha_vigencia=parsear_fecha(F["fecha"]), gaceta_referencia=F.get("gaceta")))
            elif a == "tarifa": db.session.add(TarifaISLR(desde_ut=a_decimal(F["desde"]), hasta_ut=a_decimal(F["hasta"]), porcentaje=a_decimal(F["pct"]), sustraendo_ut=a_decimal(F.get("sust") or 0)))
            elif a == "concepto": db.session.add(ConceptoRetencionISLR(concepto=F["concepto"], codigo_seniat=F.get("codigo"), porcentaje_juridica=a_decimal(F["pj"]), porcentaje_natural=a_decimal(F["pn"]),
                                                                        sustraendo_ut=a_decimal(F.get("sust") or 0), monto_minimo_ut=a_decimal(F.get("min") or 0)))
            elif a == "inpc":
                v = _decimal_positivo(F["valor"], "Índice INPC")
                f = parsear_fecha(F["fecha"])
                if db.session.execute(select(IndiceINPC.id).where(IndiceINPC.fecha == f)).first(): raise ValueError(f"Ya hay un INPC registrado para {f}")
                db.session.add(IndiceINPC(valor_indice=v, fecha=f, referencia=F.get("ref")))
            elif a == "tasa":
                usd, eur = _decimal_positivo(F["valor"], "Tasa USD"), (_decimal_positivo(F["eur"], "Tasa EUR") if F.get("eur") else None)
                f = parsear_fecha(F["fecha"])
                fila = db.session.execute(select(TasaCambio).where(TasaCambio.fecha == f)).scalar_one_or_none()
                if fila: fila.tasa_usd_ves, fila.tasa_eur_ves, fila.fuente = usd, eur or fila.tasa_eur_ves, "Manual (Maestro)"
                else: db.session.add(TasaCambio(fecha=f, tasa_usd_ves=usd, tasa_eur_ves=eur, fuente="Manual (Maestro)"))
            elif a == "tasa_bcv":
                from . import tasas
                t = tasas.consultar()
                flash(f"BCV vía DolarAPI · vigencia {t['fecha']:%d/%m/%Y}: USD {t['USD']} · EUR {t['EUR']} — {tasas.guardar(t, forzar=F.get('forzar') == '1')}")
            elif a == "alicuota":
                if F["codigo"] not in ("G", "R", "A", "IGTF"): raise ValueError("Código inválido")
                db.session.add(AlicuotaImpuesto(codigo=F["codigo"], porcentaje=a_decimal(F["valor"]), fecha_vigencia=parsear_fecha(F["fecha"]), gaceta_referencia=F.get("gaceta")))
            else: raise ValueError("Acción inválida")
            registrar(g.usuario.username, f"PARAM_FISCAL_{a.upper()}", (F.get("codigo", "") + " " + F.get("valor", ""))[:40], org=None); db.session.commit()
        intentar(guardar, "Parámetro guardado"); return redirect(request.url)
    q = lambda m, *o, n=None: db.session.execute(select(m).order_by(*o).limit(n) if n else select(m).order_by(*o)).scalars()
    f = lambda act, titulo, *cs: dict(titulo=titulo, form=dict(action=request.path, boton="Agregar", campos=[h(act), *cs]))
    return pagina("Parámetros legales (globales)", None, [
        dict(nota="Valores legales comunes a todos los clientes. Cárguelos según la Gaceta Oficial vigente: el sistema no trae valores legales fijos en el código.",
             titulo="Alícuotas (IVA e IGTF)", tabla=dict(cols=["Código", "%", "Vigencia", "Gaceta"], num=[1], filas=[dict(cells=[x.codigo, x.porcentaje, x.fecha_vigencia, x.gaceta_referencia]) for x in q(AlicuotaImpuesto, AlicuotaImpuesto.fecha_vigencia.desc(), n=20)])),
        f("alicuota", "Nueva alícuota", campo("codigo", "Tipo", "select", opciones=[("G", "IVA general"), ("R", "IVA reducida"), ("A", "IVA adicional bienes de lujo"), ("IGTF", "IGTF (pagos en divisas)")]),
          campo("valor", "%", "number"), campo("fecha", "Vigencia", "date", valor=hoy()), campo("gaceta", "Gaceta / norma", req=False)),
        dict(titulo="Tasa oficial BCV (Bs por divisa)", nota="Se actualiza sola cada día desde DolarAPI (fuente: BCV). Una tasa cargada a mano tiene prioridad sobre la automática.",
             tabla=dict(cols=["Vigencia", "USD", "EUR", "Fuente"], num=[1, 2], filas=[dict(cells=[x.fecha, x.tasa_usd_ves, x.tasa_eur_ves or "—", x.fuente or "Manual"]) for x in q(TasaCambio, TasaCambio.fecha.desc(), n=10)])),
        dict(titulo="Actualizar ahora desde el BCV", form=dict(action=request.path, boton="Consultar DolarAPI", campos=[h("tasa_bcv"),
             campo("forzar", "¿Reemplazar una tasa cargada a mano para esa fecha?", "select", opciones=[("0", "No"), ("1", "Sí")])])),
        f("tasa", "Cargar tasa a mano", campo("valor", "USD (Bs)", "number"), campo("eur", "EUR (Bs)", "number", req=False), campo("fecha", "Vigencia", "date", valor=hoy())),
        dict(titulo="Unidad Tributaria", tabla=dict(cols=["Valor Bs", "Vigencia", "Gaceta"], num=[0], filas=[dict(cells=[x.valor_bs, x.fecha_vigencia, x.gaceta_referencia]) for x in q(UnidadTributaria, UnidadTributaria.fecha_vigencia.desc(), n=5)])),
        f("ut", "Nueva Unidad Tributaria", campo("valor", "Valor Bs", "number"), campo("fecha", "Vigencia", "date", valor=hoy()), campo("gaceta", "Gaceta", req=False)),
        dict(titulo="Tarifa N° 2 ISLR (jurídicas)", tabla=dict(cols=["Desde UT", "Hasta UT", "%", "Sustraendo UT"], num=[0, 1, 2, 3], filas=[dict(cells=[x.desde_ut, x.hasta_ut, x.porcentaje, x.sustraendo_ut]) for x in q(TarifaISLR, TarifaISLR.desde_ut)])),
        f("tarifa", "Nuevo tramo de tarifa", campo("desde", "Desde UT", "number"), campo("hasta", "Hasta UT", "number"), campo("pct", "%", "number"), campo("sust", "Sustraendo UT", "number", req=False)),
        dict(titulo="Conceptos de retención ISLR (Decreto 1.808)", tabla=dict(cols=["Concepto", "Código", "% jurídica", "% natural", "Sustraendo UT", "Mínimo UT"], num=[2, 3, 4, 5], filas=[dict(cells=[x.concepto, x.codigo_seniat, x.porcentaje_juridica, x.porcentaje_natural, x.sustraendo_ut, x.monto_minimo_ut]) for x in q(ConceptoRetencionISLR, ConceptoRetencionISLR.id)])),
        f("concepto", "Nuevo concepto de retención", campo("concepto", "Concepto"), campo("codigo", "Código SENIAT", req=False), campo("pj", "% jurídica", "number"), campo("pn", "% natural", "number"), campo("sust", "Sustraendo UT", "number", req=False), campo("min", "Mínimo UT", "number", req=False)),
        dict(titulo="Índices INPC (BCV)", tabla=dict(cols=["Fecha", "Índice", "Referencia"], num=[1], filas=[dict(cells=[x.fecha, format(x.valor_indice.normalize(), "f"), x.referencia]) for x in q(IndiceINPC, IndiceINPC.fecha.desc(), n=24)])),
        f("inpc", "Nuevo índice INPC", campo("valor", "Índice (base dic-2007 = 100)", "number"), campo("fecha", "Mes (use el día 1 o el último día)", "date"), campo("ref", "Referencia", req=False))])


def _decimal_positivo(valor, nombre):
    """Acepta 1234567,89 o 1.234.567,89 (formato venezolano) además de 1234567.89."""
    from decimal import Decimal, InvalidOperation
    t = str(valor or "").strip().replace(" ", "")
    if "," in t: t = t.replace(".", "").replace(",", ".")
    try:
        d = Decimal(t)
    except InvalidOperation as e:
        raise ValueError(f"{nombre} inválido: {valor!r}") from e
    if not d.is_finite() or d <= 0 or d >= Decimal("1e27"): raise ValueError(f"{nombre} fuera de rango")
    return d


@ruta("/plataforma/bitacora", "plataforma")
def plataforma_bitacora():
    ok, rota = verificar_cadena(None)
    logs = db.session.execute(select(AuditLog).where(AuditLog.organizacion_id.is_(None)).order_by(AuditLog.id.desc()).limit(300)).scalars()
    return pagina("Bitácora de plataforma", None, [dict(titulo=f"Cadena {'ÍNTEGRA' if ok else f'ROTA en el registro {rota}'}", tabla=dict(
        cols=["Fecha (UTC)", "Usuario", "IP", "Acción", "Detalle"], filas=[dict(cells=[l.ts.strftime("%Y-%m-%d %H:%M:%S"), l.usuario, l.ip or "", l.accion, (l.detalles or "")[:120]]) for l in logs]))])


# ====================================================================== consola de licencias del Maestro
def _admin():
    f = current_app.extensions.get("admin_licencias")
    if f is None: raise ValueError("Configure LICENSE_ADMIN_SECRET y LICENSE_ADMIN_URL para gestionar licencias desde aquí")
    return f()


def _fecha(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%d") if ts else "—"


@ruta("/plataforma/licencias", "plataforma", ("GET", "POST"))
def maestro_licencias():
    from .licensing.admin import ErrorAdmin
    from .planes import PLANES
    orgs = db.session.execute(select(Organizacion).order_by(Organizacion.nombre)).scalars().all()
    por_id = {str(o.id): o for o in orgs}
    if request.method == "POST":
        a, autor = F["accion"], g.usuario.username
        def hacer():
            adm = _admin()
            if a == "crear":
                org = por_id.get(F["org"])
                if org is None: raise ValueError("Organización inexistente")
                r = adm.crear_licencia(cliente=org.nombre, correo=(F.get("correo") or org.correo_contacto or "").strip().lower(), organizacion_ref=str(org.id),
                                       plan=F["plan"], dias=int(F["dias"]), max_usuarios=int(F["usuarios"]), max_empresas=int(F["empresas"]),
                                       max_equipos=int(F["equipos"]), max_instancias=int(F.get("instancias") or 1), autor=autor)
                detalle = f"licencia={r['id']} org={org.rif} plan={F['plan']}"
                if F.get("precio"):  # licencia vendida fuera de la tienda: factura de Gigadevs en un segundo correo
                    from .tasas import tasa_vigente
                    from .tiempo import hoy_ve
                    try:
                        adm.crear_factura(cliente_nombre=org.nombre, cliente_rif=org.rif, cliente_correo=(F.get("correo") or org.correo_contacto or "").strip().lower(),
                                          moneda="USD", tasa_bcv=str(tasa_vigente(hoy_ve(), "USD") or ""), monto_con_iva=F["precio"], licencia_id=r["id"],
                                          concepto=f"Licencia de uso ERP — plan {F['plan']}, {F['dias']} días", forma_pago=F.get("forma_pago") or None, autor=autor)
                        detalle += " facturada"
                    except (ErrorAdmin, ValueError) as e:
                        flash(f"La licencia se emitió, pero la factura no: {e}. Puede emitirla en Facturación Gigadevs.")
            elif a == "modificar":
                cambios = {k: F.get(k) for k in ("plan", "estado", "max_usuarios", "max_empresas", "max_equipos", "max_instancias", "dias_extra") if F.get(k)}
                adm.modificar(int(F["lid"]), autor=autor, **cambios); detalle = f"licencia={F['lid']} {sorted(cambios)}"
            elif a == "liberar":
                adm.liberar(int(F["lid"]), autor); detalle = f"licencia={F['lid']}"
            elif a == "reenviar":
                adm.reenviar(int(F["lid"]), (F.get("correo") or "").strip().lower() or None); detalle = f"licencia={F['lid']}"
            else: raise ValueError("Acción inválida")
            registrar(autor, "LICENCIA_" + a.upper(), detalle, org=None); db.session.commit()
        try:
            hacer(); flash({"crear": "Licencia emitida: la clave se envió por correo (una sola vez).", "reenviar": "Se generó y envió una clave nueva; la anterior quedó anulada."}.get(a, "Hecho"))
        except (ErrorAdmin, ValueError, OSError) as e:
            db.session.rollback(); flash(str(e))
        return redirect(request.url)
    try:
        lics, err = _admin().licencias(), None
    except (ErrorAdmin, ValueError, OSError) as e:
        lics, err = [], str(e)
    if err: flash(err)
    nombre_org = lambda ref: por_id[ref].nombre if ref in por_id else (ref or "—")
    secs = [dict(kpis=[("Licencias", len(lics)), ("Activas", sum(l["estado"] == "activa" for l in lics)),
                       ("Equipos permitidos (total)", sum(l["max_equipos"] or 0 for l in lics)), ("Por pago en línea", sum(l["origen"] == "pago" for l in lics))]),
            dict(titulo="Licencias emitidas", tabla=dict(cols=["#", "Organización", "Plan", "Estado", "Usuarios", "Empresas", "Equipos", "Instalaciones", "Vence", "Origen", "Clave enviada"],
                                                        num=[4, 5, 6], filas=[dict(cells=[l["id"], nombre_org(l["organizacion_ref"]), PLANES.get(l["plan"], {}).get("nombre", "Legado"), l["estado"],
                                                                                          l["max_usuarios"], l["max_empresas"], l["max_equipos"] or "∞", f"{l['en_uso']}/{l['max_instancias']}",
                                                                                          _fecha(l["vence_en"]), l["origen"], _fecha(l["clave_enviada_en"])],
                                                                                    url=url_for("web.maestro_licencias", eventos=l["id"])) for l in lics]))]
    if request.args.get("eventos", "").isdigit():
        try: evs = _admin().eventos(int(request.args["eventos"]))
        except (ErrorAdmin, ValueError, OSError): evs = []
        secs.append(dict(titulo=f"Eventos de la licencia #{request.args['eventos']}", tabla=dict(cols=["Fecha (UTC)", "Evento", "IP", "Detalle"], filas=[
            dict(cells=[_fecha(e["ts"]), e["evento"], e["ip"] or "", (e["detalle"] or "")[:100]]) for e in evs])))
    planes = [(k, v["nombre"]) for k, v in PLANES.items()]
    ids = [(l["id"], f"#{l['id']} · {nombre_org(l['organizacion_ref'])}") for l in lics]
    secs += [
        dict(titulo="Emitir licencia", nota="La clave se genera en el servidor de licencias y se envía UNA vez por correo (Brevo, remitente Gigadevs Corp). Nadie más la ve: ni usted.",
             form=dict(action=request.path, boton="Emitir y enviar", campos=[h("crear"), campo("org", "Organización", "select", opciones=[(o.id, f"{o.nombre} ({o.rif})") for o in orgs]),
                       campo("correo", "Enviar a (vacío = contacto del cliente)", "email", req=False), campo("plan", "Plan", "select", opciones=planes),
                       campo("dias", "Vigencia (días)", "number", valor=365), campo("usuarios", "Usuarios activos", "number", valor=5),
                       campo("empresas", "Empresas", "number", valor=2), campo("equipos", "Equipos permitidos", "number", valor=5),
                       campo("instancias", "Instalaciones del servidor", "number", valor=1),
                       campo("precio", "Precio cobrado USD, IVA incluido (vacío = sin factura)", "number", req=False),
                       campo("forma_pago", "Forma de pago", req=False)])),
        dict(titulo="Modificar licencia", nota="Plan, topes y estado llegan al cliente en su próxima renovación (≤ 7 días) o cuando su administrador pulse «Renovar».",
             form=dict(action=request.path, boton="Guardar cambios", campos=[h("modificar"), campo("lid", "Licencia", "select", opciones=ids),
                       campo("plan", "Plan (sin cambio)", "select", req=False, opciones=[("", "—")] + planes),
                       campo("estado", "Estado (sin cambio)", "select", req=False, opciones=[("", "—"), ("activa", "Activa"), ("suspendida", "Suspendida"), ("revocada", "Revocada")]),
                       campo("max_usuarios", "Usuarios", "number", req=False), campo("max_empresas", "Empresas", "number", req=False),
                       campo("max_equipos", "Equipos", "number", req=False), campo("max_instancias", "Instalaciones", "number", req=False),
                       campo("dias_extra", "Extender (días)", "number", req=False)])),
        dict(titulo="Liberar instalaciones", nota="Permite activar la licencia en otro servidor (migración legítima).",
             form=dict(action=request.path, boton="Liberar", campos=[h("liberar"), campo("lid", "Licencia", "select", opciones=ids)])),
        dict(titulo="Regenerar y reenviar clave", nota="Para claves perdidas: se crea una clave NUEVA, se envía por correo y la anterior deja de funcionar.",
             form=dict(action=request.path, boton="Regenerar y enviar", campos=[h("reenviar"), campo("lid", "Licencia", "select", opciones=ids),
                       campo("correo", "Enviar a (vacío = correo registrado)", "email", req=False)]))]
    return pagina("Licencias", None, secs)


@ruta("/plataforma/pagos", "plataforma", ("GET", "POST"))
def maestro_pagos():
    from .licensing.admin import ErrorAdmin
    from .planes import PLANES
    if request.method == "POST":
        a = F["accion"]
        try:
            adm = _admin()
            if a == "precio":
                adm.fijar_precio(plan=F["plan"], meses=int(F["meses"]), monto_usd=F["monto"], max_usuarios=int(F["usuarios"]), max_empresas=int(F["empresas"]), max_equipos=int(F["equipos"]))
            elif a == "confirmar": adm.confirmar(F["ref"], g.usuario.username)
            elif a == "rechazar": adm.rechazar(F["ref"], F.get("motivo") or "Pago no verificado")
            elif a == "verificar": flash(f"Binance: {adm.verificar(F['ref'])}")
            else: raise ValueError("Acción inválida")
            registrar(g.usuario.username, "PAGO_" + a.upper(), F.get("ref") or F.get("plan", ""), org=None); db.session.commit()
            flash("Hecho" if a != "confirmar" else "Pago confirmado: licencia emitida y enviada por correo.")
        except (ErrorAdmin, ValueError, OSError) as e:
            db.session.rollback(); flash(str(e))
        return redirect(request.url)
    try:
        adm = _admin(); ords, precs = adm.ordenes(), adm.precios()
    except (ErrorAdmin, ValueError, OSError) as e:
        flash(str(e)); ords, precs = [], []
    orgs = {str(o.id): o.nombre for o in db.session.execute(select(Organizacion)).scalars()}
    import json as _json
    def acciones(o):
        if o["estado"] == "por_verificar":
            return dict(action=request.path, boton="Confirmar pago", campos=[h("confirmar"), campo("ref", "", "hidden", valor=o["ref"])])
        if o["proveedor"] == "binance" and o["estado"] in ("pendiente", "pagada"):
            return dict(action=request.path, boton="Buscar pago en Binance", campos=[h("verificar"), campo("ref", "", "hidden", valor=o["ref"])])
        return None
    ref_pago = lambda o: ", ".join(f"{k}: {v}" for k, v in _json.loads(o["referencia_pago"]).items() if v) if o.get("referencia_pago") else ""
    planes = [(k, v["nombre"]) for k, v in PLANES.items()]
    return pagina("Pagos y precios", None, [
        dict(kpis=[("Órdenes", len(ords)), ("Por verificar (Bs)", sum(o["estado"] == "por_verificar" for o in ords)),
                   ("Entregadas", sum(o["estado"] == "entregada" for o in ords)), ("Cobrado USD", f"{sum(float(o['monto_usd']) for o in ords if o['estado'] == 'entregada'):,.2f}")]),
        dict(titulo="Órdenes", nota="USDT: cada orden tiene un monto único; el sistema revisa su historial de Binance cada 2 min y entrega la licencia sola. "
                  "Si un cliente pagó un monto distinto, verifíquelo en Binance y use «Confirmar». Bs (pago móvil): verifique en su banco y confirme aquí.",
             tabla=dict(cols=["Orden", "Cliente", "Plan", "Meses", "Moneda", "Monto", "Estado", "Pago informado", "Fecha (UTC)"], num=[5], accion=True,
                        filas=[dict(cells=[o["ref"], orgs.get(o["organizacion_ref"], o["cliente"]), o["plan"], o["meses"], o["moneda"], o.get("monto_esperado") or o["monto"], o["estado"], ref_pago(o),
                                           _fecha(o["creada_en"])], form=acciones(o)) for o in ords])),
        dict(titulo="Confirmar un pago manualmente", nota="Para pagos que no se emparejaron solos (p. ej. el cliente envió otro monto). Verifique primero en su Binance o banco: al confirmar se emite la licencia y se envía la clave.",
             form=dict(action=request.path, boton="Confirmar y entregar", campos=[h("confirmar"), campo("ref", "Orden", "select", opciones=[(o["ref"], f"{o['ref']} · {orgs.get(o['organizacion_ref'], o['cliente'])} · {o['moneda']} {o.get('monto_esperado') or o['monto']}") for o in ords if o["estado"] in ("pendiente", "por_verificar", "pagada")])])),
        dict(titulo="Rechazar orden", form=dict(action=request.path, boton="Rechazar", campos=[h("rechazar"), campo("ref", "Orden", "select", opciones=[(o["ref"], o["ref"]) for o in ords if o["estado"] in ("pendiente", "por_verificar")]),
                                                                                           campo("motivo", "Motivo")])),
        dict(titulo="Catálogo de precios (USD / USDT)", nota="El monto de cada orden lo fija SIEMPRE este catálogo en el servidor; el cliente no puede alterarlo. En Bs se convierte a la tasa BCV del día.",
             tabla=dict(cols=["Plan", "Meses", "Precio USD", "Usuarios", "Empresas", "Equipos"], num=[2, 3, 4, 5], filas=[dict(cells=[PLANES[p["plan"]]["nombre"], p["meses"], p["monto_usd"], p["max_usuarios"], p["max_empresas"], p["max_equipos"]]) for p in precs])),
        dict(titulo="Fijar precio", form=dict(action=request.path, boton="Guardar precio", campos=[h("precio"), campo("plan", "Plan", "select", opciones=planes), campo("meses", "Meses", "number", valor=12),
                                                                                                 campo("monto", "Precio USD", "number"), campo("usuarios", "Usuarios", "number", valor=5),
                                                                                                 campo("empresas", "Empresas", "number", valor=2), campo("equipos", "Equipos", "number", valor=5)]))])


@ruta("/plataforma/base-datos", "plataforma", ("GET", "POST"))
def maestro_bd():
    """Salud de la base de datos y resumen por cliente. El Maestro ve CONTEOS, nunca el contenido de los libros (RLS)."""
    t = db.text
    if request.method == "POST":
        if F.get("accion") == "purgar":
            n = db.session.execute(t("DELETE FROM tokens_correo WHERE expira_en < now() - interval '30 days' OR usado_en < now() - interval '30 days'")).rowcount
            registrar(g.usuario.username, "BD_PURGA_TOKENS", str(n), org=None); db.session.commit(); flash(f"{n} enlaces vencidos eliminados")
        return redirect(request.url)
    info = db.session.execute(t("SELECT version(), pg_size_pretty(pg_database_size(current_database())), current_database(), "
                                "(SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()), current_user, "
                                "(SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user)")).one()
    tablas = db.session.execute(t("SELECT relname, n_live_tup, pg_size_pretty(pg_total_relation_size(relid)) FROM pg_stat_user_tables "
                                  "ORDER BY pg_total_relation_size(relid) DESC LIMIT 25")).all()
    orgs = {o.id: o for o in db.session.execute(select(Organizacion)).scalars()}
    resumen = db.session.execute(t("SELECT * FROM maestro_resumen_organizaciones()")).all()
    cadenas = {r[0]: r for r in db.session.execute(t("SELECT * FROM maestro_cadenas_bitacora()")).all()}
    return pagina("Base de datos", None, [
        dict(pares=[("Motor", info[0].split(",")[0]), ("Base", info[2]), ("Tamaño", info[1]), ("Conexiones abiertas", info[3]), ("Rol de la aplicación", info[4]),
                    ("Aislamiento entre clientes", "ATENCIÓN: la app corre con un rol que ignora RLS (configure DB_ROL_APLICACION)" if info[5]
                     else "Row-Level Security activo")]),
        dict(titulo="Clientes (solo conteos)", tabla=dict(cols=["Organización", "Estado", "Empresas", "Usuarios activos", "Asientos", "Documentos de venta", "Equipos", "Eventos en bitácora", "Último evento"],
                                                          num=[2, 3, 4, 5, 6, 7], filas=[dict(cells=[orgs[r[0]].nombre, orgs[r[0]].estado, r[1], r[2], r[3], r[4], r[5],
                                                                                                     cadenas.get(r[0], (0, 0))[1], cadenas[r[0]][2].strftime("%Y-%m-%d %H:%M") if r[0] in cadenas else "—"]) for r in resumen])),
        dict(titulo="Tablas más grandes", tabla=dict(cols=["Tabla", "Filas (aprox.)", "Tamaño"], num=[1], filas=[dict(cells=list(r)) for r in tablas])),
        dict(titulo="Mantenimiento", nota="Elimina enlaces de correo vencidos o usados hace más de 30 días. Los respaldos y la recuperación a un punto en el tiempo los gestiona Render (plan de pago).",
             form=dict(action=request.path, boton="Purgar enlaces vencidos", campos=[h("purgar")]))])


# ====================================================================== facturación de Gigadevs (venta de licencias)
@ruta("/plataforma/facturas", "plataforma", ("GET", "POST"))
def maestro_facturas():
    """Módulo para CONSTRUIR facturas de Gigadevs. Las de licencias pagadas se emiten solas y se envían en un segundo correo."""
    from .licensing.admin import ErrorAdmin
    from .tasas import tasa_vigente
    from .tiempo import hoy_ve
    orgs = db.session.execute(select(Organizacion).order_by(Organizacion.nombre)).scalars().all()
    if request.method == "POST":
        a, autor = F["accion"], g.usuario.username
        try:
            adm = _admin()
            if a == "emisor":
                adm.fx_guardar(**{k: F.get(k) for k in ("razon_social", "rif", "domicilio", "telefono", "correo", "contribuyente_especial", "iva_pct", "serie",
                                                       "control_prefijo", "imprenta_razon_social", "imprenta_rif", "imprenta_providencia")}); msg = "Datos del emisor guardados"
            elif a == "rango":
                adm.fx_rango(desde=F["desde"], hasta=F["hasta"], fecha=F["fecha"], providencia=F.get("providencia") or None); msg = "Rango de control registrado"
            elif a == "crear":
                org = next((o for o in orgs if str(o.id) == F.get("org")), None)
                nombre, rif = (org.nombre, org.rif) if org else (F.get("cliente_nombre", ""), validar_rif(F.get("cliente_rif")))
                correo_cli = (F.get("cliente_correo") or (org.correo_contacto if org else "") or "").strip().lower() or None
                moneda = F.get("moneda", "USD")
                tasa = None if moneda == "VES" else (F.get("tasa") or tasa_vigente(hoy_ve(), "EUR" if moneda == "EUR" else "USD"))
                lineas = [dict(descripcion=F.get(f"l{i}_desc"), cantidad=F.get(f"l{i}_cant") or "1", precio=F.get(f"l{i}_precio") or "0",
                               descuento_pct="0", alicuota=F.get(f"l{i}_alic") or "G") for i in range(1, 6) if (F.get(f"l{i}_desc") or "").strip()]
                if not lineas: raise ValueError("Agregue al menos una línea")
                r = adm.crear_factura(cliente_nombre=nombre, cliente_rif=rif, cliente_direccion=F.get("cliente_direccion"), cliente_correo=correo_cli,
                                      moneda=moneda, tasa_bcv=str(tasa) if tasa else None, lineas=lineas, forma_pago=F.get("forma_pago"), autor=autor)
                registrar(autor, "FACTURA_GIGADEVS_EMITIDA", f"id={r['id']} cliente={rif}", org=None); db.session.commit()
                return redirect(url_for("web.maestro_factura_ver", fid=r["id"]))
            elif a == "reenviar": adm.reenviar_factura(int(F["fid"])); msg = "Factura reenviada al cliente"
            elif a == "anular": adm.anular_factura(int(F["fid"]), F.get("motivo", "")); msg = "Factura anulada (conserva su número y control)"
            elif a == "orden": adm.facturar_orden(F["ref"]); msg = "Factura emitida y enviada"
            else: raise ValueError("Acción inválida")
            registrar(autor, "FACTURA_GIGADEVS_" + a.upper(), F.get("fid") or F.get("ref") or "", org=None); db.session.commit()
            flash(msg)
        except (ErrorAdmin, ValueError, OSError) as e:
            db.session.rollback(); flash(str(e))
        return redirect(request.url)
    try:
        adm = _admin(); cfgf, fs, ords = adm.fx_config(), adm.facturas(), adm.ordenes()
    except (ErrorAdmin, ValueError, OSError) as e:
        flash(str(e)); cfgf, fs, ords = {"config": {}, "cadena_integra": True}, [], []
    c = cfgf["config"]
    facturadas = {f.get("orden_ref") for f in fs}
    sin_factura = [o for o in ords if o["estado"] == "entregada" and o["ref"] not in facturadas]
    disponibles = max(int(c.get("control_hasta") or 0) - int(c.get("control_siguiente") or 1) + 1, 0)
    secs = [dict(kpis=[("Facturas emitidas", sum(f["estado"] == "Emitida" for f in fs)), ("Números de control disponibles", disponibles),
                       ("Licencias sin facturar", len(sin_factura)), ("Cadena de facturas", "Íntegra" if cfgf.get("cadena_integra") else "ROTA")])]
    if not c.get("rif") or not disponibles:
        secs.append(dict(titulo="Falta configurar", nota="Para emitir facturas cargue los datos fiscales de Gigadevs y un rango de números de control asignado por su imprenta. "
                                                        "Mientras tanto las licencias se entregan igual y quedan «sin facturar»."))
    secs.append(dict(titulo="Facturas", tabla=dict(cols=["Número", "Control", "Fecha", "Cliente", "RIF", "Moneda", "Total Bs", "Estado", "Correo"], num=[6], filas=[
        dict(cells=[f["numero_completo"], f["numero_control"], f["fecha"], f["cliente_nombre"], f["cliente_rif"], f["moneda"], f["total"], f["estado"],
                    "Enviado" if f.get("correo_enviado_en") else "—"], url=url_for("web.maestro_factura_ver", fid=f["id"]),
             clase="anulado" if f["estado"] == "Anulada" else "") for f in fs])))
    if sin_factura:
        secs.append(dict(titulo="Licencias pagadas sin factura", tabla=dict(cols=["Orden", "Cliente", "Plan", "Moneda", "Monto"], accion=True, filas=[
            dict(cells=[o["ref"], o["cliente"], o["plan"], o["moneda"], o.get("monto_esperado") or o["monto"]],
                 form=dict(action=request.path, boton="Emitir factura", campos=[h("orden"), campo("ref", "", "hidden", valor=o["ref"])])) for o in sin_factura])))
    alic = [("G", f"IVA {str(c.get('iva_pct', '16')).rstrip('0').rstrip('.')}%"), ("E", "Exento (E)")]
    lineas = sum([[campo(f"l{i}_desc", f"Descripción {i}", req=i == 1), campo(f"l{i}_cant", "Cant.", "number", req=False, valor="1" if i == 1 else None),
                   campo(f"l{i}_precio", "Precio sin IVA", "number", req=i == 1), campo(f"l{i}_alic", "IVA", "select", opciones=alic)] for i in range(1, 6)], [])
    secs += [
        dict(titulo="Construir una factura", nota="Elija un cliente de la plataforma o escriba los datos del adquirente. Precios SIN IVA, en la moneda elegida; "
                                                  "en divisas se usa la tasa BCV del día. Al emitir se envía por correo con la plantilla de ERP y Gigadevs.",
             form=dict(action=request.path, boton="Emitir y enviar factura", campos=[h("crear"),
                       campo("org", "Cliente de la plataforma", "select", req=False, opciones=[("", "— Otro (escriba sus datos) —")] + [(o.id, f"{o.nombre} ({o.rif})") for o in orgs]),
                       campo("cliente_nombre", "Nombre o razón social (si es otro)", req=False), campo("cliente_rif", "RIF / C.I. (si es otro)", req=False),
                       campo("cliente_direccion", "Domicilio fiscal", req=False), campo("cliente_correo", "Correo para enviarla", "email", req=False),
                       campo("moneda", "Moneda", "select", opciones=[("USD", "USD"), ("EUR", "EUR"), ("VES", "Bolívares")]), campo("tasa", "Tasa BCV (vacío = la del día)", "number", req=False),
                       campo("forma_pago", "Forma de pago", req=False), *lineas])),
        dict(titulo="Datos fiscales de Gigadevs (emisor)", form=dict(action=request.path, boton="Guardar", campos=[h("emisor"),
             campo("razon_social", "Razón social", valor=c.get("razon_social")), campo("rif", "RIF", valor=c.get("rif")), campo("domicilio", "Domicilio fiscal", valor=c.get("domicilio")),
             campo("telefono", "Teléfono", req=False, valor=c.get("telefono")), campo("correo", "Correo", req=False, valor=c.get("correo")),
             campo("contribuyente_especial", "¿Contribuyente especial?", "select", opciones=[("0", "No"), ("1", "Sí")], valor=c.get("contribuyente_especial")),
             campo("iva_pct", "IVA %", "number", valor=c.get("iva_pct")), campo("serie", "Serie (opcional)", req=False, valor=c.get("serie")),
             campo("control_prefijo", "Prefijo de control", valor=c.get("control_prefijo")), campo("imprenta_razon_social", "Imprenta autorizada", valor=c.get("imprenta_razon_social")),
             campo("imprenta_rif", "RIF de la imprenta", valor=c.get("imprenta_rif")), campo("imprenta_providencia", "Providencia de la imprenta", valor=c.get("imprenta_providencia"))])),
        dict(titulo="Nuevo rango de números de control", nota=f"Próximo número de control: {c.get('control_prefijo', '00')}-{int(c.get('control_siguiente') or 1):08d}.",
             form=dict(action=request.path, boton="Registrar rango", campos=[h("rango"), campo("desde", "Desde", "number"), campo("hasta", "Hasta", "number"),
                       campo("fecha", "Fecha de asignación", "date", valor=hoy()), campo("providencia", "Providencia (si cambió)", req=False)]))]
    return pagina("Facturación Gigadevs", None, secs)


@ruta("/plataforma/facturas/<int:fid>", "plataforma", ("GET",))
def maestro_factura_ver(fid):
    """La factura tal como la recibe el cliente (misma plantilla), lista para imprimir o guardar en PDF."""
    from flask import Response
    from .licensing.admin import ErrorAdmin
    from . import plantilla_correo as pc
    try:
        f = _admin().factura(fid)
    except (ErrorAdmin, ValueError, OSError) as e:
        flash(str(e)); return redirect(url_for("web.maestro_facturas"))
    acciones = (f'<div class="no-print" style="max-width:680px;margin:16px auto 0;font-family:sans-serif;display:flex;gap:8px;flex-wrap:wrap">'
                f'<a href="{url_for("web.maestro_facturas")}" style="padding:10px 14px;border:1px solid #ccc;border-radius:10px;color:#191919;text-decoration:none">← Volver</a>'
                f'<form method="post" action="{url_for("web.maestro_facturas")}"><input type="hidden" name="csrf_token" value="{csrf_token_seguro()}">'
                f'<input type="hidden" name="accion" value="reenviar"><input type="hidden" name="fid" value="{int(fid)}">'
                f'<button style="padding:10px 14px;border:0;border-radius:10px;background:#87e64b;color:#1a4200;font-weight:700">Reenviar al cliente</button></form>'
                + (f'<form method="post" action="{url_for("web.maestro_facturas")}"><input type="hidden" name="csrf_token" value="{csrf_token_seguro()}">'
                   f'<input type="hidden" name="accion" value="anular"><input type="hidden" name="fid" value="{int(fid)}">'
                   f'<input name="motivo" required placeholder="Motivo de anulación" style="padding:9px;border:1px solid #ccc;border-radius:10px">'
                   f'<button style="padding:10px 14px;border:0;border-radius:10px;background:#c8372d;color:#fff;font-weight:700">Anular (solo hoy)</button></form>'
                   if f["estado"] == "Emitida" else "")
                + '</div><style>@media print{.no-print{display:none!important}}</style>')
    html = pc.factura_html(f, current_app.config["APP_BASE_URL"]).replace("</body>", acciones + "</body>", 1)
    g.csp_estilos_en_linea = True  # la plantilla del correo usa estilos en línea; sigue sin permitirse ningún script
    return Response(html, mimetype="text/html")


def csrf_token_seguro():
    from flask_wtf.csrf import generate_csrf
    return generate_csrf()
