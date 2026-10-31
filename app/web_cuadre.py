"""Contabilidad → Cuadre de libros. Compara lo que dicen los módulos (libro de ventas, libro de compras, kardex, cuentas
por cobrar y por pagar) con lo que dice la contabilidad, lista lo que falta contabilizar (facturas, compras, órdenes de
compra recibidas sin factura del proveedor, saldos iniciales migrados) y lo contabiliza con un clic, usando las cuentas
de «Configuración y cierres». Así el contador lleva todo al diario sin cargar asiento por asiento."""
from datetime import date, timedelta
from decimal import Decimal

from flask import g, redirect, request, url_for
from sqlalchemy import func, select

from . import servicios as S
from . import servicios_fact as SF
from .auditoria import registrar
from .calculos import cartera
from .calculos import facturacion as CF
from .contabilidad import service as contab
from .extensions import db
from .models import Asiento, Cuenta, Movimiento
from .models2 import ConfigAsientos, DocumentoCxC, DocumentoCxP, MovInventario
from .models_compras import AsientoOrigen, OrdenCompra
from .models_fact import DocumentoVenta
from .security import tiene_permiso
from .servicios_migracion import REF_SALDO_INICIAL
from .tiempo import hoy_ve
from .web import F, campo, hoy, intentar, pagina, ruta

Z = Decimal(0)
h = lambda v: campo("accion", "", "hidden", valor=v)


def _cfg(eid) -> dict:
    c = db.session.get(ConfigAsientos, eid)
    return c.como_dict() if c else {}


def _mov(eid, cuenta, desde=None, hasta=None) -> Decimal:
    """Debe − haber de una cuenta (en el período o acumulado hasta una fecha)."""
    if not cuenta: return None
    q = select(func.coalesce(func.sum(Movimiento.debe - Movimiento.haber), 0)).join(Asiento, Asiento.id == Movimiento.asiento_id) \
        .where(Movimiento.empresa_id == eid, Movimiento.cuenta_codigo == cuenta)
    if desde: q = q.where(Asiento.fecha >= desde)
    if hasta: q = q.where(Asiento.fecha <= hasta)
    return Decimal(db.session.execute(q).scalar())


def _ligados(eid, origen) -> set:
    return set(db.session.execute(select(AsientoOrigen.origen_id).where(AsientoOrigen.empresa_id == eid, AsientoOrigen.origen == origen)).scalars())


def pendientes(eid, ini, fin):
    ventas = [d for d in db.session.execute(select(DocumentoVenta).where(DocumentoVenta.empresa_id == eid, DocumentoVenta.estado == "Emitido",
                                                                         DocumentoVenta.asiento_id.is_(None), DocumentoVenta.fecha_emision <= fin)
                                            .order_by(DocumentoVenta.fecha_emision)).scalars() if d.id not in _ligados(eid, "venta")]
    compras = db.session.execute(select(DocumentoCxP).where(DocumentoCxP.empresa_id == eid, DocumentoCxP.asiento_id.is_(None), DocumentoCxP.origen != "migracion",
                                                            DocumentoCxP.fecha_emision <= fin).order_by(DocumentoCxP.fecha_emision)).scalars().all()
    de_facturas = {x for x in db.session.execute(select(DocumentoVenta.cxc_id).where(DocumentoVenta.empresa_id == eid, DocumentoVenta.cxc_id.is_not(None))).scalars()}
    cxc = [d for d in db.session.execute(select(DocumentoCxC).where(DocumentoCxC.empresa_id == eid, DocumentoCxC.asiento_id.is_(None), DocumentoCxC.origen != "migracion",
                                                                     DocumentoCxC.fecha_emision <= fin)).scalars() if d.id not in de_facturas]
    oc_sin_factura = db.session.execute(select(OrdenCompra).where(OrdenCompra.empresa_id == eid, OrdenCompra.estado == "Recibida", OrdenCompra.cxp_id.is_(None))).scalars().all()
    return ventas, compras, cxc, oc_sin_factura


def saldos_migrados(eid):
    ya = _ligados(eid, "apertura_inv")
    movs = [m for m in db.session.execute(select(MovInventario).where(MovInventario.empresa_id == eid, MovInventario.referencia == REF_SALDO_INICIAL)).scalars() if m.id not in ya]
    cxc = db.session.execute(select(DocumentoCxC).where(DocumentoCxC.empresa_id == eid, DocumentoCxC.origen == "migracion", DocumentoCxC.asiento_id.is_(None))).scalars().all()
    cxp = db.session.execute(select(DocumentoCxP).where(DocumentoCxP.empresa_id == eid, DocumentoCxP.origen == "migracion", DocumentoCxP.asiento_id.is_(None))).scalars().all()
    return movs, cxc, cxp


def contabilizar_pendientes(eid, u, ini, fin) -> int:
    """Genera el asiento de cada documento pendiente con las cuentas configuradas. Falla si falta alguna cuenta."""
    cfg = _cfg(eid)
    ventas, compras, cxc, _ = pendientes(eid, ini, fin)
    n = 0
    for d in ventas:
        iva, base = d.iva_g + d.iva_r + d.iva_a, d.exento + d.base_g + d.base_r + d.base_a
        faltan = [k for k in CF.claves_asiento(d.condicion, iva, d.igtf) if not cfg.get(f"cuenta_{k}")]
        if faltan: raise ValueError("Configure las cuentas: " + ", ".join(faltan))
        aid = contab.crear_asiento(eid, u, d.fecha_emision, f"{d.tipo} {SF.numero_completo(d)} — {d.cliente_nombre} (cuadre)",
                                   CF.asiento_documento(cfg, d.tipo, d.condicion, base, iva, d.igtf), commit=False)
        db.session.add(AsientoOrigen(empresa_id=eid, origen="venta", origen_id=d.id, asiento_id=aid)); n += 1
    for d in compras:
        if not (cfg.get("cuenta_cxp") and cfg.get("cuenta_compras_gastos")): raise ValueError("Configure las cuentas de CxP y de compras/gastos")
        lineas = cartera.asiento_compra(cfg, d.monto, d.base_imponible + d.exento, d.monto_iva)
        if d.tipo_documento == "NC": lineas = [(c, hb, db_) for c, db_, hb in lineas]
        d.asiento_id = contab.crear_asiento(eid, u, d.fecha_emision, f"Compra {d.tipo_documento} {d.numero_documento} — {d.proveedor_nombre} (cuadre)", lineas, commit=False); n += 1
    for d in cxc:
        if not (cfg.get("cuenta_cxc") and cfg.get("cuenta_ventas")): raise ValueError("Configure las cuentas de CxC y de ventas")
        d.asiento_id = contab.crear_asiento(eid, u, d.fecha_emision, f"Venta a crédito Doc. {d.numero_documento} (cuadre)", cartera.asiento_venta(cfg, d.monto, d.monto, 0), commit=False); n += 1
    registrar(u.username, "CUADRE_CONTABILIZADO", f"empresa={eid} documentos={n}")
    db.session.commit()
    return n


def asiento_apertura(eid, u, fecha, contrapartida) -> int:
    """Un solo asiento de apertura con los saldos iniciales migrados: inventario y CxC al debe, CxP al haber, la diferencia
    contra la cuenta de patrimonio elegida."""
    cfg = _cfg(eid)
    movs, cxc, cxp = saldos_migrados(eid)
    inv = sum((m.cantidad * m.costo_unitario for m in movs), Z).quantize(Decimal("0.01"))
    tc, tp = sum((d.saldo_pendiente for d in cxc), Z), sum((d.saldo_pendiente for d in cxp), Z)
    if not (inv or tc or tp): raise ValueError("No hay saldos migrados pendientes de apertura")
    lineas = []
    for monto, clave, lado in ((inv, "inventario", "d"), (tc, "cxc", "d"), (tp, "cxp", "h")):
        if not monto: continue
        if not cfg.get(f"cuenta_{clave}"): raise ValueError(f"Configure la cuenta de {clave} en Configuración y cierres")
        lineas.append((cfg[f"cuenta_{clave}"], monto, 0) if lado == "d" else (cfg[f"cuenta_{clave}"], 0, monto))
    dif = inv + tc - tp
    if dif: lineas.append((contrapartida, 0, dif) if dif > 0 else (contrapartida, -dif, 0))
    aid = contab.crear_asiento(eid, u, fecha, "Asiento de apertura — saldos migrados de otro sistema", lineas, commit=False)
    for m in movs: db.session.add(AsientoOrigen(empresa_id=eid, origen="apertura_inv", origen_id=m.id, asiento_id=aid))
    for d in [*cxc, *cxp]: d.asiento_id = aid
    registrar(u.username, "ASIENTO_APERTURA_MIGRACION", f"empresa={eid} asiento={aid} inv={inv} cxc={tc} cxp={tp}")
    db.session.commit()
    return aid


def ajuste_inventario(eid, u, fecha, contrapartida) -> int:
    cfg = _cfg(eid)
    if not cfg.get("cuenta_inventario"): raise ValueError("Configure la cuenta de inventario")
    kardex, _ = S.valor_inventario(eid)
    dif = (Decimal(kardex) - _mov(eid, cfg["cuenta_inventario"], hasta=hoy_ve())).quantize(Decimal("0.01"))
    if not dif: raise ValueError("El inventario ya cuadra con la contabilidad")
    c = cfg["cuenta_inventario"]
    lineas = [(c, dif, 0), (contrapartida, 0, dif)] if dif > 0 else [(contrapartida, -dif, 0), (c, 0, -dif)]
    aid = contab.crear_asiento(eid, u, fecha, "Ajuste de inventario para cuadrar con el kardex", lineas, commit=False)
    registrar(u.username, "AJUSTE_INVENTARIO_CUADRE", f"empresa={eid} diferencia={dif}"); db.session.commit()
    return aid


@ruta("/e/<int:eid>/cuadre", "reportes.ver", ("GET", "POST"))
def cuadre(eid):
    periodo = request.values.get("periodo") or hoy()[:7]
    try: ini = date(int(periodo[:4]), int(periodo[5:7]), 1)
    except ValueError: ini = hoy_ve().replace(day=1); periodo = ini.strftime("%Y-%m")
    fin = date(ini.year + (ini.month == 12), ini.month % 12 + 1, 1) - timedelta(days=1)
    if request.method == "POST":
        if not tiene_permiso(g.usuario, "asientos.crear"): return ("", 403)
        a = F.get("accion")
        cuentas = set(db.session.execute(select(Cuenta.codigo).where(Cuenta.empresa_id == eid)).scalars())
        def contra():
            c = (F.get("contrapartida") or "").strip()
            if c not in cuentas: raise ValueError("Elija una cuenta de contrapartida válida")
            return c
        if a == "contabilizar": intentar(lambda: contabilizar_pendientes(eid, g.usuario, ini, fin), lambda n: f"{n} documento(s) contabilizado(s)")
        elif a == "apertura": intentar(lambda: asiento_apertura(eid, g.usuario, F.get("fecha") or hoy(), contra()), lambda a_: f"Asiento de apertura N°{a_} registrado")
        elif a == "ajuste_inv": intentar(lambda: ajuste_inventario(eid, g.usuario, F.get("fecha") or hoy(), contra()), lambda a_: f"Asiento de ajuste N°{a_} registrado")
        return redirect(url_for("web.cuadre", eid=eid, periodo=periodo))
    cfg = _cfg(eid)
    _, tv = SF.libro_ventas(eid, periodo)
    _, tc = S.libro_compras(eid, periodo)
    kardex, _ = S.valor_inventario(eid)
    hoy_d = hoy_ve()
    saldo_cxc = db.session.execute(select(func.coalesce(func.sum(DocumentoCxC.saldo_pendiente), 0)).where(DocumentoCxC.empresa_id == eid)).scalar()
    saldo_cxp = db.session.execute(select(func.coalesce(func.sum(DocumentoCxP.saldo_pendiente), 0)).where(DocumentoCxP.empresa_id == eid)).scalar()
    neg = lambda v: None if v is None else -v
    filas = [("IVA débito fiscal del mes", "Libro de ventas", tv["iva_g"] + tv["iva_r"] + tv["iva_a"], neg(_mov(eid, cfg.get("cuenta_iva_debito_fiscal"), ini, fin)), "iva_debito_fiscal"),
             ("Ventas gravadas y exentas del mes (base)", "Libro de ventas", tv["exento"] + tv["base_g"] + tv["base_r"] + tv["base_a"], neg(_mov(eid, cfg.get("cuenta_ventas"), ini, fin)), "ventas"),
             ("IVA crédito fiscal del mes", "Libro de compras", tc["iva"], _mov(eid, cfg.get("cuenta_iva_credito_fiscal"), ini, fin), "iva_credito_fiscal"),
             ("Inventario a hoy", "Kardex (inventario)", Decimal(kardex), _mov(eid, cfg.get("cuenta_inventario"), hasta=hoy_d), "inventario"),
             ("Cuentas por cobrar a hoy", "Saldos de CxC", Decimal(saldo_cxc), _mov(eid, cfg.get("cuenta_cxc"), hasta=hoy_d), "cxc"),
             ("Cuentas por pagar a hoy", "Saldos de CxP", Decimal(saldo_cxp), neg(_mov(eid, cfg.get("cuenta_cxp"), hasta=hoy_d)), "cxp")]
    tabla = []
    for concepto, fuente, modulo, conta, clave in filas:
        if conta is None: est, dif = f"Configure la cuenta «{clave.replace('_', ' ')}»", ""
        else:
            dif = (Decimal(modulo) - conta).quantize(Decimal("0.01"))
            est = "✔ Cuadra" if not dif else "✘ Diferencia"
        tabla.append(dict(cells=[concepto, fuente, Decimal(modulo).quantize(Decimal("0.01")), "—" if conta is None else conta.quantize(Decimal("0.01")), dif, est],
                          clase="" if est == "✔ Cuadra" else "anulado"))
    ventas, compras, cxc, ocs = pendientes(eid, ini, fin)
    movs, mcxc, mcxp = saldos_migrados(eid)
    cods = [(c.codigo, f"{c.codigo} {c.nombre}") for c in db.session.execute(select(Cuenta).where(Cuenta.empresa_id == eid).order_by(Cuenta.codigo)).scalars()]
    puede = tiene_permiso(g.usuario, "asientos.crear")
    secs = [dict(titulo="Período", form=dict(action=request.path, metodo="get", boton="Ver", campos=[campo("periodo", "Mes", "month", valor=periodo)])),
            dict(kpis=[("Facturas sin asiento", len(ventas)), ("Compras sin asiento", len(compras) + len(cxc)), ("OC recibidas sin factura", len(ocs)),
                       ("Saldos migrados sin apertura", len(movs) + len(mcxc) + len(mcxp))]),
            dict(titulo=f"Cuadre {periodo}: módulos contra contabilidad", nota="Un ✘ indica que hay documentos sin contabilizar o asientos manuales sobre esas cuentas. "
                 "Contabilice los pendientes de abajo y vuelva a revisar.",
                 tabla=dict(cols=["Concepto", "Según", "Módulo", "Contabilidad", "Diferencia", "Estado"], num=[2, 3, 4], filas=tabla))]
    doc = [dict(cells=["Factura de venta", d.fecha_emision, f"{d.tipo} {SF.numero_completo(d)}", d.cliente_nombre, d.total]) for d in ventas]
    doc += [dict(cells=["Compra (CxP)", d.fecha_emision, d.numero_documento, d.proveedor_nombre, d.monto]) for d in compras]
    doc += [dict(cells=["Venta a crédito (CxC)", d.fecha_emision, d.numero_documento, d.deudor_nombre, d.monto]) for d in cxc]
    secs.append(dict(titulo="Documentos pendientes de contabilizar", tabla=dict(cols=["Tipo", "Fecha", "Documento", "Tercero", "Monto"], num=[4], filas=doc)))
    if doc and puede:
        secs.append(dict(titulo=f"Contabilizar los {len(doc)} documento(s) pendientes", abierto=True, nota="Cada documento genera su asiento con las cuentas de «Configuración y cierres», en su fecha.",
                         form=dict(action=request.path, boton="Contabilizar ahora", campos=[h("contabilizar"), campo("periodo", "", "hidden", valor=periodo)])))
    if ocs:
        secs.append(dict(titulo="Órdenes de compra recibidas sin la factura del proveedor", nota="Registre la factura del proveedor (N° y control) desde la orden: entra al libro de compras y a CxP.",
                         tabla=dict(cols=["Orden", "Fecha", "Proveedor", "Total Bs"], num=[3], filas=[dict(cells=[f"OC-{o.numero:06d}", o.fecha, o.dest_nombre, o.total_bs],
                                                                                                       url=url_for("web.orden_compra_ver", eid=eid, oid=o.id)) for o in ocs])))
    if (movs or mcxc or mcxp):
        inv = sum((m.cantidad * m.costo_unitario for m in movs), Z).quantize(Decimal("0.01"))
        secs.append(dict(titulo="Saldos iniciales migrados (sin asiento de apertura)", pares=[("Inventario inicial", inv), ("Cuentas por cobrar", sum((d.saldo_pendiente for d in mcxc), Z)),
                                                                                             ("Cuentas por pagar", sum((d.saldo_pendiente for d in mcxp), Z))]))
        if puede:
            secs.append(dict(titulo="Registrar el asiento de apertura", abierto=True, nota="Inventario y CxC al debe, CxP al haber; la diferencia va a la cuenta de patrimonio que elija (p. ej. Capital o Resultados acumulados).",
                             form=dict(action=request.path, boton="Registrar apertura", campos=[h("apertura"), campo("periodo", "", "hidden", valor=periodo),
                                       campo("fecha", "Fecha de apertura", "date", valor=hoy()), campo("contrapartida", "Cuenta de patrimonio", "select", opciones=cods)])))
    inv_fila = tabla[3]["cells"]
    if puede and inv_fila[4] not in ("", Decimal("0.00")) and not movs:
        secs.append(dict(titulo="Ajustar inventario a lo que dice el kardex", nota=f"Diferencia: Bs {inv_fila[4]}. Genera un asiento contra la cuenta que elija (costo de ventas, faltantes y sobrantes…).",
                         form=dict(action=request.path, boton="Registrar ajuste", campos=[h("ajuste_inv"), campo("periodo", "", "hidden", valor=periodo),
                                   campo("fecha", "Fecha", "date", valor=hoy()), campo("contrapartida", "Cuenta de contrapartida", "select", opciones=cods)])))
    return pagina("Cuadre de libros", eid, secs, acciones=[dict(url=url_for("web.libro_ventas", eid=eid, periodo=periodo), texto="Libro de ventas"),
                                                            dict(url=url_for("web.libro_compras", eid=eid, periodo=periodo), texto="Libro de compras")])
