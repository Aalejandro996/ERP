from app import legal as L


def _c(**kw):
    return dict(numero_txt="C-000001", cliente_nombre="General de Suministros Orituco, C.A.", cliente_rif="J-12345678-9", cliente_domicilio="Altagracia de Orituco",
                representante="Ana Pérez", representante_ci="V-12345678", presupuesto_txt="P-000002", objeto="Licencia Contax All in One", monto="400",
                forma_pago="100% al firmar", inicio="2026-11-01", fin="2027-10-31", **{"opcionales": [], **kw})


def test_clausulas_fijas_cubren_el_marco_legal():
    texto = "\n".join(t + " " + " ".join(ps) for t, ps in L.clausulas(_c()))
    for ref in ("LOCDOFT", "ONCDOFT", "Ley Especial contra los Delitos Informáticos", "Mensajes de Datos y Firmas Electrónicas", "SNAT/2011/00071",
                "Gaceta Oficial", "Derecho de Autor", "Convenio Cambiario N° 1", "artículos 28 y 60", "Código Civil", "Caracas"):
        assert ref in texto, ref
    assert "$400,00" in texto and "J-12345678-9" in texto and "01/11/2026" in texto and "________" not in texto
    assert L.clausulas(_c())[0][0].startswith("PRIMERA") and "Ley aplicable" in L.clausulas(_c())[-1][0]


def test_opcionales_y_huella():
    c = _c(opcionales=["whatsapp", "profit", "soporte", "arbitraje"], soporte_mensual="36", especiales="Entrega en Altagracia de Orituco")
    cl = L.clausulas(c)
    titulos = [t for t, _ in cl]
    assert any("WhatsApp" in t for t in titulos) and any("Profit Plus" in t for t in titulos) and any("Estipulaciones especiales" in t for t in titulos)
    assert "Ley aplicable" in titulos[-1] and "CEDCA" in " ".join(cl[-1][1]) and "$36,00" in " ".join(p for _, ps in cl for p in ps)
    assert L.texto_plano(c) == L.texto_plano(dict(c)) and L.texto_plano(c) != L.texto_plano(_c())
    assert L.sugerir_opcionales("Integración del bot de WhatsApp con Profit Plus; soporte mensual") == ["whatsapp", "profit", "soporte"]
    assert set(L.PAGINAS) == {"terminos", "privacidad", "cookies"}
