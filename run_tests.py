"""Ejecutor mínimo para entornos sin pytest (con pytest instalado, use simplemente `pytest`)."""
import importlib, inspect, sys, traceback, types
sys.path.insert(0, ".")
try:
    import pytest  # noqa: F401
except ImportError:
    pt = types.ModuleType("pytest"); pt.fixture = lambda f: f; sys.modules["pytest"] = pt
ok = fail = 0
for mod in ("tests.test_licencias", "tests.test_validadores", "tests.test_calculos", "tests.test_premigracion", "tests.test_seguridad_legacy",
            "tests.test_facturacion"):
    try: M = importlib.import_module(mod)
    except ModuleNotFoundError as e:
        if e.name == mod: continue   # el módulo de pruebas aún no existe
        raise
    for n, f in inspect.getmembers(M, inspect.isfunction):
        if not n.startswith("test_"): continue
        try:
            f(M.Entorno()) if f.__code__.co_argcount else f(); ok += 1
        except Exception:
            fail += 1; print("FAIL", mod, n); traceback.print_exc()
print(f"{ok} passed, {fail} failed"); sys.exit(1 if fail else 0)
