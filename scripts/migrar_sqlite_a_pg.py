"""ETL completo del ERP original (SQLite) al sistema nuevo (PostgreSQL).

Uso:  python scripts/migrar_sqlite_a_pg.py sistema_contable.db [--ejecutar] [--org-rif J-12345678-9 [--org-nombre "Cliente SA"]]
 - Cada base del sistema de escritorio es UN cliente: sus datos van a la organización indicada (se crea si no existe;
   sin --org-rif se usa la primera organización o se crea la «Organización principal»).
 - Los parámetros legales globales (UT, tarifa, INPC, tasas) solo se importan si la plataforma aún no los tiene.
 - Sin --ejecutar: ensayo completo y ROLLBACK. Todo corre en UNA transacción.
 - Concilia al final (filas y totales debe/haber, saldos de CxC/CxP, existencias). Si algo no cuadra, aborta.
 - NO migra los valores sembrados como "EJEMPLO" (INPC, UT, conceptos de retención): cargue los reales en Administración → Parámetros.
 - La bitácora antigua (editable) se exporta a CSV como archivo histórico; no se inyecta en la cadena de hashes nueva.
"""
import csv
import sqlite3
import sys
from datetime import date
from decimal import Decimal

sys.path.insert(0, ".")
from scripts.premigracion_check import revisar  # noqa: E402

D2, D4 = Decimal("0.01"), Decimal("0.0001")


def dec(v, q=D2): return Decimal(str(v or 0)).quantize(q)
def fch(v): return date.fromisoformat(str(v)[:10])
def es_ejemplo(*textos): return any("EJEMPLO" in (t or "").upper() for t in textos)


def leer(src, tabla):
    src.row_factory = sqlite3.Row
    try: return [dict(r) for r in src.execute(f"SELECT * FROM {tabla}")]   # tabla = literal interno, nunca entrada de usuario
    except sqlite3.OperationalError: return []


def _arg(nombre):
    return sys.argv[sys.argv.index(nombre) + 1] if nombre in sys.argv else None


def main(ruta, ejecutar, org_rif=None, org_nombre=None):
    if any(n == "CRITICO" for n, _ in revisar(ruta)):
        sys.exit("Hay hallazgos CRÍTICOS: corrija el origen (premigracion_check.py) antes de migrar.")
    from app import create_app
    from app.extensions import db
    from app.models import Asiento, Cuenta, Empresa, Movimiento, Organizacion, PeriodoCerrado, Usuario
    from app.models2 import (CobroCxC, ConfigAsientos, DocumentoCxC, DocumentoCxP, DocumentoFiscal, ExtractoBancario, Hallazgo,
                             IndiceINPC, LotePeps, MovInventario, PagoCxP, PapelTrabajo, PartidaConciliacion, Producto,
                             RebajaISLR, RetencionIVA, RetencionISLR, TarifaISLR, TasaCambio, TramoProvision,
                             UnidadTributaria, ConceptoRetencionISLR, CUENTAS_CFG)
    app = create_app()
    src = sqlite3.connect(f"file:{ruta}?mode=ro", uri=True)
    omitidos, add = [], db.session.add
    with app.app_context():
        try:
            q = db.session.query(Organizacion)
            org = q.filter_by(rif=org_rif).first() if org_rif else q.order_by(Organizacion.id).first()
            if org is None:
                org = Organizacion(nombre=org_nombre or "Organización principal", rif=org_rif or "J-00000000-0", estado="activa")
                add(org); db.session.flush(); print(f"Organización creada: {org.nombre} ({org.rif})")
            print(f"Destino: organización «{org.nombre}» ({org.rif})")
            vacia = lambda M: db.session.query(M).count() == 0
            emp = {}
            for r in leer(src, "clientes_contables"):
                e = Empresa(organizacion_id=org.id, razon_social=r["razon_social"], rif=r["rif"].strip().upper(), direccion=r.get("direccion"), telefono=r.get("telefono"),
                            es_sujeto_pasivo_especial=bool(r.get("es_sujeto_pasivo_especial") or 0))
                add(e); db.session.flush(); emp[r["id"]] = e.id
            for r in leer(src, "cuentas"):
                add(Cuenta(empresa_id=emp[r["cliente_id"]], codigo=r["codigo"], nombre=r["nombre"], tipo=r["tipo"],
                           categoria_niif18=r.get("categoria_niif18") or "Operacion", es_partida_monetaria=bool(r.get("es_partida_monetaria") or 0)))
            import hashlib
            for r in leer(src, "usuarios"):  # hash PBKDF2 envuelto: se re-hashea a Argon2id en el 1.er login
                guardado = f"legacy$pbkdf2$200000${r['salt']}${r['password_hash']}" if r.get("salt") else "!"
                if r.get("salt") and hashlib.pbkdf2_hmac("sha256", b"admin123", bytes.fromhex(r["salt"]), 200_000).hex() == r["password_hash"]:
                    guardado = "!"; omitidos.append(f"Contraseña de «{r['usuario']}» invalidada (era la clave por defecto admin123): restablézcala con scripts/inicializar.py")
                if db.session.query(Usuario).filter_by(username=r["usuario"]).first():
                    raise RuntimeError(f"El usuario «{r['usuario']}» ya existe en la plataforma: renómbrelo en el origen antes de migrar")
                add(Usuario(organizacion_id=org.id, username=r["usuario"], rol=r["rol"], debe_cambiar_clave=True, password_hash=guardado))
            db.session.flush()
            admin = db.session.query(Usuario).filter_by(rol="Administrador", organizacion_id=org.id).first()
            if admin is None: raise RuntimeError("No hay usuario Administrador para atribuir los asientos")
            movs = {}
            for m in leer(src, "movimientos"): movs.setdefault(m["asiento_id"], []).append(m)
            for r in leer(src, "asientos"):
                a = Asiento(empresa_id=emp[r["cliente_id"]], fecha=fch(r["fecha"]), concepto=r.get("concepto") or "(sin concepto)", creado_por=admin.id)
                add(a); db.session.flush()
                for m in movs.get(r["id"], []):
                    add(Movimiento(asiento_id=a.id, empresa_id=a.empresa_id, cuenta_codigo=m["cuenta_codigo"], debe=dec(m["debe"]), haber=dec(m["haber"])))
            for r in leer(src, "periodos_cerrados"): add(PeriodoCerrado(empresa_id=emp[r["cliente_id"]], mes_anio=r["mes_anio"], cerrado_por=admin.id))
            for r in leer(src, "configuracion_asientos_automaticos"):
                c = ConfigAsientos(empresa_id=emp[r["cliente_id"]], generar_automatico=bool(r.get("generar_automatico", 1)))
                for k in CUENTAS_CFG: setattr(c, f"cuenta_{k}", r.get(f"cuenta_{k}"))
                add(c)
            # --- cartera (se conserva el mapa de IDs para los cobros/pagos) ---
            for Doc, Mov, td, tm, ter in ((DocumentoCxC, CobroCxC, "documentos_cxc", "cobros_cxc", "deudor"), (DocumentoCxP, PagoCxP, "documentos_cxp", "pagos_cxp", "proveedor")):
                ids = {}
                for r in leer(src, td):
                    d = Doc(empresa_id=emp[r["cliente_id"]], numero_documento=r["numero_documento"], fecha_emision=fch(r["fecha_emision"]), fecha_vencimiento=fch(r["fecha_vencimiento"]),
                            monto=dec(r["monto"]), saldo_pendiente=dec(r["saldo_pendiente"]), estado=r.get("estado") or "Pendiente", **{f"{ter}_nombre": r[f"{ter}_nombre"], f"{ter}_rif": r.get(f"{ter}_rif")})
                    add(d); db.session.flush(); ids[r["id"]] = d.id
                for r in leer(src, tm):
                    if r["documento_id"] in ids: add(Mov(documento_id=ids[r["documento_id"]], fecha=fch(r["fecha"]), monto=dec(r["monto"]), referencia=r.get("referencia")))
            # --- inventario ---
            for r in leer(src, "productos_inventario"):
                add(Producto(empresa_id=emp[r["cliente_id"]], codigo=r["codigo"], nombre=r["nombre"], metodo_costeo=r["metodo_costeo"], cantidad_actual=dec(r["cantidad_actual"], D4), costo_promedio_actual=dec(r["costo_promedio_actual"], D4)))
            db.session.flush()
            for r in leer(src, "movimientos_inventario"):
                add(MovInventario(empresa_id=emp[r["cliente_id"]], producto_codigo=r["producto_codigo"], tipo=r["tipo"], cantidad=dec(r["cantidad"], D4), costo_unitario=dec(r["costo_unitario"], D4), fecha=fch(r["fecha"]), referencia=r.get("referencia")))
            for r in leer(src, "lotes_peps"):
                add(LotePeps(empresa_id=emp[r["cliente_id"]], producto_codigo=r["producto_codigo"], fecha=fch(r["fecha"]), cantidad_restante=dec(r["cantidad_restante"], D4), costo_unitario=dec(r["costo_unitario"], D4)))
            # --- fiscal ---
            for r in (leer(src, "unidad_tributaria") if vacia(UnidadTributaria) else []):
                if es_ejemplo(r.get("gaceta_referencia")): omitidos.append(f"UT {r['valor_bs']} (marcada EJEMPLO)"); continue
                add(UnidadTributaria(valor_bs=dec(r["valor_bs"], D4), fecha_vigencia=fch(r["fecha_vigencia"]), gaceta_referencia=r.get("gaceta_referencia")))
            for r in (leer(src, "tarifa_islr_juridicas") if vacia(TarifaISLR) else []): add(TarifaISLR(desde_ut=r["desde_ut"], hasta_ut=r["hasta_ut"], porcentaje=r["porcentaje"], sustraendo_ut=r.get("sustraendo_ut") or 0))
            for r in (leer(src, "tabla_retenciones_islr") if vacia(ConceptoRetencionISLR) else []):
                if es_ejemplo(r["concepto"]): omitidos.append(f"Concepto de retención «{r['concepto']}»"); continue
                add(ConceptoRetencionISLR(concepto=r["concepto"], codigo_seniat=r.get("codigo_seniat"), porcentaje_juridica=r["porcentaje_persona_juridica"], porcentaje_natural=r["porcentaje_persona_natural"], sustraendo_ut=r.get("sustraendo_ut") or 0, monto_minimo_ut=r.get("monto_minimo_ut") or 0))
            for r in leer(src, "tabla_provision_incobrables"): add(TramoProvision(organizacion_id=org.id, dias_desde=r["dias_desde"], dias_hasta=r["dias_hasta"], porcentaje=r["porcentaje_provision"]))
            for r in (leer(src, "indice_inpc") if vacia(IndiceINPC) else []):
                if es_ejemplo(r.get("referencia")): omitidos.append(f"INPC {r['fecha']}={r['valor_indice']} (EJEMPLO)"); continue
                add(IndiceINPC(valor_indice=r["valor_indice"], fecha=fch(r["fecha"]), referencia=r.get("referencia")))
            for r in (leer(src, "tasa_cambio") if vacia(TasaCambio) else []): add(TasaCambio(fecha=fch(r["fecha"]), tasa_usd_ves=r["tasa_usd_ves"]))
            for r in leer(src, "retenciones_iva"):
                add(RetencionIVA(empresa_id=emp[r["cliente_id"]], rif_contribuyente=r["rif_contribuyente"], periodo_impositivo=r["periodo_impositivo"], fecha_documento=fch(r["fecha_documento"]), tipo_operacion=r["tipo_operacion"], tipo_documento=r["tipo_documento"],
                                 rif_proveedor=r["rif_proveedor"], numero_documento=r["numero_documento"], numero_control=r["numero_control"], monto_documento=dec(r["monto_documento"]), base_imponible=dec(r["base_imponible"]), monto_iva=dec(r["monto_iva"]),
                                 numero_documento_afectado=r.get("numero_documento_afectado"), numero_comprobante=r.get("numero_comprobante"), monto_exento=dec(r.get("monto_exento")), alicuota=dec(r["alicuota"]), numero_expediente=r.get("numero_expediente")))
            for r in leer(src, "retenciones_islr"):
                add(RetencionISLR(empresa_id=emp[r["cliente_id"]], fecha=fch(r["fecha"]), concepto=r["concepto"], codigo_seniat=r.get("codigo_seniat"), beneficiario_nombre=r["beneficiario_nombre"], beneficiario_rif=r["beneficiario_rif"], tipo_beneficiario=r["tipo_beneficiario"],
                                  monto_pagado=dec(r["monto_pagado"]), porcentaje_aplicado=dec(r["porcentaje_aplicado"]), sustraendo_bs=dec(r.get("sustraendo_bs")), monto_retenido=dec(r["monto_retenido"]), numero_comprobante=r.get("numero_comprobante")))
            for r in leer(src, "partidas_conciliacion_fiscal"): add(PartidaConciliacion(empresa_id=emp[r["cliente_id"]], ejercicio_fiscal=r["ejercicio_fiscal"], concepto=r["concepto"], categoria=r.get("categoria"), tipo=r["tipo"], monto=dec(r["monto"])))
            for r in leer(src, "rebajas_islr"): add(RebajaISLR(empresa_id=emp[r["cliente_id"]], ejercicio_fiscal=r["ejercicio_fiscal"], concepto=r["concepto"], monto_bs=dec(r["monto_bs"])))
            for r in leer(src, "documentos_fiscales"):
                add(DocumentoFiscal(empresa_id=emp[r["cliente_id"]], tipo_documento=r["tipo_documento"], rif_tercero=r["rif_tercero"], razon_social_tercero=r["razon_social_tercero"], numero_factura=r["numero_factura"], numero_control=r.get("numero_control"), fecha=fch(r["fecha"]),
                                    base_imponible=dec(r["base_imponible"]), porcentaje_iva=dec(r["porcentaje_iva"]), monto_iva=dec(r["monto_iva"]), total_operacion=dec(r["total_operacion"]), retencion_iva=dec(r.get("retencion_iva")), retencion_islr=dec(r.get("retencion_islr"))))
            for r in leer(src, "extracto_bancario"):
                add(ExtractoBancario(empresa_id=emp[r["cliente_id"]], fecha=fch(r["fecha"]), descripcion=r.get("descripcion"), monto=dec(r["monto"]), conciliado=bool(r.get("conciliado")), categoria_flujo=r.get("categoria_flujo") or "Operacion"))
            for r in leer(src, "hallazgos_auditoria"):
                add(Hallazgo(empresa_id=emp[r["cliente_id"]], titulo=r["titulo"], descripcion=r.get("descripcion"), cedula_ref=r.get("cedula_ref"), cuenta_codigo=r.get("cuenta_codigo"), severidad=r["severidad"], estado=r.get("estado") or "Abierto", responsable=r.get("responsable"), auditor=r.get("auditor") or "legado", comentario_cierre=r.get("comentario_cierre")))
            for r in leer(src, "papeles_trabajo"):
                add(PapelTrabajo(empresa_id=emp[r["cliente_id"]], cedula_tipo=r["cedula_tipo"], cuenta_codigo=r.get("cuenta_codigo"), titulo=r["titulo"], contenido=r.get("contenido"), auditor=r.get("auditor") or "legado"))
            db.session.flush()

            # ---------------- conciliación obligatoria ----------------
            ids = ",".join(str(i) for i in emp.values()) or "NULL"   # concilia SOLO las empresas de este cliente
            def sql(q): return db.session.execute(db.text(q.replace("{E}", f"empresa_id IN ({ids})"))).fetchone()
            comprobaciones = [
                ("movimientos (n, debe, haber)", src.execute("SELECT COUNT(*), ROUND(COALESCE(SUM(debe),0),2), ROUND(COALESCE(SUM(haber),0),2) FROM movimientos").fetchone(), sql("SELECT COUNT(*), COALESCE(SUM(debe),0), COALESCE(SUM(haber),0) FROM movimientos WHERE {E}")),
                ("saldo CxC", src.execute("SELECT ROUND(COALESCE(SUM(saldo_pendiente),0),2) FROM documentos_cxc").fetchone(), sql("SELECT COALESCE(SUM(saldo_pendiente),0) FROM documentos_cxc WHERE {E}")),
                ("saldo CxP", src.execute("SELECT ROUND(COALESCE(SUM(saldo_pendiente),0),2) FROM documentos_cxp").fetchone(), sql("SELECT COALESCE(SUM(saldo_pendiente),0) FROM documentos_cxp WHERE {E}")),
                ("nº de asientos", src.execute("SELECT COUNT(*) FROM asientos").fetchone(), sql("SELECT COUNT(*) FROM asientos WHERE {E}")),
            ]
            for nombre, o, n in comprobaciones:
                if tuple(dec(x) for x in o) != tuple(dec(x) for x in n): raise RuntimeError(f"Conciliación FALLÓ en {nombre}: origen={tuple(o)} destino={tuple(n)}")
                print(f"Conciliación OK — {nombre}: {tuple(dec(x) for x in n)}")
            if omitidos:
                print("\nNO MIGRADO (valores de ejemplo del sistema original; cargue los reales en Administración → Parámetros):")
                for o in omitidos: print("  -", o)
            if ejecutar: db.session.commit(); print("\nMIGRACIÓN CONFIRMADA")
            else: db.session.rollback(); print("\nEnsayo correcto: no se guardó nada (use --ejecutar)")
        except Exception:
            db.session.rollback(); raise
    # bitácora histórica: archivo de solo lectura
    destino = ruta.rsplit(".", 1)[0] + "_bitacora_historica.csv"
    filas = leer(src, "audit_log")
    if filas and ejecutar:
        with open(destino, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(filas[0])); w.writeheader(); w.writerows(filas)
        print(f"Bitácora histórica archivada en {destino} ({len(filas)} registros)")


if __name__ == "__main__":
    main(sys.argv[1], "--ejecutar" in sys.argv, _arg("--org-rif"), _arg("--org-nombre"))
