"""Contabilidad automática de Gigadevs Corp en Contax.

Cada factura que emite la plataforma (licencias de Contax, suscripciones de CriteriSeg, Enlace MIT, Surtia y Quincena VE, y
facturas manuales) se asienta sola en la empresa de Gigadevs con su plan de cuentas oficial:

  Venta    Debe 1.1.2.01 Clientes Nacionales (total)   ·  Haber 4.1.1.01 Ventas SaaS (base + exento)  ·  Haber 2.1.2.01 Débito Fiscal IVA
  Cobro    Debe 1.1.1.03 Bancos en Bs. / 1.1.1.05 Pasarelas (Ávila Pay USD, Zelle, Binance) / 1.1.1.02 Efectivo USD  ·  Haber 1.1.2.01
  Anulada  contra-asiento de la venta y del cobro (la contabilidad no se borra)

Es idempotente: cada asiento queda ligado a su origen en `asientos_origen` (origen `fx`, `fx_cobro`, `fx_anulada`, `fx_cobro_anulado`
+ id de la factura), cuya clave primaria impide duplicar,
así que sincronizar dos veces, o desde dos procesos a la vez, nunca duplica un asiento."""
import logging
import threading
import time
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from .calculos.plan_cuentas import _categoria, _monetaria, tipo_de

log = logging.getLogger(__name__)

CLIENTES, VENTAS_SAAS, DEBITO_IVA = "1.1.2.01", "4.1.1.01", "2.1.2.01"
BANCOS_BS, PASARELAS, EFECTIVO_USD = "1.1.1.03", "1.1.1.05", "1.1.1.02"
NOMBRES_SISTEMA = {"contax": "Contax", "criteriseg": "CriteriSeg", "bot_whatsapp": "Enlace MIT", "surtia": "Surtia",
                   "control_financiero": "Quincena VE"}

# Plan de cuentas definitivo de Gigadevs Corp (código, nombre, es_detalle).
PLAN = (
    ('1', 'ACTIVO', False),
    ('1.1', 'Activo Corriente', False),
    ('1.1.1', 'Efectivo y Equivalentes de Efectivo', False),
    ('1.1.1.01', 'Caja Principal (Bolívares)', True),
    ('1.1.1.02', 'Custodia de Efectivo en Divisas (Caja Fuerte USD)', True),
    ('1.1.1.03', 'Bancos Nacionales - Cuentas en Bolívares', True),
    ('1.1.1.04', 'Bancos Nacionales - Cuentas en Divisas (Convenio Cambiario N° 1)', True),
    ('1.1.1.05', 'Pasarelas de Pago e Ingresos Internacionales (Stripe, PayPal, Binance)', True),
    ('1.1.2', 'Cuentas por Cobrar Comerciales', False),
    ('1.1.2.01', 'Clientes Nacionales (Facturas por Cobrar en Bs./USD)', True),
    ('1.1.2.02', 'Clientes Internacionales (Exportación de Software)', True),
    ('1.1.2.05', 'Crédito Fiscal IVA', True),
    ('1.1.2.06', 'Retenciones de IVA Acumuladas por Compensar (75%)', True),
    ('1.1.2.09', '(-) Provisión para Cuentas Incobrables', True),
    ('1.1.3', 'Inventarios (Mercaderías)', False),
    ('1.1.3.01', 'Inventario de Partes y Piezas de Computación', True),
    ('1.1.3.02', 'Inventario de Accesorios Electrónicos', True),
    ('1.1.3.03', 'Inventario de Equipos Tecnológicos Terminados', True),
    ('1.1.4', 'Gastos Pagados por Anticipado / Activos Diferidos', False),
    ('1.1.4.01', 'Inventario de Números de Control Digitales (Folios Precomprados)', True),
    ('1.1.4.02', 'Talonarios Manuales de Contingencia (SENIAT)', True),
    ('1.1.4.03', 'Seguros Pagados por Anticipado', True),
    ('1.2', 'Activo No Corriente', False),
    ('1.2.1', 'Propiedad, Planta y Equipo', False),
    ('1.2.1.01', 'Equipos de Computación y Desarrollo (Laptops, Servidores)', True),
    ('1.2.1.02', 'Mobiliario y Equipos de Oficina', True),
    ('1.2.1.09', '(-) Depreciación Acumulada', True),
    ('1.2.2', 'Activos Intangibles', False),
    ('1.2.2.01', 'Plataforma Software Propia (Costos de Desarrollo Capitalizados)', True),
    ('1.2.2.02', 'Licencias de Software y Herramientas de Desarrollo', True),
    ('2', 'PASIVO', False),
    ('2.1', 'Pasivo Corriente', False),
    ('2.1.1', 'Cuentas por Pagar Comerciales', False),
    ('2.1.1.01', 'Proveedores de Hardware y Accesorios (Nacionales)', True),
    ('2.1.1.02', 'Proveedores de Componentes (Internacionales / Importaciones)', True),
    ('2.1.1.03', 'Proveedores de Infraestructura Cloud (AWS, Azure, DigitalOcean)', True),
    ('2.1.2', 'Obligaciones Tributarias por Pagar (SENIAT / Municipales)', False),
    ('2.1.2.01', 'Débito Fiscal IVA (16%)', True),
    ('2.1.2.02', 'Retenciones de IVA Recibidas (Sujetos Pasivos Especiales)', True),
    ('2.1.2.03', 'Retenciones de ISLR por Pagar', True),
    ('2.1.2.04', 'IGTF Percibido por Enterar (3% Divisas en Efectivo)', True),
    ('2.1.2.05', 'Impuestos Municipales por Pagar (Actividad Económica)', True),
    ('2.1.3', 'Pasivos Diferidos (Ingresos No Devengados)', False),
    ('2.1.3.01', 'Anticipos de Clientes - Proyectos en Desarrollo (A la medida/Web)', True),
    ('2.1.3.02', 'Suscripciones SaaS Cobradas por Anticipado (Planes Anuales)', True),
    ('2.2', 'Pasivo No Corriente', False),
    ('2.2.1', 'Obligaciones Financieras a Largo Plazo', True),
    ('3', 'PATRIMONIO', False),
    ('3.1', 'Capital Social (Actualizado según VEN-NIF)', True),
    ('3.2', 'Reserva Legal', True),
    ('3.3', 'Resultados Acumulados de Ejercicios Anteriores', True),
    ('3.4', 'Resultado del Ejercicio Actual', True),
    ('4', 'INGRESOS', False),
    ('4.1', 'Ingresos por Servicios Tecnológicos', False),
    ('4.1.1', 'Ingresos por Suscripciones SaaS (Línea Recurrente)', False),
    ('4.1.1.01', 'Ventas SaaS - Clientes Nacionales', True),
    ('4.1.1.02', 'Ventas SaaS - Clientes Internacionales (Exportación)', True),
    ('4.1.2', 'Ingresos por Proyectos Cerrados y Diseño', False),
    ('4.1.2.01', 'Desarrollo de Software y Aplicaciones a Medida (Por Hitos)', True),
    ('4.1.2.02', 'Diseño y Desarrollo de Páginas Web y E-commerce', True),
    ('4.1.2.03', 'Contratos de Soporte Técnico y Mantenimiento', True),
    ('4.2', 'Ingresos por Comercialización (Hardware)', False),
    ('4.2.1', 'Ventas de Partes y Piezas Electrónicas', True),
    ('4.2.2', 'Ventas de Accesorios Computacionales', True),
    ('4.3', 'Ingresos No Operativos', False),
    ('4.3.1.01', 'Ganancia por Diferencial Cambiario (Ajustes Tasa Oficial BCV)', True),
    ('5', 'COSTOS', False),
    ('5.1', 'Costo de Servicios Tecnológicos (SaaS y Proyectos)', False),
    ('5.1.1', 'Mano de Obra Directa (Nómina/Honorarios Programadores, Diseñadores)', True),
    ('5.1.2', 'Infraestructura Cloud Directa (Hosting Producción, Cloud DB)', True),
    ('5.1.3', 'Almacenamiento y Respaldo Fiscal Cloud (Obligatorio 10 años)', True),
    ('5.1.4', 'APIs y Licencias de Terceros integradas en desarrollos', True),
    ('5.2', 'Costo de Ventas - Comercialización', False),
    ('5.2.1', 'Costo de Venta - Partes y Piezas', True),
    ('5.2.2', 'Costo de Venta - Accesorios', True),
    ('5.2.3', 'Gastos de Importación, Aranceles y Fletes Nacionales', True),
    ('6', 'GASTOS OPERATIVOS', False),
    ('6.1', 'Gastos de Administración', False),
    ('6.1.1', 'Sueldos del Personal Administrativo y Directivo', True),
    ('6.1.2', 'Gasto de Proveedor Tecnológico / Imprenta Digital (Facturación Electrónica)', True),
    ('6.1.3', 'Licencias de Software Interno (CRM, ERP, Contabilidad)', True),
    ('6.1.4', 'Depreciaciones y Amortizaciones', True),
    ('6.2', 'Gastos de Ventas y Mercadeo', False),
    ('6.2.1', 'Publicidad Digital (Google Ads, Meta Ads, LinkedIn)', True),
    ('6.2.2', 'Comisiones sobre Ventas', True),
    ('6.3', 'Gastos Financieros y No Operativos', False),
    ('6.3.1.01', 'Comisiones Bancarias y de Pasarelas de Pago', True),
    ('6.3.1.02', 'Pérdida por Diferencial Cambiario (Ajustes Tasa Oficial BCV)', True),
)


class ErrorContabilidadGigadevs(ValueError):
    pass


def empresa_de(org):
    """La empresa de Gigadevs dentro de su organización: la que tiene el RIF de la organización, o la primera."""
    from .extensions import db
    from .models import Empresa
    es = db.session.execute(select(Empresa).where(Empresa.organizacion_id == org.id).order_by(Empresa.id)).scalars().all()
    return next((e for e in es if (e.rif or "").upper() == (org.rif or "").upper()), es[0] if es else None)


def cargar_plan(empresa) -> int:
    """Agrega las cuentas del plan que falten (no toca las existentes). Devuelve cuántas creó."""
    from .extensions import db
    from .models import Cuenta
    existentes = set(db.session.execute(select(Cuenta.codigo).where(Cuenta.empresa_id == empresa.id)).scalars())
    nuevas = 0
    for codigo, nombre, _detalle in PLAN:
        if codigo in existentes: continue
        tipo = tipo_de(None, codigo)
        db.session.add(Cuenta(empresa_id=empresa.id, codigo=codigo, nombre=nombre, tipo=tipo,
                              categoria_niif18=_categoria(None, nombre, tipo), es_partida_monetaria=_monetaria(None, nombre)))
        nuevas += 1
    if nuevas: db.session.flush()
    return nuevas


def _d(v) -> Decimal:
    return Decimal(str(v or 0)).quantize(Decimal("0.01"))


def cuenta_cobro(f) -> str:
    fp = (f.get("forma_pago") or "").lower()
    if "efectivo" in fp and f.get("moneda") != "VES": return EFECTIVO_USD
    if f.get("moneda") != "VES" or any(k in fp for k in ("binance", "zelle", "paypal", "stripe", "usdt", "usd")): return PASARELAS
    return BANCOS_BS


def asientos_de(f) -> list[tuple[str, str, str, list]]:
    """(origen, fecha, concepto, líneas) que corresponden a una factura de la plataforma. Montos en bolívares."""
    num, cli = f.get("numero_completo") or f["numero"], f"{f['cliente_nombre']} ({f['cliente_rif']})"
    sis = NOMBRES_SISTEMA.get(f.get("sistema") or "contax", f.get("sistema") or "Contax")
    total, iva = _d(f["total"]), _d(f["iva"])
    venta = [(CLIENTES, total, 0), (VENTAS_SAAS, 0, total - iva)] + ([(DEBITO_IVA, 0, iva)] if iva > 0 else [])
    if total <= 0: return []
    fecha = f["fecha"]
    r = [(("fx", f["id"]), fecha, f"Factura {num} · {sis} · {cli}", venta)]
    pagada = bool((f.get("forma_pago") or "").strip()) or bool(f.get("orden_ref"))
    if pagada:
        r.append((("fx_cobro", f["id"]), fecha, f"Cobro factura {num} · {f.get('forma_pago') or 'pago en línea'} · {cli}",
                  [(cuenta_cobro(f), total, 0), (CLIENTES, 0, total)]))
    if f.get("estado") == "Anulada":
        anul = max(fecha, time.strftime("%Y-%m-%d", time.localtime(f["anulada_en"])) if f.get("anulada_en") else fecha)
        motivo = (f.get("motivo_anulacion") or "")[:120]
        r.append((("fx_anulada", f["id"]), anul, f"Anulación factura {num} · {motivo}", [(c, h, d) for c, d, h in venta]))
        if pagada:
            r.append((("fx_cobro_anulado", f["id"]), anul, f"Reverso cobro factura {num} · {motivo}", [(CLIENTES, total, 0), (cuenta_cobro(f), 0, total)]))
    return r


def sincronizar(facturas, usuario, org) -> dict:
    """Asienta las facturas que falten. `facturas`: lista de dicts como los devuelve /admin/v1/facturas."""
    from .contabilidad.service import crear_asiento
    from .contabilidad.validadores import ErrorAsiento
    from .extensions import db
    from .models_compras import AsientoOrigen
    empresa = empresa_de(org)
    if empresa is None: raise ErrorContabilidadGigadevs(f"{org.nombre} aún no tiene una empresa creada en Contax")
    plan = cargar_plan(empresa)
    hechos = {(o, int(i)) for o, i in db.session.execute(select(AsientoOrigen.origen, AsientoOrigen.origen_id).where(
        AsientoOrigen.empresa_id == empresa.id, AsientoOrigen.origen.like("fx%")))}
    nuevos, errores = 0, []
    for f in sorted(facturas, key=lambda x: x["id"]):
        for origen, fecha, concepto, lineas in asientos_de(f):
            if origen in hechos: continue
            try:
                with db.session.begin_nested():
                    aid = crear_asiento(empresa.id, usuario, fecha, concepto[:500], lineas, commit=False)
                    db.session.add(AsientoOrigen(empresa_id=empresa.id, origen=origen[0], origen_id=origen[1], asiento_id=aid))
                nuevos += 1; hechos.add(origen)
            except IntegrityError:
                hechos.add(origen)        # otro proceso lo asentó a la vez
            except ErrorAsiento as e:
                errores.append(f"factura {origen[1]} ({origen[0]}): {e}")
    db.session.commit()
    return {"empresa": empresa.razon_social, "cuentas_creadas": plan, "asientos": nuevos, "errores": errores}


def sincronizar_ahora(app) -> dict | None:
    """Corre una sincronización completa fuera de una petición (hilo de fondo). Un candado de PostgreSQL evita que dos procesos
    trabajen a la vez. Devuelve None si no hay nada configurado."""
    from flask import g
    from .extensions import db
    from .rls import fijar_org
    from .web_plataforma import _usuario_gigadevs
    fab = app.extensions.get("admin_licencias")
    if fab is None or not app.config.get("GIGADEVS_USUARIO"): return None
    with app.test_request_context("/_contabilidad_gigadevs"):
        g.org_rls = None
        try:
            if db.engine.dialect.name == "postgresql" and not db.session.execute(text("SELECT pg_try_advisory_xact_lock(726170)")).scalar():
                return None
            u, org = _usuario_gigadevs()
            if u is None or org is None: return None
            facturas = fab().facturas()
            fijar_org(db, org.id)
            r = sincronizar(facturas, u, org)
            from . import drive
            try: r["drive"] = drive.subir_facturas(facturas)[0]
            except drive.ErrorDrive as e: log.warning("Drive: %s", e)
            return r
        except Exception:  # noqa: BLE001 — el hilo de fondo nunca debe caer
            db.session.rollback(); log.exception("contabilidad_gigadevs: sincronización fallida"); return None
        finally:
            db.session.remove()


def iniciar_en_fondo(app, minutos):
    """Sincroniza cada `minutos` minutos mientras el proceso viva."""
    if minutos <= 0: return None

    def ciclo():
        time.sleep(30)
        while True:
            r = sincronizar_ahora(app)
            if r and (r["asientos"] or r["errores"]): log.info("contabilidad_gigadevs: %s", r)
            time.sleep(minutos * 60)
    t = threading.Thread(target=ciclo, name="contabilidad-gigadevs", daemon=True)
    t.start()
    return t
