"""Parte comercial de los demás sistemas de Gigadevs (CriteriSeg, Enlace MIT, Surtia, Quincena VE), igual que en Contax:
clientes de cada sistema, planes con precio (sin necesidad de un cliente), órdenes y pagos que crean o renuevan la suscripción
y emiten la factura con la marca del sistema.

Los sistemas que tienen su propia consola (Surtia) envían aquí sus clientes por la API /sync/v1/<sistema>/…, firmada con HMAC
y un secreto propio de cada sistema (SYNC_SECRET_<SISTEMA>), y traen de vuelta la licencia firmada de cada cliente."""
import hashlib
import hmac
import re
import secrets
import time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from flask import jsonify, request
from sqlalchemy import Column, Float, Integer, MetaData, Numeric, String, Table, Text, UniqueConstraint, insert, select, update

md = MetaData()
clientes = Table(
    "lic_clientes_producto", md,
    Column("id", Integer, primary_key=True), Column("producto_id", String(40), nullable=False),
    Column("referencia", String(200), nullable=False),          # RIF (organización) o correo (cuenta individual)
    Column("nombre", Text, nullable=False), Column("correo", Text), Column("telefono", Text), Column("direccion", Text),
    Column("documento", Text),                                   # RIF o cédula para la factura (cuentas individuales)
    Column("origen", String(20), nullable=False, default="consola"),    # consola (Maestro) | sistema (lo envió el propio sistema)
    Column("externo_id", Text), Column("estado", String(12), nullable=False, default="activo"),
    Column("creado_en", Float, nullable=False), Column("actualizado_en", Float, nullable=False),
    UniqueConstraint("producto_id", "referencia", name="uq_cliente_producto"))
ordenes = Table(
    "lic_ordenes_producto", md,
    Column("id", Integer, primary_key=True), Column("ref", String(20), nullable=False, unique=True),
    Column("producto_id", String(40), nullable=False), Column("cliente_id", Integer, nullable=False),
    Column("plan", String(32), nullable=False), Column("periodo", String(10), nullable=False), Column("meses", Integer, nullable=False),
    Column("monto", Numeric(18, 2), nullable=False), Column("moneda", String(4), nullable=False, default="USD"),
    Column("forma_pago", Text), Column("referencia_pago", Text), Column("tasa", Numeric(18, 6)),
    Column("estado", String(10), nullable=False, default="Pendiente"),     # Pendiente | Pagada | Anulada
    Column("suscripcion_id", Integer), Column("factura_id", Integer), Column("nota", Text),
    # cobro en línea con Ávila Pay (la persona paga desde el propio sistema, p. ej. la app de Quincena VE)
    Column("proveedor", String(20)), Column("metodo", String(20)), Column("prepay_id", Text), Column("checkout_url", Text),
    Column("expira_en", Float), Column("precio_usd", Numeric(18, 2)), Column("ajuste", Numeric(18, 2)),
    Column("creada_en", Float, nullable=False), Column("pagada_en", Float), Column("autor", Text))
COLUMNAS_PLANES = [("descripcion", "TEXT"), ("precio_mensual", "NUMERIC(18,2)"), ("precio_anual", "NUMERIC(18,2)"), ("precio_unico", "NUMERIC(18,2)"),
                   ("max_usuarios", "INTEGER"), ("max_empresas", "INTEGER"), ("max_equipos", "INTEGER"), ("max_instancias", "INTEGER"),
                   ("modulos", "TEXT")]
PERIODOS = {"mensual": 1, "anual": 12, "unico": 120}           # «pago único»: 10 años, el máximo de una licencia
FORMAS_PAGO = ("Transferencia en bolívares", "Pago móvil", "Zelle", "USDT (Binance)", "Tarjeta internacional", "Efectivo", "Otro")
RE_RIF = re.compile(r"^[VEJPGC]-\d{8}-\d$")
RE_ORDEN = re.compile(r"^OP\d{4}[0-9A-F]{6}$")                 # sin guiones: Ávila Pay la usa como referencia externa
D2 = Decimal("0.01")


def instalar(eng):
    from sqlalchemy import inspect, text
    md.create_all(eng)
    with eng.begin() as c:
        existentes = {x["name"] for x in inspect(c).get_columns("lic_planes_producto")}
        for nombre, tipo in COLUMNAS_PLANES:
            if nombre not in existentes: c.execute(text(f"ALTER TABLE lic_planes_producto ADD COLUMN {nombre} {tipo}"))


def _monto(v, nombre="precio", opcional=True):
    if v in (None, ""):
        if opcional: return None
        raise ValueError(f"Indique el {nombre}")
    try:
        d = Decimal(str(v).replace(",", ".")).quantize(D2, ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as e:
        raise ValueError(f"{nombre.capitalize()} inválido") from e
    if not d.is_finite() or d <= 0 or d > Decimal("10000000"): raise ValueError(f"{nombre.capitalize()} fuera de rango")
    return d


def _entero(v, nombre, minimo=1, maximo=100000):
    if v in (None, ""): return None
    try:
        n = int(v)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{nombre} inválido") from e
    if not (minimo <= n <= maximo): raise ValueError(f"{nombre} fuera de rango")
    return n


def _texto(v, n=300):
    return (str(v or "").strip()[:n]) or None


def _dict(fila):
    return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in dict(fila._mapping).items()}


def referencia_valida(tipo_sujeto, ref) -> str:
    ref = str(ref or "").strip()
    if tipo_sujeto == "cuenta":
        ref = ref.lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", ref): raise ValueError("La referencia de una cuenta individual es su correo")
        return ref
    ref = ref.upper().replace(" ", "")
    m = re.fullmatch(r"([VEJPGC])-?(\d{8})-?(\d)", ref)
    if not m: raise ValueError("RIF inválido (J-12345678-9)")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


class Comercial:
    def __init__(self, st, productos_cfg):
        self.st, self.cfg = st, productos_cfg
        # avisos por correo (los fija el servidor): factura emitida → se envía al cliente; orden creada → orden de compra
        self.al_facturar = None
        self.al_crear_orden = None
        instalar(st.eng)

    def _producto(self, pid):
        if pid not in self.cfg: raise ValueError("Sistema inválido")
        return self.cfg[pid]

    # ------------------------------------------------------------------ clientes
    def clientes(self, pid):
        self._producto(pid)
        with self.st.eng.connect() as c:
            filas = [_dict(r) for r in c.execute(select(clientes).where(clientes.c.producto_id == pid).order_by(clientes.c.nombre))]
        subs = {}
        for s in self.st.listar_suscripciones(pid):
            subs.setdefault(s["referencia_sujeto"], s)        # la más reciente primero
        for f in filas:
            s = subs.get(f["referencia"])
            f["suscripcion"] = ({"id": s["id"], "plan": s["plan"], "estado": s["estado"], "vence_en": s["vence_en"]} if s else None)
        return filas

    def guardar_cliente(self, pid, d, origen="consola"):
        """Crea el cliente o, si ya existe (mismo sistema y referencia), lo actualiza. Devuelve (cliente, creado)."""
        cfg = self._producto(pid)
        ref = referencia_valida(cfg["tipo_sujeto"], d.get("referencia"))
        nombre = _texto(d.get("nombre"), 200)
        if not nombre: raise ValueError("Indique el nombre o la razón social del cliente")
        correo = (_texto(d.get("correo"), 254) or "").lower() or None
        if correo and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", correo): raise ValueError("Correo del cliente inválido")
        estado = d.get("estado") or "activo"
        if estado not in ("activo", "suspendido"): raise ValueError("Estado inválido")
        ahora = self.st.reloj()
        documento = (_texto(d.get("documento"), 15) or "").upper().replace(" ", "") or None
        if documento:
            m = re.fullmatch(r"([VEJPGC])-?(\d{8})-?(\d)", documento)
            if not m: raise ValueError("RIF para la factura inválido (V-12345678-9)")
            documento = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        valores = dict(nombre=nombre, correo=correo, telefono=_texto(d.get("telefono"), 40), direccion=_texto(d.get("direccion")), documento=documento,
                       externo_id=_texto(d.get("externo_id"), 80), estado=estado, actualizado_en=ahora)
        with self.st.eng.begin() as c:
            fila = c.execute(select(clientes).where(clientes.c.producto_id == pid, clientes.c.referencia == ref).with_for_update()).first()
            if fila is None:
                cid = c.execute(insert(clientes).values(producto_id=pid, referencia=ref, origen=origen, creado_en=ahora, **valores)
                                .returning(clientes.c.id)).scalar()
                creado = True
            else:
                cid, creado = fila.id, False
                c.execute(update(clientes).where(clientes.c.id == cid).values(**{k: v for k, v in valores.items() if v is not None or k == "externo_id"}))
            return _dict(c.execute(select(clientes).where(clientes.c.id == cid)).first()), creado

    def cliente(self, cid, pid=None):
        with self.st.eng.connect() as c:
            fila = c.execute(select(clientes).where(clientes.c.id == int(cid))).first()
        if fila is None or (pid and fila.producto_id != pid): raise LookupError("Cliente inexistente")
        return _dict(fila)

    # ------------------------------------------------------------------ planes con precio
    def guardar_plan(self, pid, d):
        """Crea o actualiza un plan del sistema. No necesita ningún cliente."""
        from .server import _modulos_producto, planes_producto
        self._producto(pid)
        codigo = str(d.get("codigo") or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,30}", codigo): raise ValueError("Código del plan: minúsculas, números, guion o guion bajo")
        nombre = _texto(d.get("nombre"), 100)
        if not nombre: raise ValueError("Indique el nombre del plan")
        precios = {k: _monto(d.get(k), "precio") for k in ("precio_mensual", "precio_anual", "precio_unico")}
        valores = dict(nombre=nombre, descripcion=_texto(d.get("descripcion"), 500), **precios,
                       precio_estado="Publicado" if any(precios.values()) else "Cotizar",
                       max_usuarios=_entero(d.get("max_usuarios"), "usuarios"), max_empresas=_entero(d.get("max_empresas"), "empresas"),
                       max_equipos=_entero(d.get("max_equipos"), "equipos"), max_instancias=_entero(d.get("max_instancias"), "instalaciones", 1, 1000),
                       modulos=_modulos_producto(pid, d.get("modulos") or None))
        if self.cfg[pid]["tipo_sujeto"] == "cuenta": valores["max_usuarios"] = 1
        with self.st.eng.begin() as c:
            existe = c.execute(select(planes_producto.c.codigo).where(planes_producto.c.producto_id == pid, planes_producto.c.codigo == codigo)).first()
            if existe: c.execute(update(planes_producto).where(planes_producto.c.producto_id == pid, planes_producto.c.codigo == codigo).values(**valores))
            else: c.execute(insert(planes_producto).values(producto_id=pid, codigo=codigo, activo=1, **valores))
        return {"codigo": codigo, "creado": not existe, **{k: (str(v) if isinstance(v, Decimal) else v) for k, v in valores.items()}}

    def plan(self, pid, codigo):
        from .server import planes_producto
        with self.st.eng.connect() as c:
            fila = c.execute(select(planes_producto).where(planes_producto.c.producto_id == pid, planes_producto.c.codigo == codigo)).first()
        if fila is None: raise LookupError("Plan inexistente")
        return _dict(fila)

    # ------------------------------------------------------------------ órdenes y pagos
    def ordenes(self, pid):
        self._producto(pid)
        with self.st.eng.connect() as c:
            q = (select(ordenes, clientes.c.nombre.label("cliente_nombre"), clientes.c.referencia.label("cliente_referencia"))
                 .join(clientes, clientes.c.id == ordenes.c.cliente_id).where(ordenes.c.producto_id == pid).order_by(ordenes.c.id.desc()).limit(500))
            return [_dict(r) for r in c.execute(q)]

    def crear_orden(self, pid, d):
        self._producto(pid)
        cli = self.cliente(d.get("cliente_id"), pid)
        if cli["estado"] != "activo": raise ValueError("El cliente está suspendido")
        plan = self.plan(pid, str(d.get("plan") or ""))
        if not plan["activo"]: raise ValueError("Ese plan está inactivo")
        periodo = d.get("periodo") if d.get("periodo") in PERIODOS else "mensual"
        sugerido = plan.get({"mensual": "precio_mensual", "anual": "precio_anual", "unico": "precio_unico"}[periodo])
        monto = _monto(d.get("monto") or sugerido, "monto de la orden", opcional=False)
        moneda = d.get("moneda") or "USD"
        if moneda not in ("USD", "VES", "USDT", "EUR"): raise ValueError("Moneda inválida")
        ref = "OP" + time.strftime("%y%m", time.gmtime(self.st.reloj())) + secrets.token_hex(3).upper()
        with self.st.eng.begin() as c:
            c.execute(insert(ordenes).values(ref=ref, producto_id=pid, cliente_id=cli["id"], plan=plan["codigo"], periodo=periodo,
                                             meses=PERIODOS[periodo], monto=monto, moneda=moneda, forma_pago=_texto(d.get("forma_pago"), 60),
                                             estado="Pendiente", nota=_texto(d.get("nota"), 500), creada_en=self.st.reloj(), autor=_texto(d.get("autor"), 80)))
        return ref

    def orden(self, ref):
        with self.st.eng.connect() as c:
            fila = c.execute(select(ordenes).where(ordenes.c.ref == str(ref))).first()
        if fila is None: raise LookupError("Orden inexistente")
        return _dict(fila)

    def marcar_cobro(self, ref, **valores):
        with self.st.eng.begin() as c: c.execute(update(ordenes).where(ordenes.c.ref == str(ref)).values(**valores))

    def pendientes_avila(self, desde):
        with self.st.eng.connect() as c:
            return [r[0] for r in c.execute(select(ordenes.c.ref).where(ordenes.c.estado == "Pendiente", ordenes.c.proveedor.isnot(None),
                                                                        ordenes.c.creada_en > desde))]

    def planes_publicos(self, pid):
        """Planes activos con precio, para que el sistema los muestre a la persona (sin datos internos)."""
        from .server import planes_producto
        with self.st.eng.connect() as c:
            filas = c.execute(select(planes_producto).where(planes_producto.c.producto_id == pid, planes_producto.c.activo == 1)
                              .order_by(planes_producto.c.precio_mensual)).mappings().all()
        return [{"codigo": p["codigo"], "nombre": p["nombre"], "descripcion": p["descripcion"],
                 "precios": {k: str(p[f"precio_{k}"]) for k in PERIODOS if p.get(f"precio_{k}")}} for p in filas
                if any(p.get(f"precio_{k}") for k in PERIODOS)]

    def _orden(self, ref, con):
        fila = con.execute(select(ordenes).where(ordenes.c.ref == str(ref)).with_for_update()).first()
        if fila is None: raise LookupError("Orden inexistente")
        return fila

    def pagar(self, ref, d, emitir_factura):
        """Confirma el pago: crea o renueva la suscripción del cliente y emite la factura (si falla, la orden queda pagada sin factura)."""
        with self.st.eng.begin() as c:
            o = self._orden(ref, c)
            if o.estado != "Pendiente": raise ValueError(f"La orden ya está {o.estado.lower()}")
        cli, plan = self.cliente(o.cliente_id), self.plan(o.producto_id, o.plan)
        dias = 30 * o.meses if o.periodo == "mensual" else (365 if o.periodo == "anual" else 3650)
        vigente = next((s for s in self.st.listar_suscripciones(o.producto_id)
                        if s["referencia_sujeto"] == cli["referencia"] and s["estado"] == "activa" and s["vence_en"] > self.st.reloj()), None)
        limites = {k: plan.get(k) for k in ("max_usuarios", "max_instancias", "max_empresas", "max_equipos")}
        if vigente:
            self.st.cambiar_suscripcion(vigente["id"], dias_extra=dias, plan=o.plan if o.plan != vigente["plan"] else None,
                                        **{k: v for k, v in limites.items() if v})
            sid, accion = vigente["id"], "renovada"
        else:
            if not cli.get("correo"): raise ValueError("El cliente no tiene correo: agréguelo antes de confirmar el pago")
            sid = self.st.crear_suscripcion(o.producto_id, cli["nombre"], cli["correo"], cli["referencia"], o.plan, dias,
                                            self.cfg[o.producto_id]["entornos"][0], *(limites[k] or 1 for k in ("max_usuarios", "max_instancias", "max_empresas", "max_equipos")),
                                            f"Orden {o.ref}", (plan.get("modulos") or "").split(",") if plan.get("modulos") else None)
            accion = "creada"
        tasa = d.get("tasa")
        with self.st.eng.begin() as c:
            c.execute(update(ordenes).where(ordenes.c.ref == o.ref).values(
                estado="Pagada", pagada_en=self.st.reloj(), suscripcion_id=sid, forma_pago=_texto(d.get("forma_pago"), 60) or o.forma_pago,
                referencia_pago=_texto(d.get("referencia_pago"), 120), tasa=Decimal(str(tasa)) if tasa not in (None, "") else None))
        factura = None
        try:
            factura = self.facturar(o.ref, emitir_factura)
        except Exception as e:  # noqa: BLE001 — sin datos fiscales o sin RIF la orden queda pagada y se factura después
            factura = {"error": str(e)[:200]}
        return {"suscripcion_id": sid, "suscripcion": accion, "factura": factura}

    def facturar(self, ref, emitir_factura):
        r = self._facturar(ref, emitir_factura)
        if r.get("nueva") and self.al_facturar:          # toda persona que paga recibe su factura por correo
            try:
                self.al_facturar(r["id"]); r["enviada"] = True
            except Exception as e:  # noqa: BLE001 — la factura ya existe; el Maestro puede reenviarla
                r["envio_error"] = str(e)[:200]
        return r

    def _facturar(self, ref, emitir_factura):
        from .presupuestos import SISTEMAS
        with self.st.eng.begin() as c:
            o = self._orden(ref, c)
            if o.estado != "Pagada": raise ValueError("Solo se factura una orden pagada")
            if o.factura_id: return {"id": o.factura_id}
            cli = c.execute(select(clientes).where(clientes.c.id == o.cliente_id)).first()
            rif = cli.referencia if RE_RIF.match(cli.referencia) else (cli.documento or "")
            if not RE_RIF.match(rif): raise ValueError("Para facturar, el cliente necesita un RIF (cargue su RIF en Clientes y emita la factura)")
            periodo = {"mensual": f"{o.meses} mes(es)", "anual": "12 meses", "unico": "pago único"}[o.periodo]
            f = emitir_factura(c, cliente_nombre=cli.nombre, cliente_rif=rif, cliente_direccion=cli.direccion, cliente_correo=cli.correo,
                               moneda=o.moneda, tasa_bcv=o.tasa, monto_con_iva=o.monto, forma_pago=o.forma_pago, sistema=o.producto_id,
                               concepto=f"Licencia de uso {SISTEMAS.get(o.producto_id, o.producto_id)} — plan {o.plan} — {periodo}")
            c.execute(update(ordenes).where(ordenes.c.ref == o.ref).values(factura_id=f["id"]))
            return {"id": f["id"], "numero": f"{f['serie']}{int(f['numero']):08d}", "nueva": True}

    def anular(self, ref, motivo):
        with self.st.eng.begin() as c:
            o = self._orden(ref, c)
            if o.estado != "Pendiente": raise ValueError("Solo se anula una orden pendiente")
            c.execute(update(ordenes).where(ordenes.c.ref == o.ref).values(estado="Anulada", nota=_texto(motivo, 500) or o.nota))


def firma_valida(secreto, cabecera, metodo, ruta, cuerpo, reloj=time.time, tolerancia=300) -> str | None:
    """Firma de los sistemas: «t=…,n=…,f=hmac_sha256(t\\nn\\nMÉTODO\\nruta\\nsha256(cuerpo))». Devuelve el nonce si es válida.
    `secreto` puede ser una lista (secreto actual y anterior) para rotarlo sin cortar el servicio."""
    if isinstance(secreto, (list, tuple)):
        return next((n for n in (firma_valida(x, cabecera, metodo, ruta, cuerpo, reloj, tolerancia) for x in secreto if x) if n), None)
    if not secreto: return None
    partes = dict(p.split("=", 1) for p in (cabecera or "").split(",") if "=" in p)
    t, n, f = partes.get("t", ""), partes.get("n", ""), partes.get("f", "")
    if not (t.isdigit() and re.fullmatch(r"[0-9a-f]{32}", n) and re.fullmatch(r"[0-9a-f]{64}", f)): return None
    if abs(reloj() - int(t)) > tolerancia: return None
    base = "\n".join([t, n, metodo.upper(), ruta, hashlib.sha256(cuerpo).hexdigest()])
    return n if hmac.compare_digest(hmac.new(secreto.encode(), base.encode(), hashlib.sha256).hexdigest(), f) else None


def autenticar_sistema(st, secretos_sync, pid) -> bool:
    """Firma del sistema `pid` con su propio secreto (o el anterior, durante una rotación), hora ±5 min y nonce de un solo uso.
    Si es válida, la respuesta saldrá firmada con el mismo secreto y ligada a este nonce; si no, el intento queda en la bitácora."""
    from flask import g
    secretos = secretos_sync.get(pid)
    lista = secretos if isinstance(secretos, (list, tuple)) else [secretos]
    cab = request.headers.get("X-Gigadevs-Firma")
    for s in lista:
        n = firma_valida(s, cab, request.method, request.full_path.rstrip("?"), request.get_data(), st.reloj)
        if n is not None:
            if not st.usar_nonce(n): break                       # firma repetida: reenvío de una petición vieja
            g.firma_respuesta = (s, n)
            return True
    with st.eng.begin() as c:
        st.log(c, None, "sync_rechazado", f"{pid} {request.method} {request.path} firma={'sí' if cab else 'no'}", request.headers.get("X-Forwarded-For") or request.remote_addr)
    return False


def firma_respuesta(secreto, nonce, estado, cuerpo: bytes, t) -> str:
    """Cabecera X-Gigadevs-Respuesta: prueba de que la respuesta la dio este servidor para ESA petición (nonce) y no fue alterada."""
    base = "\n".join([str(int(t)), nonce, str(estado), hashlib.sha256(cuerpo).hexdigest()])
    return f"t={int(t)},f={hmac.new(secreto.encode(), base.encode(), hashlib.sha256).hexdigest()}"


def registrar_rutas(app, st, com, secretos_sync, clave_privada, emitir_factura, cuerpo):
    """Rutas /admin/v1/… (las protege la firma del Maestro) y /sync/v1/<sistema>/… (firma propia de cada sistema)."""

    @app.get("/admin/v1/productos/<pid>/clientes")
    def a_clientes_producto(pid):
        return jsonify(clientes=com.clientes(pid))

    @app.post("/admin/v1/productos/<pid>/clientes")
    def a_guardar_cliente_producto(pid):
        d = cuerpo(); cli, creado = com.guardar_cliente(pid, d)
        with st.eng.begin() as con: st.log(con, None, "cliente_producto_" + ("creado" if creado else "actualizado"), f"{pid}/{cli['referencia']} por {d.get('autor', '')}")
        return jsonify(cliente=cli, creado=creado), 201 if creado else 200

    @app.put("/admin/v1/productos/<pid>/planes/<codigo>")
    def a_guardar_plan_producto(pid, codigo):
        d = cuerpo(); plan = com.guardar_plan(pid, {**d, "codigo": codigo})
        with st.eng.begin() as con: st.log(con, None, "plan_producto_guardado", f"{pid}/{plan['codigo']} por {d.get('autor', '')}")
        return jsonify(plan=plan)

    @app.get("/admin/v1/productos/<pid>/ordenes")
    def a_ordenes_producto(pid):
        return jsonify(ordenes=com.ordenes(pid), formas_pago=list(FORMAS_PAGO))

    @app.post("/admin/v1/productos/<pid>/ordenes")
    def a_crear_orden_producto(pid):
        d = cuerpo(); ref = com.crear_orden(pid, d)
        with st.eng.begin() as con: st.log(con, None, "orden_producto_creada", f"{pid}/{ref} por {d.get('autor', '')}")
        enviada = False
        if com.al_crear_orden:                           # el cliente recibe su orden de compra
            try:
                enviada = bool(com.al_crear_orden(ref))
            except Exception as e:  # noqa: BLE001
                with st.eng.begin() as con: st.log(con, None, "orden_producto_correo_error", f"{ref} {str(e)[:150]}")
        return jsonify(ref=ref, correo=enviada), 201

    @app.post("/admin/v1/productos/ordenes/<ref>/pagar")
    def a_pagar_orden_producto(ref):
        d = cuerpo(); r = com.pagar(ref, d, emitir_factura)
        with st.eng.begin() as con: st.log(con, None, "orden_producto_pagada", f"{ref} suscripción #{r['suscripcion_id']} {r['suscripcion']} por {d.get('autor', '')}")
        return jsonify(r)

    @app.post("/admin/v1/productos/ordenes/<ref>/facturar")
    def a_facturar_orden_producto(ref):
        return jsonify(factura=com.facturar(ref, emitir_factura))

    @app.post("/admin/v1/productos/ordenes/<ref>/anular")
    def a_anular_orden_producto(ref):
        d = cuerpo(); com.anular(ref, d.get("motivo"))
        return jsonify(ok=True)

    # ---------------------------------------------------------------- API de los sistemas (p. ej. la consola de Surtia)
    def sistema_autenticado(pid):
        return autenticar_sistema(st, secretos_sync, pid)

    @app.post("/sync/v1/<pid>/clientes")
    def s_cliente(pid):
        if pid not in com.cfg or not sistema_autenticado(pid): return jsonify(message="No autorizado"), 401
        d = cuerpo(); cli, creado = com.guardar_cliente(pid, d, origen="sistema")
        with st.eng.begin() as con: st.log(con, None, "cliente_producto_sincronizado", f"{pid}/{cli['referencia']} {'nuevo' if creado else 'actualizado'}")
        return jsonify(cliente={k: cli[k] for k in ("id", "referencia", "nombre", "estado")}, creado=creado), 201 if creado else 200

    @app.get("/sync/v1/<pid>/licencia")
    def s_licencia(pid):
        if pid not in com.cfg or not sistema_autenticado(pid): return jsonify(message="No autorizado"), 401
        ref = referencia_valida(com.cfg[pid]["tipo_sujeto"], request.args.get("referencia"))
        vigente = next((s for s in st.listar_suscripciones(pid)
                        if s["referencia_sujeto"] == ref and s["estado"] == "activa" and s["vence_en"] > st.reloj()), None)
        if vigente is None: return jsonify(message="Sin suscripción vigente"), 404
        with st.eng.begin() as con: st.log(con, None, "token_suscripcion_sincronizado", f"{pid}/{ref} #{vigente['id']}")
        return jsonify(token=st.token_suscripcion(vigente["id"], clave_privada), vence_en=int(vigente["vence_en"]))
