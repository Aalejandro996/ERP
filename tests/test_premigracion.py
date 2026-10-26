import os, sqlite3, sys, tempfile
sys.path.insert(0, ".")
from scripts.premigracion_check import revisar


def _bd():
    p = os.path.join(tempfile.mkdtemp(), "o.db"); c = sqlite3.connect(p)
    c.executescript("""
    CREATE TABLE clientes_contables(id INTEGER PRIMARY KEY, razon_social, rif);
    CREATE TABLE cuentas(codigo, cliente_id, nombre, tipo);
    CREATE TABLE asientos(id INTEGER PRIMARY KEY, cliente_id, fecha, concepto);
    CREATE TABLE movimientos(id INTEGER PRIMARY KEY, asiento_id, cuenta_codigo, debe, haber);
    CREATE TABLE usuarios(usuario, password_hash, salt, rol);
    INSERT INTO clientes_contables VALUES(1,'A','J-1'),(2,'B','J-1');
    INSERT INTO cuentas VALUES('1.1',1,'Caja','Activo'),('4.1',1,'Ventas','Ingreso');
    INSERT INTO asientos VALUES(1,1,'2026-01-05','ok'),(2,1,'05/01/2026','mala fecha'),(3,1,'2026-01-06','descuadre');
    INSERT INTO movimientos VALUES(1,1,'1.1',100,0),(2,1,'4.1',0,100),(3,2,'1.1',50,0),(4,2,'4.1',0,50),
                                   (5,3,'1.1',10,0),(6,3,'9.9',0,5);
    """)
    import hashlib
    c.execute("INSERT INTO usuarios VALUES('admin',?,NULL,'Administrador')", (hashlib.sha256(b"admin123").hexdigest(),))
    c.commit(); c.close(); return p


def test_detecta_problemas():
    msgs = " | ".join(m for _, m in revisar(_bd()))
    assert "Asiento 3 descuadrado" in msgs
    assert "cuenta 9.9" in msgs
    assert "fecha con formato inválido" in msgs
    assert "RIF duplicado" in msgs
    assert "admin123" in msgs
