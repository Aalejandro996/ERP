"""Validación PREVIA a migrar: detecta datos que romperían las restricciones del sistema nuevo.
Solo lectura sobre el SQLite original. Uso: python scripts/premigracion_check.py sistema_contable.db
"""
import hashlib
import sqlite3
import sys


def revisar(ruta: str) -> list[tuple[str, str]]:
    con = sqlite3.connect(f"file:{ruta}?mode=ro", uri=True)
    hallazgos = []
    q = lambda sql: con.execute(sql).fetchall()

    for aid, d, h in q("""SELECT a.id, ROUND(COALESCE(SUM(m.debe),0),2) AS d, ROUND(COALESCE(SUM(m.haber),0),2) AS h
                          FROM asientos a LEFT JOIN movimientos m ON m.asiento_id=a.id
                          GROUP BY a.id HAVING d <> h OR COUNT(m.id) < 2"""):
        hallazgos.append(("CRITICO", f"Asiento {aid} descuadrado o incompleto (debe={d}, haber={h})"))
    for (n,) in q("SELECT COUNT(*) FROM movimientos m LEFT JOIN asientos a ON a.id=m.asiento_id WHERE a.id IS NULL HAVING COUNT(*)>0"):
        hallazgos.append(("CRITICO", f"{n} movimientos huérfanos (sin asiento)"))
    for aid, cod in q("""SELECT m.asiento_id, m.cuenta_codigo FROM movimientos m JOIN asientos a ON a.id=m.asiento_id
                         LEFT JOIN cuentas c ON c.codigo=m.cuenta_codigo AND c.cliente_id=a.cliente_id WHERE c.codigo IS NULL"""):
        hallazgos.append(("CRITICO", f"Asiento {aid} usa la cuenta {cod} que no existe para su cliente"))
    for aid, d, h in q("SELECT asiento_id, debe, haber FROM movimientos WHERE debe<0 OR haber<0 OR (debe>0 AND haber>0)"):
        hallazgos.append(("CRITICO", f"Asiento {aid}: monto negativo o debe+haber simultáneos"))
    for (rif, n) in q("SELECT rif, COUNT(*) FROM clientes_contables GROUP BY rif HAVING COUNT(*)>1"):
        hallazgos.append(("CRITICO", f"RIF duplicado {rif} ({n} clientes): requiere fusión manual"))
    for (aid, f) in q("SELECT id, fecha FROM asientos WHERE fecha NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"):
        hallazgos.append(("CRITICO", f"Asiento {aid}: fecha con formato inválido {f!r}"))
    # Credenciales débiles heredadas (PBKDF2-SHA256 200k con sal hex, igual que el sistema original)
    for u, h, salt in q("SELECT usuario, password_hash, salt FROM usuarios"):
        if not salt:
            hallazgos.append(("ADVERTENCIA", f"Usuario {u}: hash legado sin sal (no podrá entrar hasta que un administrador restablezca su clave)"))
            if h == hashlib.sha256(b"admin123").hexdigest():
                hallazgos.append(("CRITICO", f"Usuario {u} aún tiene la clave por defecto admin123"))
        else:
            try: calc = hashlib.pbkdf2_hmac("sha256", b"admin123", bytes.fromhex(salt), 200_000).hex()
            except ValueError: calc = None
            if calc and calc == h:
                hallazgos.append(("ADVERTENCIA", f"Usuario {u} tiene la clave por defecto admin123: la migración INVALIDARÁ esa contraseña; restablézcala con scripts/inicializar.py"))
    hallazgos.append(("INFO", f"Todos los usuarios migrados quedan con cambio de clave obligatorio y SIN empresas asignadas"))
    return hallazgos


if __name__ == "__main__":
    res = revisar(sys.argv[1])
    for nivel, msg in res: print(f"[{nivel}] {msg}")
    sys.exit(1 if any(n == "CRITICO" for n, _ in res) else 0)
