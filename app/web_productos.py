"""Consola Maestra → módulos comerciales de cada sistema (CriteriSeg, Enlace MIT, Surtia, Quincena VE), como en Contax:
Clientes, Precios y planes, y Órdenes y pagos. Los planes se crean sin cliente; al confirmar el pago de una orden se crea o se
renueva la suscripción del cliente y se emite la factura con la marca del sistema."""
import re
from decimal import Decimal

from flask import abort, flash, g, redirect, request, url_for

from . import sistemas
from .auditoria import registrar
from .extensions import db
from .web import F, campo, pagina, ruta
from .web_plataforma import _admin, _fecha

h = lambda v: campo("accion", "", "hidden", valor=v)
PERIODOS = [("mensual", "Mensual"), ("anual", "Anual"), ("unico", "Pago único (10 años)")]
MONEDAS = [("USD", "Dólares (USD)"), ("VES", "Bolívares"), ("USDT", "USDT"), ("EUR", "Euros")]


def _sistema(pid):
    if pid not in sistemas.SISTEMAS or pid == "contax": abort(404)
    return sistemas.SISTEMAS[pid]


def _usd(v):
    return f"${Decimal(str(v)):,.2f}" if v not in (None, "") else "—"


def _cargar(fn, defecto):
    from .licensing.admin import ErrorAdmin
    try:
        return fn()
    except (ErrorAdmin, ValueError, OSError) as e:
        flash(f"No se pudo consultar el servicio de licencias: {str(e)[:180]}"); return defecto


def _hacer(fn, ok):
    from .licensing.admin import ErrorAdmin
    try:
        r = fn(); db.session.commit(); flash(ok(r) if callable(ok) else ok)
    except (ErrorAdmin, ValueError, OSError) as e:
        db.session.rollback(); flash(str(e)[:250])


def _producto(pid):
    return next((p for p in _cargar(lambda: _admin().productos(), []) if p["id"] == pid), None) or {"planes": [], "modulos": [], "tipo_sujeto": "organizacion"}


# ====================================================================== clientes
@ruta("/plataforma/sistemas/<pid>/clientes", "plataforma", ("GET", "POST"))
def maestro_producto_clientes(pid):
    S = _sistema(pid)
    prod = _producto(pid)
    cuenta = prod.get("tipo_sujeto") == "cuenta"
    if request.method == "POST":
        def guardar():
            r = _admin().guardar_cliente_producto(pid, referencia=F.get("referencia"), nombre=F.get("nombre"), correo=F.get("correo"),
                                                  telefono=F.get("telefono"), direccion=F.get("direccion"), estado=F.get("estado") or "activo",
                                                  autor=g.usuario.username)
            registrar(g.usuario.username, "CLIENTE_PRODUCTO_" + ("CREADO" if r["creado"] else "ACTUALIZADO"), f"{pid}/{r['cliente']['referencia']}", org=None)
            return r
        _hacer(guardar, lambda r: f"Cliente {'creado' if r['creado'] else 'actualizado'}: {r['cliente']['nombre']}")
        return redirect(url_for("web.maestro_producto_clientes", pid=pid))
    cs = _cargar(lambda: _admin().clientes_producto(pid), [])
    ed = next((c for c in cs if str(c["id"]) == request.args.get("editar", "")), None)
    sus = lambda c: (f"{c['suscripcion']['plan']} · {c['suscripcion']['estado']} · vence {_fecha(c['suscripcion']['vence_en'])}" if c["suscripcion"] else "Sin suscripción")
    nota_origen = ("Las empresas que se crean en la consola de Surtia llegan aquí solas (origen «Surtia»)." if pid == "surtia" else
                   "Registre aquí a los clientes del sistema; no hace falta que tengan una suscripción.")
    return pagina(f"Clientes · {S['nombre']}", None, [
        dict(kpis=[("Clientes", len(cs)), ("Con suscripción vigente", sum(1 for c in cs if c["suscripcion"] and c["suscripcion"]["estado"] == "activa")),
                   ("Enviados por el sistema", sum(c["origen"] == "sistema" for c in cs)), ("Suspendidos", sum(c["estado"] != "activo" for c in cs))]),
        dict(titulo=f"Clientes de {S['nombre']}", nota=nota_origen + " Pulse un cliente para editarlo.",
             tabla=dict(cols=["Cliente", "Correo" if cuenta else "RIF", "Contacto", "Origen", "Estado", "Suscripción"], filas=[
                 dict(cells=[c["nombre"], c["referencia"], c.get("correo") or c.get("telefono") or "—", S["nombre"] if c["origen"] == "sistema" else "Consola",
                             c["estado"].capitalize(), sus(c)], url=url_for("web.maestro_producto_clientes", pid=pid, editar=c["id"]),
                      clase=f"producto-{pid}") for c in cs])),
        dict(titulo=f"Editar a {ed['nombre']}" if ed else "Nuevo cliente", clase="maestro-form", abierto=True,
             nota="Si la referencia ya existe en este sistema, se actualizan sus datos.",
             form=dict(action=url_for("web.maestro_producto_clientes", pid=pid), boton="Guardar cliente", campos=[h("guardar"),
                 campo("nombre", "Nombre" if cuenta else "Razón social", valor=ed["nombre"] if ed else None),
                 campo("referencia", "Correo verificado de la cuenta" if cuenta else "RIF", "email" if cuenta else "text",
                       ph="" if cuenta else "J-12345678-9", valor=ed["referencia"] if ed else None),
                 campo("correo", "Correo de contacto y facturación", "email", req=False, valor=ed.get("correo") if ed else None),
                 campo("telefono", "Teléfono", req=False, valor=ed.get("telefono") if ed else None),
                 campo("direccion", "Domicilio fiscal", req=False, valor=ed.get("direccion") if ed else None),
                 campo("estado", "Estado", "select", opciones=[("activo", "Activo"), ("suspendido", "Suspendido")], valor=ed["estado"] if ed else "activo")]))],
        acciones=[dict(url=url_for("web.maestro_producto_ordenes", pid=pid), texto="Órdenes y pagos", primaria=True)])


# ====================================================================== precios y planes
@ruta("/plataforma/sistemas/<pid>/planes", "plataforma", ("GET", "POST"))
def maestro_producto_planes(pid):
    S = _sistema(pid)
    if request.method == "POST":
        if F.get("accion") == "estado":
            _hacer(lambda: _admin().activar_plan_producto(pid, F["codigo"], F.get("activo") == "1"), "Estado del plan actualizado")
        else:
            def guardar():
                plan = _admin().guardar_plan_producto(pid, (F.get("codigo") or "").strip().lower(), nombre=F.get("nombre"), descripcion=F.get("descripcion"),
                                                     precio_mensual=F.get("precio_mensual"), precio_anual=F.get("precio_anual"), precio_unico=F.get("precio_unico"),
                                                     max_usuarios=F.get("max_usuarios"), max_empresas=F.get("max_empresas"), max_equipos=F.get("max_equipos"),
                                                     max_instancias=F.get("max_instancias"), modulos=F.getlist("modulos"), autor=g.usuario.username)
                registrar(g.usuario.username, "PLAN_PRODUCTO_GUARDADO", f"{pid}/{plan['codigo']} {plan['precio_estado']}", org=None)
                return plan
            _hacer(guardar, lambda p: f"Plan «{p['nombre']}» {'creado' if p['creado'] else 'actualizado'} ({p['precio_estado']}).")
        return redirect(url_for("web.maestro_producto_planes", pid=pid))
    prod = _producto(pid)
    mods = [(m["codigo"], m["nombre"]) for m in prod.get("modulos", [])]
    nombre_mod = dict(mods)
    planes = prod.get("planes", [])
    ed = next((p for p in planes if p["codigo"] == request.args.get("editar")), None)
    limites = lambda p: " · ".join(f"{p[k]} {t}" for k, t in (("max_usuarios", "usuarios"), ("max_empresas", "empresas"), ("max_equipos", "equipos")) if p.get(k)) or "—"
    filas = [dict(cells=[p["nombre"], p["codigo"], _usd(p.get("precio_mensual")), _usd(p.get("precio_anual")), _usd(p.get("precio_unico")),
                         ", ".join(nombre_mod.get(m, m) for m in (p.get("modulos") or "").split(",") if m) or "Todos", limites(p),
                         "Activo" if p["activo"] else "Inactivo"],
                  url=url_for("web.maestro_producto_planes", pid=pid, editar=p["codigo"]), clase=f"producto-{pid}",
                  form=dict(action=url_for("web.maestro_producto_planes", pid=pid), boton="Desactivar" if p["activo"] else "Activar",
                            campos=[h("estado"), campo("codigo", "", "hidden", valor=p["codigo"]), campo("activo", "", "hidden", valor="0" if p["activo"] else "1")]))
             for p in planes]
    v = lambda k, d=None: (ed.get(k) if ed and ed.get(k) not in (None, "") else d)
    return pagina(f"Precios y planes · {S['nombre']}", None, [
        dict(kpis=[("Planes", len(planes)), ("Activos", sum(1 for p in planes if p["activo"])),
                   ("Con precio publicado", sum(1 for p in planes if p.get("precio_estado") == "Publicado")), ("Módulos del sistema", len(mods))]),
        dict(titulo=f"Planes de {S['nombre']}", nota="Precios en USD con IVA incluido, como en Contax. Un plan sin precio queda en «Cotizar» y se vende con presupuesto. "
             "Pulse un plan para editarlo.",
             tabla=dict(cols=["Plan", "Código", "Mensual", "Anual", "Pago único", "Módulos", "Límites", "Estado"], num=[2, 3, 4], filas=filas, accion=True, ancha=True)),
        dict(titulo=f"Editar «{ed['nombre']}»" if ed else "Nuevo plan", clase="maestro-form", abierto=True,
             nota="No necesita ningún cliente. Los módulos y límites del plan se aplican a las suscripciones que se creen o renueven con él.",
             form=dict(action=url_for("web.maestro_producto_planes", pid=pid), boton="Guardar plan", campos=[h("guardar"),
                 campo("nombre", "Nombre del plan", valor=v("nombre")),
                 campo("codigo", "Código corto (en la licencia)", ph="basico", valor=v("codigo")),
                 campo("descripcion", "Descripción", req=False, valor=v("descripcion")),
                 campo("precio_mensual", "Precio mensual USD", "number", req=False, valor=v("precio_mensual"), fijo=True),
                 campo("precio_anual", "Precio anual USD", "number", req=False, valor=v("precio_anual"), fijo=True),
                 campo("precio_unico", "Pago único USD", "number", req=False, valor=v("precio_unico"), fijo=True),
                 campo("max_usuarios", "Usuarios", "number", req=False, valor=v("max_usuarios")),
                 campo("max_empresas", "Empresas", "number", req=False, valor=v("max_empresas")),
                 campo("max_equipos", "Equipos", "number", req=False, valor=v("max_equipos")),
                 campo("max_instancias", "Instalaciones", "number", req=False, valor=v("max_instancias")),
                 campo("modulos", "Módulos incluidos", "checks", req=False, opciones=mods,
                       valor=(v("modulos") or "").split(",") if v("modulos") else [m for m, _ in mods])]))])


# ====================================================================== órdenes y pagos
@ruta("/plataforma/sistemas/<pid>/ordenes", "plataforma", ("GET", "POST"))
def maestro_producto_ordenes(pid):
    S = _sistema(pid)
    if request.method == "POST":
        a, autor = F.get("accion"), g.usuario.username
        ref = (F.get("ref") or "").strip()
        if a != "crear" and not re.fullmatch(r"OP\d{4}[0-9A-F]{6}", ref): flash("Orden inválida"); return redirect(request.url)
        if a == "crear":
            _hacer(lambda: _admin().crear_orden_producto(pid, cliente_id=F.get("cliente_id"), plan=F.get("plan"), periodo=F.get("periodo"),
                                                         monto=F.get("monto") or None, moneda=F.get("moneda"), forma_pago=F.get("forma_pago"),
                                                         nota=F.get("nota"), autor=autor),
                   lambda r: f"Orden {r} creada. Al recibir el pago, confírmela para activar la suscripción.")
        elif a == "pagar":
            def pagar():
                from .tasas import tasa_vigente
                from .tiempo import hoy_ve
                tasa = tasa_vigente(hoy_ve(), "USD")
                r = _admin().pagar_orden_producto(ref, forma_pago=F.get("forma_pago"), referencia_pago=F.get("referencia_pago"),
                                                  tasa=str(tasa) if tasa else None, autor=autor)
                registrar(autor, "ORDEN_PRODUCTO_PAGADA", f"{pid}/{ref} suscripción #{r['suscripcion_id']}", org=None)
                return r
            def aviso(r):
                fac = r.get("factura") or {}
                return (f"Pago confirmado: suscripción #{r['suscripcion_id']} {r['suscripcion']}. "
                        + (f"Factura {fac['numero']} emitida." if fac.get("numero") else f"Factura pendiente: {fac.get('error', 'emítala desde la orden')}"))
            _hacer(pagar, aviso)
        elif a == "facturar":
            _hacer(lambda: _admin().facturar_orden_producto(ref), lambda f: f"Factura {f.get('numero', '')} emitida")
        elif a == "anular":
            _hacer(lambda: _admin().anular_orden_producto(ref, F.get("motivo")), "Orden anulada")
        else: flash("Acción inválida")
        return redirect(url_for("web.maestro_producto_ordenes", pid=pid))
    datos = _cargar(lambda: _admin().ordenes_producto(pid), {"ordenes": [], "formas_pago": []})
    os_, formas = datos["ordenes"], [(f, f) for f in datos.get("formas_pago", [])]
    cs = _cargar(lambda: _admin().clientes_producto(pid), [])
    prod = _producto(pid)
    planes = [p for p in prod.get("planes", []) if p["activo"]]
    precio = lambda p: " · ".join(f"{t} {_usd(p.get(k))}" for k, t in (("precio_mensual", "mes"), ("precio_anual", "año"), ("precio_unico", "único")) if p.get(k)) or "Cotizar"
    pendientes = [o for o in os_ if o["estado"] == "Pendiente"]
    fila = lambda o: dict(cells=[o["ref"], o["cliente_nombre"], o["plan"], dict(PERIODOS).get(o["periodo"], o["periodo"]),
                                 f"{o['moneda']} {Decimal(o['monto']):,.2f}", o.get("forma_pago") or "—", o["estado"],
                                 f"#{o['suscripcion_id']}" if o.get("suscripcion_id") else "—", "Emitida" if o.get("factura_id") else "—", _fecha(o["creada_en"])],
                          clase="anulado" if o["estado"] == "Anulada" else f"producto-{pid}")
    secs = [dict(kpis=[("Órdenes", len(os_)), ("Por cobrar", len(pendientes)),
                       ("Cobrado USD", f"{sum(Decimal(o['monto']) for o in os_ if o['estado'] == 'Pagada' and o['moneda'] in ('USD', 'USDT')):,.2f}"),
                       ("Pagadas sin factura", sum(1 for o in os_ if o["estado"] == "Pagada" and not o.get("factura_id")))]),
            dict(titulo="Cómo se cobra", clase="maestro-contexto",
                 nota=f"Los pagos de {S['nombre']} entran por las mismas cuentas de Gigadevs que configura Contax (bolívares, dólares y USDT). "
                      "Cree la orden, confirme el pago cuando lo reciba y la suscripción del cliente se crea o se renueva sola, con su factura."),
            dict(titulo="Órdenes", tabla=dict(cols=["Orden", "Cliente", "Plan", "Período", "Monto", "Forma de pago", "Estado", "Suscripción", "Factura", "Fecha"],
                                              filas=[fila(o) for o in os_], ancha=True))]
    if not cs or not planes:
        secs.append(dict(titulo="Antes de crear una orden", nota=("Registre al menos un cliente en Clientes. " if not cs else "")
                         + ("Cree al menos un plan activo en Precios y planes." if not planes else "")))
    else:
        secs.append(dict(titulo="Nueva orden", clase="maestro-form", abierto=True,
                         nota="El monto se toma del precio del plan según el período; escriba otro monto solo si se negoció distinto.",
                         form=dict(action=url_for("web.maestro_producto_ordenes", pid=pid), boton="Crear orden", campos=[h("crear"),
                             campo("cliente_id", "Cliente", "select", opciones=[(c["id"], f"{c['nombre']} ({c['referencia']})") for c in cs if c["estado"] == "activo"]),
                             campo("plan", "Plan", "select", opciones=[(p["codigo"], f"{p['nombre']} · {precio(p)}") for p in planes]),
                             campo("periodo", "Período", "select", opciones=PERIODOS),
                             campo("monto", "Monto (vacío = precio del plan)", "number", req=False),
                             campo("moneda", "Moneda", "select", opciones=MONEDAS),
                             campo("forma_pago", "Forma de pago prevista", "select", req=False, opciones=[("", "—")] + formas),
                             campo("nota", "Nota interna", req=False)])))
    if pendientes:
        ops = [(o["ref"], f"{o['ref']} · {o['cliente_nombre']} · {o['moneda']} {Decimal(o['monto']):,.2f}") for o in pendientes]
        secs += [dict(titulo="Confirmar pago", clase="maestro-form", nota="Crea o renueva la suscripción del cliente con el plan de la orden y emite su factura.",
                      form=dict(action=url_for("web.maestro_producto_ordenes", pid=pid), boton="Confirmar pago", campos=[h("pagar"),
                          campo("ref", "Orden", "select", opciones=ops), campo("forma_pago", "Forma de pago", "select", opciones=formas),
                          campo("referencia_pago", "Referencia del pago", req=False)])),
                 dict(titulo="Anular orden", form=dict(action=url_for("web.maestro_producto_ordenes", pid=pid), boton="Anular", campos=[h("anular"),
                      campo("ref", "Orden", "select", opciones=ops), campo("motivo", "Motivo")]))]
    sin_factura = [o for o in os_ if o["estado"] == "Pagada" and not o.get("factura_id")]
    if sin_factura:
        secs.append(dict(titulo="Emitir factura pendiente", nota="Necesita los datos fiscales y números de control cargados en Contax → Facturación.",
                         form=dict(action=url_for("web.maestro_producto_ordenes", pid=pid), boton="Emitir factura", campos=[h("facturar"),
                             campo("ref", "Orden pagada", "select", opciones=[(o["ref"], f"{o['ref']} · {o['cliente_nombre']}") for o in sin_factura])])))
    return pagina(f"Órdenes y pagos · {S['nombre']}", None, secs,
                  acciones=[dict(url=url_for("web.maestro_producto_planes", pid=pid), texto="Precios y planes"),
                            dict(url=url_for("web.maestro_producto_clientes", pid=pid), texto="Clientes")])
