"""Profit Plus (solo lectura) y monitoreo del bot.

* Empresa → Clientes → «Profit Plus»: activa el conector, descarga su configuración y elige almacenes y lista de precios.
  La conexión a SQL Server se configura SOLO en el equipo del cliente (contax_profit.ini): la clave de la base nunca sale de su red.
* API del conector: /api/profit/v1/ping y /api/profit/v1/sync, firmadas con HMAC (app/profit.py).
* Maestro → «Bot y Profit Plus»: interruptor y parámetros de la API y monitoreo básico por empresa (solo conteos y estados)."""
import time
from datetime import timedelta
from pathlib import Path

from flask import Response, abort, current_app, g, redirect, request, url_for
from sqlalchemy import select

from . import profit as PF
from . import servicios_profit as SP
from .auditoria import registrar
from .extensions import csrf, db, limiter
from .models import _ahora
from .models_profit import ProfitArticulo, ProfitPlataforma
from .web import F, bp, campo, intentar, pagina, ruta

h = lambda v: campo("accion", "", "hidden", valor=v)
CARPETA_CONECTOR = Path(__file__).resolve().parent.parent / "conector_profit"
# Las URL no llevan extensión (.py, .sql, .ini): el filtro de rastreos (publico.py) las bloquea antes de llegar a Flask.
DESCARGAS = {"conector": ("contax_profit.py", "text/x-python"), "usuario-sql": ("crear_usuario_lectura.sql", "text/plain"),
             "instrucciones": ("LEAME.md", "text/markdown"), "requisitos": ("requirements.txt", "text/plain")}


def _hora(ts):
    from .tiempo import a_hora_ve
    return a_hora_ve(ts).strftime("%Y-%m-%d %H:%M") if ts else "—"


def _atraso(ts, intervalo):
    """Texto del estado de frescura de la última foto."""
    if ts is None: return "Nunca"
    minutos = int((_ahora() - ts).total_seconds() // 60)
    txt = "hace " + (f"{minutos} min" if minutos < 120 else f"{minutos // 60} h")
    return txt + (" ⚠️ atrasado" if minutos > 2 * intervalo + 5 else "")


# ====================================================================== pantalla de la empresa
@ruta("/e/<int:eid>/whatsapp/profit", "whatsapp.configurar", ("GET", "POST"))
def profit_config(eid):
    c, plat = SP.conexion(eid), SP.plataforma()
    if request.method == "POST":
        a = F.get("accion")
        def hacer():
            nonlocal c
            if plat is None or not plat.activa: raise ValueError("La integración con Profit Plus no está habilitada en la plataforma. Contacte a Gigadevs.")
            if a == "activar":
                c = SP.crear_conexion(eid); c.activo = True
                registrar(g.usuario.username, "PROFIT_ACTIVADO", f"empresa={eid} conector={c.conector_id}")
            elif c is None: raise ValueError("Primero active la integración")
            elif a == "guardar":
                elegidos = [x.strip()[:10] for x in request.form.getlist("almacenes") if x.strip()]
                elegidos += [x.strip()[:10] for x in (F.get("almacenes_txt") or "").replace(";", ",").split(",") if x.strip()]
                c.almacenes = sorted(set(elegidos))[:50]
                lp = (F.get("lista_precio") or "").strip()[:10]
                if not lp: raise ValueError("Indique la lista de precios de Profit (co_precio, p. ej. 01)")
                c.lista_precio = lp
                c.restar_comprometido = F.get("restar_comprometido", "1") == "1"
                c.solo_con_existencia = F.get("solo_con_existencia") == "1"
                c.activo = F.get("activo", "1") == "1"
                registrar(g.usuario.username, "PROFIT_CONFIGURADO", f"empresa={eid} almacenes={','.join(c.almacenes) or 'todos'} lista={c.lista_precio} activo={c.activo}")
            elif a == "rotar":
                SP.rotar_secreto(c)
                registrar(g.usuario.username, "PROFIT_CLAVE_ROTADA", f"empresa={eid}")
            else: raise ValueError("Acción inválida")
            db.session.commit()
        intentar(hacer, {"activar": "Integración activada: descargue la configuración del conector", "guardar": "Configuración guardada: el conector la aplicará en su próxima lectura",
                         "rotar": "Clave renovada: descargue de nuevo contax_profit.ini y reemplácelo en el servidor de Profit"}.get(a, "Hecho"))
        return redirect(request.url)

    secs = []
    if plat is None or not plat.activa:
        return pagina("Profit Plus", eid, [dict(titulo="Integración con Profit Plus", nota="La integración todavía no está habilitada en la plataforma. Contacte a soporte de Gigadevs.")])
    if c is None:
        return pagina("Profit Plus", eid, [dict(titulo="Conectar Profit Plus (solo lectura)", abierto=True,
            nota="El bot de WhatsApp usará el catálogo de Profit Plus: artículos, existencias por almacén, precios de venta y clientes, actualizados cada "
                 f"{plat.intervalo_min} minutos. Contax NO escribe ni envía nada a Profit Plus; la factura fiscal se sigue emitiendo en Profit. "
                 "Se instala un pequeño conector en el servidor donde corre Profit Plus: solo hace consultas de lectura y envía los datos a Contax por "
                 "HTTPS. No hay que abrir puertos ni exponer SQL Server a Internet.",
            form=dict(action=request.path, boton="Activar integración", campos=[h("activar")]))])
    res = SP.resumen(eid)
    secs.append(dict(kpis=[("Conector", ("Activo" if c.activo else "Pausado") + (" · con error" if c.ultimo_estado == "error" else "")),
                           ("Última foto", _atraso(c.ultima_sync, plat.intervalo_min)), ("Artículos", c.n_articulos), ("Con existencia", res["con_existencia"]),
                           ("Clientes", c.n_clientes)]))
    secs.append(dict(pares=[("Bot de WhatsApp", "Usa el catálogo de Profit Plus" if SP.activa(eid) else "Usa el inventario de Contax (aún no llega la primera foto de Profit)"),
                            ("Último contacto del conector", _hora(c.ultimo_contacto)), ("Último error", c.ultimo_error or "—"),
                            ("Equipo / versión", f"{c.agente_equipo or '—'} · {c.agente_version or '—'}"), ("ID del conector", c.conector_id),
                            ("Artículos con precio en divisas", f"{res['en_divisas']} (se muestran en Bs a la tasa BCV del día)")]))
    disp = c.disponibles or {}
    alms = [(x["codigo"], f"{x['codigo']} · {x['nombre']}") for x in disp.get("almacenes") or []]
    listas = [(x["codigo"], f"{x['codigo']} · {x['nombre']}") for x in disp.get("listas_precio") or []]
    campos = [h("guardar"), campo("activo", "Conector", "select", opciones=[("1", "Activo"), ("0", "Pausado: el bot vuelve al inventario de Contax")], valor="1" if c.activo else "0")]
    if alms: campos.append(campo("almacenes", "Almacenes que cuentan para la existencia (ninguno marcado = todos los de venta)", "checks", req=False, opciones=alms, valor=c.almacenes))
    else: campos.append(campo("almacenes_txt", "Almacenes (co_alma separados por coma; vacío = todos los de venta)", req=False, valor=", ".join(c.almacenes or [])))
    campos += [campo("lista_precio", "Lista de precios de venta", "select", opciones=listas, valor=c.lista_precio) if listas else
               campo("lista_precio", "Lista de precios (co_precio de Profit)", valor=c.lista_precio),
               campo("restar_comprometido", "Existencia que ve el cliente", "select", opciones=[("1", "Actual menos lo comprometido en pedidos"), ("0", "Existencia actual")],
                     valor="1" if c.restar_comprometido else "0"),
               campo("solo_con_existencia", "Artículos que ofrece el bot", "select", opciones=[("0", "Todos los que tienen precio (los agotados se marcan)"), ("1", "Solo los que tienen existencia")],
                     valor="1" if c.solo_con_existencia else "0")]
    secs.append(dict(titulo="Qué toma el bot de Profit", abierto=True, nota="Los almacenes y listas aparecen aquí cuando el conector se conecta por primera vez.",
                     form=dict(action=request.path, boton="Guardar", campos=campos)))
    descargas = [dict(url=url_for("web.profit_descarga", eid=eid, archivo="configuracion"), texto="⬇ contax_profit.ini (configuración con su clave)")]
    descargas += [dict(url=url_for("web.profit_descarga", eid=eid, archivo=k), texto="⬇ " + v[0]) for k, v in DESCARGAS.items()]
    if plat.url_instalador: descargas.insert(0, dict(url=plat.url_instalador, texto="⬇ Instalador contax_profit.exe (Windows)"))
    secs.append(dict(titulo="Instalar el conector en el servidor de Profit Plus", abierto=True,
                     nota="1) En SQL Server ejecute crear_usuario_lectura.sql (crea un usuario que solo puede LEER). 2) Copie contax_profit.exe (o contax_profit.py) y "
                          "contax_profit.ini en una carpeta del servidor, p. ej. C:\\Contax. 3) Complete en el .ini la sección [sqlserver]. 4) Ejecute «contax_profit --probar». "
                          "5) Instálelo como tarea programada: «contax_profit --instalar-tarea» (arranca con Windows y lee cada "
                          f"{plat.intervalo_min} minutos). Detalles en LEAME.md.", enlaces=descargas))
    secs.append(dict(titulo="Renovar la clave del conector", nota="Úsela si cree que el archivo contax_profit.ini se filtró. El conector deja de funcionar hasta que reemplace el archivo.",
                     form=dict(action=request.path, boton="Renovar clave", boton_clase="sec", confirmar="¿Renovar la clave? El conector actual dejará de sincronizar.", campos=[h("rotar")])))
    arts = db.session.execute(select(ProfitArticulo).where(ProfitArticulo.empresa_id == eid).order_by(ProfitArticulo.codigo).limit(25)).scalars().all()
    if arts:
        secs.append(dict(titulo="Muestra del catálogo recibido", tabla=dict(cols=["Código", "Descripción", "Precio", "Existencia", "Por almacén", "IVA"], num=[2, 3], filas=[
            dict(cells=[a.codigo, a.nombre, f"{a.moneda} {a.precio:,.2f}", f"{a.existencia.normalize():f}", " · ".join(f"{k}: {v}" for k, v in (a.por_almacen or {}).items()), a.alicuota])
            for a in arts])))
    secs.append(dict(titulo="Sincronizaciones recientes", tabla=dict(cols=["Fecha", "Resultado", "Artículos", "Clientes", "Duración", "Detalle"], filas=[
        dict(cells=[_hora(s.ts), s.resultado, s.articulos, s.clientes, f"{s.duracion_ms} ms" if s.duracion_ms is not None else "—", s.detalle or ""]) for s in SP.ultimas(eid)])))
    return pagina("Profit Plus", eid, secs, acciones=[dict(url=url_for("web.whatsapp_config", eid=eid), texto="← Bot de WhatsApp")])


@ruta("/e/<int:eid>/whatsapp/profit/descargar/<archivo>", "whatsapp.configurar")
def profit_descarga(eid, archivo):
    c = SP.conexion(eid)
    if c is None: abort(404)
    if archivo == "configuracion":
        registrar(g.usuario.username, "PROFIT_INI_DESCARGADO", f"empresa={eid}"); db.session.commit()
        return Response(SP.conector_ini(c), mimetype="text/plain; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename=contax_profit.ini", "Cache-Control": "no-store"})
    if archivo not in DESCARGAS: abort(404)
    nombre, tipo = DESCARGAS[archivo]
    return Response((CARPETA_CONECTOR / nombre).read_bytes(), mimetype=tipo + "; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename={nombre}"})


# ====================================================================== API del conector (sin usuario: firma HMAC)
def _conector():
    """Autentica la petición del conector. Devuelve (eid, org, conexión, plataforma, cuerpo crudo) o responde el error."""
    from . import rls
    cid = request.headers.get("X-Contax-Conector", "")
    if not cid or len(cid) > 64: abort(401)
    fila = db.session.execute(db.text("SELECT empresa_id, organizacion_id FROM profit_destino(:c)"), {"c": cid}).first()
    if fila is None: abort(401)
    eid, org = fila
    rls.fijar_org(db, org)
    c, plat = SP.conexion(eid), SP.plataforma()
    cuerpo = request.get_data(cache=True)
    if c is None or not PF.firma_valida(SP.secreto(c), request.headers.get("X-Contax-Fecha", ""), cuerpo, request.headers.get("X-Contax-Firma", "")):
        abort(401)
    if plat is None or not plat.activa: abort(503)
    est = current_app.extensions["gestor_licencia"](org).estado()
    from .planes import modulos_de
    if not (est.valida and {"facturacion", "inventario"} & set(modulos_de(est.claims))): abort(402)
    return eid, org, c, plat, cuerpo


def _respuesta(c, plat, **extra):
    return {"ok": True, "config": PF.config_para_conector(c, plat.intervalo_min), **extra}


@bp.route("/api/profit/v1/ping", methods=["POST"])
@csrf.exempt
@limiter.limit("30/minute")
def profit_api_ping():
    eid, org, c, plat, cuerpo = _conector()
    try:
        d = PF.descomprimir(cuerpo, request.headers.get("Content-Encoding", ""))
    except PF.ErrorProfit as e:
        return {"ok": False, "error": str(e)}, 400
    c.ultimo_contacto = _ahora()
    c.agente_version = str(d.get("version") or c.agente_version or "")[:20] or None
    c.agente_equipo = str(d.get("equipo") or c.agente_equipo or "")[:60] or None
    if d.get("error"):                                   # el conector avisa que no pudo leer SQL Server
        SP.error_conector(eid, c, f"Conector: {str(d['error'])[:400]}", ip=request.remote_addr)
    else: SP.anotar(eid, "ping", ip=request.remote_addr)
    db.session.commit()
    return _respuesta(c, plat)


@bp.route("/api/profit/v1/sync", methods=["POST"])
@csrf.exempt
@limiter.limit("12/minute")
def profit_api_sync():
    t0 = time.monotonic()
    eid, org, c, plat, cuerpo = _conector()
    if not c.activo:
        c.ultimo_contacto = _ahora(); db.session.commit()
        return _respuesta(c, plat, ignorado="pausado")
    try:
        foto = PF.validar_foto(PF.descomprimir(cuerpo, request.headers.get("Content-Encoding", "")), plat.max_articulos, plat.max_clientes)
    except PF.ErrorProfit as e:
        from . import rls
        db.session.rollback(); rls.fijar_org(db, org)
        c = SP.conexion(eid); SP.error_conector(eid, c, str(e), ip=request.remote_addr); db.session.commit()
        return {"ok": False, "error": str(e)}, 400
    SP.aplicar_foto(eid, c, foto, ip=request.remote_addr, duracion_ms=int((time.monotonic() - t0) * 1000))
    db.session.commit()
    return _respuesta(c, plat, articulos=c.n_articulos, clientes=c.n_clientes)


# ====================================================================== consola Maestro
@ruta("/plataforma/bot", "plataforma", ("GET", "POST"))
def maestro_bot():
    """API del conector de Profit Plus y monitoreo del bot de todas las empresas. El Maestro ve conteos y estados técnicos,
    nunca conversaciones, clientes ni precios (funciones SECURITY DEFINER acotadas, migración 012)."""
    from . import openwa as OW
    p = SP.plataforma()
    if request.method == "POST":
        def guardar():
            nonlocal p
            if F.get("accion") != "guardar": raise ValueError("Acción inválida")
            url = (F.get("url_publica") or "").strip().rstrip("/")
            inst = (F.get("url_instalador") or "").strip()
            for u in (url, inst):
                if u and not u.startswith("https://"): raise ValueError("Las URL deben empezar por https://")
            intervalo = int(F.get("intervalo_min") or 15)
            if not 5 <= intervalo <= 1440: raise ValueError("El intervalo debe estar entre 5 y 1440 minutos")
            arts, clis = int(F.get("max_articulos") or 20000), int(F.get("max_clientes") or 50000)
            if not (100 <= arts <= 200000 and 100 <= clis <= 500000): raise ValueError("Límites fuera de rango")
            if p is None:
                p = ProfitPlataforma(id=1); db.session.add(p)
            p.activa, p.url_publica, p.url_instalador = F.get("activa", "1") == "1", url or None, inst or None
            p.intervalo_min, p.max_articulos, p.max_clientes, p.actualizado_por = intervalo, arts, clis, g.usuario.username
            registrar(g.usuario.username, "PROFIT_API", f"activa={p.activa} intervalo={intervalo} url={url or 'APP_BASE_URL'}", org=None)
            db.session.commit()
        intentar(guardar, "API de Profit Plus guardada")
        return redirect(request.url)

    filas = db.session.execute(db.text("SELECT * FROM maestro_monitor_bot()")).mappings().all()
    intervalo = p.intervalo_min if p else 15
    ahora = _ahora()
    conectados = sum(1 for f in filas if f["sesion_estado"] == "ready" or f["proveedor"] in ("meta", "wasender"))
    atrasados = [f for f in filas if f["profit_activo"] and (f["profit_ultima_sync"] is None or ahora - f["profit_ultima_sync"] > timedelta(minutes=2 * intervalo + 5))]
    caidos = [f for f in filas if f["proveedor"] == "openwa" and f["sesion_estado"] not in ("ready",)]
    alertas = [dict(cells=[f"{f['razon_social']} ({f['organizacion']})", "WhatsApp " + OW.ESTADOS.get(f["sesion_estado"], f["sesion_estado"] or "sin vincular").lower()]) for f in caidos]
    alertas += [dict(cells=[f"{f['razon_social']} ({f['organizacion']})", "Profit: " + ("sin foto aún" if f["profit_ultima_sync"] is None else "foto " + _atraso(f["profit_ultima_sync"], intervalo))
                            + (f" · {f['profit_error']}" if f["profit_estado"] == "error" and f["profit_error"] else "")]) for f in atrasados]
    alertas += [dict(cells=[f"{f['razon_social']} ({f['organizacion']})", f"{f['fallidos_24h']} respuesta(s) del bot sin enviar en 24 h"]) for f in filas if f["fallidos_24h"]]
    wa = lambda f: ("Meta (anterior)" if f["proveedor"] == "meta" else "WasenderAPI (anterior)" if f["proveedor"] == "wasender"
                    else OW.ESTADOS.get(f["sesion_estado"], "Sin vincular") if f["proveedor"] else "—")
    return pagina("Bot y Profit Plus", None, [
        dict(kpis=[("API Profit", ("Activa" if p.activa else "Pausada") if p else "Sin configurar"), ("Empresas con bot", len(filas)), ("WhatsApp conectados", conectados),
                   ("Mensajes 24 h", sum(f["recibidos_24h"] + f["enviados_24h"] for f in filas)), ("Conectores Profit", sum(1 for f in filas if f["profit_activo"])),
                   ("Alertas", len(alertas))]),
        dict(titulo="Alertas", tabla=dict(cols=["Empresa", "Qué pasa"], filas=alertas or [dict(cells=["—", "Todo en orden ✅"])])),
        dict(titulo="Monitoreo por empresa", tabla=dict(cols=["Empresa", "WhatsApp", "Bot", "Recibidos 24 h", "Enviados 24 h", "Fallidos 24 h", "Último mensaje", "Pedidos 24 h",
                                                            "Profit: última foto", "Artículos", "Conector"], num=[3, 4, 5, 7, 9], filas=[
            dict(cells=[f"{f['razon_social']} ({f['organizacion']})", wa(f), "—" if f["bot_activo"] is None else ("Activo" if f["bot_activo"] else "Pausado"),
                        f["recibidos_24h"], f["enviados_24h"], f["fallidos_24h"], _hora(f["ultimo_mensaje"]), f["pedidos_24h"],
                        ("—" if f["profit_activo"] is None else (_atraso(f["profit_ultima_sync"], intervalo) if f["profit_activo"] else "Pausado")) + (" · error" if f["profit_estado"] == "error" else ""),
                        f["profit_articulos"] if f["profit_activo"] is not None else "—", f["profit_version"] or "—"]) for f in filas])),
        dict(titulo="API del conector de Profit Plus", abierto=True,
             nota="El conector corre en el servidor de Profit Plus de cada cliente y llama a esta URL por HTTPS (solo salida; el cliente no abre puertos). "
                  "Cada empresa tiene su propio ID y clave, que ella descarga en contax_profit.ini. Pausar la API detiene la sincronización de todos los clientes: "
                  "sus bots siguen atendiendo con la última foto recibida.",
             form=dict(action=request.path, boton="Guardar", campos=[h("guardar"),
                       campo("activa", "API", "select", opciones=[("1", "Activa"), ("0", "Pausada")], valor="1" if (p is None or p.activa) else "0"),
                       campo("url_publica", "URL pública que usan los conectores", req=False, valor=p.url_publica if p else None, ph=current_app.config["APP_BASE_URL"]),
                       campo("intervalo_min", "Cada cuántos minutos sincronizan", "number", valor=intervalo),
                       campo("max_articulos", "Máximo de artículos por empresa", "number", valor=p.max_articulos if p else 20000),
                       campo("max_clientes", "Máximo de clientes por empresa", "number", valor=p.max_clientes if p else 50000),
                       campo("url_instalador", "Descarga del instalador contax_profit.exe (https://…, opcional)", req=False, valor=p.url_instalador if p else None)])),
    ], acciones=[dict(url=url_for("web.maestro_whatsapp"), texto="Pasarela de WhatsApp →")])
