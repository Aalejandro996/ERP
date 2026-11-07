"""Plantillas de los documentos legales de los sistemas de Gigadevs Corp (Contax, CriteriSeg, Enlace MIT, Surtia, Quincena VE).

Son BORRADORES de trabajo: ninguno rige hasta que la abogada autorizada (Inpreabogado) los revisa, los edita si hace falta y los
aprueba en la consola Maestro → Legal. Cada versión aprobada queda con su huella SHA-256, la firma de la abogada y la fecha; lo que
cada cliente acepta queda registrado contra esa versión exacta.

Marco venezolano de referencia (ver docs/MARCO_LEGAL.md, con fuentes):
  * Constitución (CRBV): arts. 28 (habeas data), 60 (honor, intimidad, límites al uso de la informática), 98 (propiedad
    intelectual), 112 (libertad económica) y 117 (derecho a bienes y servicios de calidad e información adecuada).
  * TSJ, Sala Constitucional, sentencia N° 1318 del 04/08/2011 (vinculante): principios del tratamiento de datos personales y
    consentimiento previo, libre, informado, inequívoco y revocable. Venezuela no tiene una ley especial de datos personales.
  * Decreto-Ley sobre Mensajes de Datos y Firmas Electrónicas (G.O. 37.148 del 28/02/2001): eficacia probatoria de los mensajes de
    datos (art. 4) y de la firma electrónica; la firma con certificado de proveedor acreditado ante SUSCERTE goza de presunción.
  * Ley Especial contra los Delitos Informáticos (G.O. 37.313 del 30/10/2001). Ley sobre el Derecho de Autor (G.O. 4.638 E, 1993).
  * Código Civil (arts. 1.133, 1.137, 1.141, 1.159, 1.160, 1.264, 1.363 y 1.380) y Código de Comercio.
  * Ley Orgánica contra la Delincuencia Organizada y Financiamiento al Terrorismo (LOCDOFT) y lineamientos de la ONCDOFT/UNIF.
  * Código Orgánico Tributario, Ley del IVA, Ley de IGTF, Providencias SNAT/2011/00071 y SNAT/2024/000102 (facturación por medios
    digitales), Convenio Cambiario N° 1 (pacto de obligaciones en divisas).
  * Ley Orgánica de Precios Justos (relación con consumidores en servicios a personas naturales, p. ej. Quincena VE).
"""
from hashlib import sha256

EMPRESA = "Gigadevs Corp"
CONTACTO = "gigadevsc@gmail.com · WhatsApp +58 424 258 8321"
TIPOS = {"contrato": "Contrato de licencia de uso y suscripción", "terminos": "Términos y condiciones de uso",
         "privacidad": "Política de privacidad y tratamiento de datos personales", "cookies": "Política de cookies"}

# Perfil de cada sistema: lo que cambia entre un documento y otro
PERFILES = {
    "contax": dict(
        nombre="Contax®", servicio="plataforma web de contabilidad (VEN-NIF), facturación conforme a las normas del SENIAT, administración, "
        "inventario, auditoría y bot de WhatsApp para empresas", cliente="la organización contratante y sus usuarios", persona_juridica=True,
        datos="datos de identificación de los usuarios (nombre, usuario, correo, cédula cuando aplique), datos de acceso (IP, navegador, fecha y hora), "
        "y la información contable, fiscal, comercial y de terceros (clientes, proveedores, trabajadores) que la organización registra",
        terceros="Brevo (correo), Render (alojamiento en la nube), Ávila Pay y Binance (cobros), OpenWA o Meta (WhatsApp, si se activa)",
        especiales=[
            ("Cumplimiento tributario", "El sistema es una herramienta para cumplir los deberes formales del Código Orgánico Tributario, la Ley del IVA, "
             "la Ley de IGTF y las providencias del SENIAT, incluidas las de facturación por medios digitales. La determinación, declaración y pago de "
             "los tributos, la veracidad de lo facturado y la contratación de la imprenta digital o del proveedor de números de control son "
             "responsabilidad del contribuyente. GIGADEVS incorporará los cambios publicados en Gaceta Oficial en un plazo razonable."),
            ("Conservación de la información", "La organización conserva sus libros y comprobantes por el tiempo que exige el Código Orgánico Tributario; "
             "el sistema permite exportarlos. Al terminar el contrato tendrá treinta (30) días para descargar su información.")]),
    "criteriseg": dict(
        nombre="CriteriSeg", servicio="plataforma de auditoría y cumplimiento normativo: planificación, trabajo de campo, evidencias, informes y "
        "planes de acción", cliente="la organización contratante y sus usuarios", persona_juridica=True,
        datos="datos de identificación y acceso de los usuarios, y la documentación de auditoría, evidencias y hallazgos que la organización carga",
        terceros="Render u otra infraestructura en la nube (o la infraestructura propia de la organización en entorno cerrado) y Brevo (correo)",
        especiales=[
            ("Independencia y juicio profesional", "CriteriSeg apoya el trabajo de auditoría; las conclusiones, opiniones e informes son responsabilidad "
             "exclusiva de los profesionales de la organización. GIGADEVS no audita ni certifica la información."),
            ("Entorno cerrado", "En instalaciones en la red de la organización, esta mantiene la seguridad perimetral, los respaldos y la renovación "
             "oportuna del archivo de licencia; la suspensión o revocación se aplica al renovar o vencer la licencia instalada.")]),
    "bot_whatsapp": dict(
        nombre="Enlace MIT", servicio="plataforma de mensajería para organizaciones: sesiones, envío de mensajes, historial e incidencias",
        cliente="la organización contratante y sus usuarios", persona_juridica=True,
        datos="datos de identificación y acceso de los usuarios, números telefónicos y el contenido de los mensajes que la organización envía o recibe",
        terceros="WhatsApp/Meta y la pasarela de mensajería, Render u otra infraestructura en la nube y Brevo (correo)",
        especiales=[
            ("Uso responsable de la mensajería", "La organización solo escribe a personas que dieron su consentimiento, no envía publicidad no solicitada "
             "ni mensajes masivos que violen las políticas de WhatsApp, y responde por el contenido. Las restricciones o bloqueos que imponga WhatsApp "
             "son ajenos a GIGADEVS."),
            ("Entorno cerrado", "En instalaciones en la red de la organización, esta controla el acceso por VPN o firewall y la custodia de los mensajes.")]),
    "surtia": dict(
        nombre="Surtia™", servicio="bot de pedidos por WhatsApp conectado en solo lectura al sistema administrativo de la empresa (Profit Plus, Odoo, "
        "WooCommerce u otros): catálogo, existencias, precios, pedidos, presupuestos y lectura de órdenes de compra", cliente="la empresa contratante y sus usuarios",
        persona_juridica=True,
        datos="datos de identificación y acceso de los usuarios, el catálogo y existencias leídos del sistema administrativo, y los datos de los "
        "clientes de la empresa que escriben por WhatsApp (nombre, teléfono, pedidos y conversaciones)",
        terceros="WhatsApp/Meta y la pasarela OpenWA, el sistema administrativo de la empresa (solo lectura), Render (nube) y Brevo (correo)",
        especiales=[
            ("Conexión de solo lectura", "Surtia consulta el sistema administrativo de la empresa con un usuario de solo lectura y no escribe, modifica ni "
             "envía datos a ese sistema. La factura fiscal se sigue emitiendo en el sistema de la empresa. Las licencias de ese sistema son de la empresa."),
            ("Mensajes con los clientes de la empresa", "La empresa responde ante sus propios clientes por precios, existencias, entregas y por contar con su "
             "consentimiento para atenderlos por WhatsApp. GIGADEVS trata esos datos solo por cuenta de la empresa y para prestar el servicio.")]),
    "control_financiero": dict(
        nombre="Quincena VE", servicio="aplicación de finanzas personales por quincena (ingresos, gastos, deudas, pagos) y, con la suscripción "
        "Negocio Plus, herramientas para el negocio de la persona (ventas, catálogo, inventario, recibos y vendedores)", cliente="la persona titular de la cuenta",
        persona_juridica=False,
        datos="nombre de usuario, correo, país y moneda, datos de acceso, y la información financiera que la persona registra (ingresos, gastos, "
        "deudas, cuentas y, si usa Negocio Plus, ventas, productos y clientes de su negocio)",
        terceros="Render (nube), Brevo (correo), Ávila Pay (cobro de la suscripción) y los servicios de tasa de cambio (BCV y dolarapi)",
        especiales=[
            ("Información orientativa", "Quincena VE organiza la información que la persona registra y muestra cálculos orientativos; no presta asesoría "
             "financiera, contable ni legal, ni maneja dinero. Las decisiones financieras son de la persona."),
            ("Suscripción Negocio Plus", "La suscripción se paga por adelantado por el período elegido, se renueva solo si la persona vuelve a pagar y se "
             "factura a su nombre. Puede cancelarla cuando quiera dejando de renovarla; el período ya pagado se respeta hasta su vencimiento. Los derechos "
             "que la Ley Orgánica de Precios Justos y la Constitución (art. 117) reconocen a las personas como usuarias de servicios no se renuncian.")]),
}


def _secciones_contrato(p):
    juridica = p["persona_juridica"]
    parte = "EL CLIENTE" if juridica else "LA PERSONA USUARIA"
    return [
        ("Partes, objeto y aceptación",
         [f"El presente contrato se celebra entre {EMPRESA}, en adelante «GIGADEVS», titular de {p['nombre']}, y {p['cliente']}, en adelante «{parte}». "
          f"Su objeto es el otorgamiento del derecho de uso de {p['nombre']}, {p['servicio']}, en los términos del plan contratado.",
          f"{parte} acepta este contrato al marcar la casilla de aceptación y pulsar «Acepto» al ingresar por primera vez o cuando se publique una nueva "
          "versión. Esa aceptación es un mensaje de datos con plena eficacia probatoria conforme al Decreto-Ley sobre Mensajes de Datos y Firmas "
          "Electrónicas, y queda registrada con la identidad de quien acepta, la fecha y hora, la dirección IP, el navegador y la huella digital "
          "(SHA-256) del texto aceptado. " + ("Quien acepta declara tener facultades suficientes para obligar a la organización que representa."
                                             if juridica else "Quien acepta declara ser mayor de edad y tener capacidad para contratar.")]),
        ("Licencia de uso", [
         f"GIGADEVS otorga a {parte} una licencia de uso no exclusiva, intransferible, revocable y limitada al plazo, módulos, usuarios y demás límites del "
         "plan contratado, que viajan firmados digitalmente en el archivo de licencia o suscripción. No se transfiere la propiedad del software.",
         f"{parte} no podrá copiar, modificar, descompilar, desensamblar, someter a ingeniería inversa, sublicenciar, alquilar, ceder ni eludir los "
         "controles técnicos de licencia del software."]),
        ("Plan, precio, facturación y moneda", [
         "El precio es el del plan vigente al contratar o renovar, expresado en dólares de los Estados Unidos con el impuesto al valor agregado "
         "incluido, pactado en moneda extranjera conforme al Convenio Cambiario N° 1 del Banco Central de Venezuela. Podrá pagarse en divisas, en USDT o "
         "en bolívares al tipo de cambio oficial publicado por el BCV para el día del pago, por los medios habilitados (Ávila Pay, transferencia, pago "
         "móvil, Binance u otros).",
         "Por cada pago GIGADEVS emite la factura conforme a las normas del SENIAT y la envía al correo registrado; antes del pago puede enviarse una "
         "orden de compra. El Impuesto a las Grandes Transacciones Financieras (IGTF) y las comisiones de la pasarela de pago se trasladan cuando la ley "
         "o el medio de pago lo permitan, y se muestran antes de pagar. Si la organización es agente de retención, entregará sus comprobantes en los plazos legales."]),
        ("Vigencia, renovación y suspensión", [
         "La suscripción dura el período pagado y se renueva con un nuevo pago. GIGADEVS notificará los cambios de precio con al menos treinta (30) días "
         "de anticipación; no afectan períodos ya pagados. La falta de pago al vencimiento suspende el acceso, sin pérdida de la información durante los "
         "treinta (30) días siguientes."]),
        ("Obligaciones de GIGADEVS", [
         "a) Prestar el servicio con diligencia, con un objetivo de disponibilidad mensual del 99 %, salvo mantenimientos programados y causas ajenas. "
         "b) Prestar soporte por los canales informados. c) Hacer respaldos periódicos. d) Proteger la información con medidas técnicas razonables: "
         "verificación en dos pasos, cifrado de secretos, aislamiento por cliente y bitácora inalterable. e) Notificar sin demora indebida los "
         "incidentes de seguridad que afecten los datos y las medidas adoptadas. f) Guardar confidencialidad."]),
        (f"Obligaciones de {parte}", [
         "a) Suministrar datos veraces y mantenerlos actualizados. b) Custodiar sus credenciales y segundos factores, que son personales e "
         "intransferibles, y avisar de inmediato cualquier uso no autorizado. c) Usar el servicio solo para fines lícitos y conforme a este contrato. "
         "d) Pagar el precio en los plazos convenidos. e) Contar con las autorizaciones de los terceros cuyos datos registre. f) Revisar la información "
         "que obtiene del sistema antes de usarla ante terceros u organismos."]),
        *[(t, [x]) for t, x in p["especiales"]],
        ("Datos personales y confidencialidad", [
         f"El tratamiento de datos se rige por la Política de privacidad de {p['nombre']}, que forma parte de este contrato. GIGADEVS trata " +
         ("los datos que la organización registra por cuenta de ella, como encargado, solo para prestar el servicio" if juridica else
          "los datos de la persona solo para prestar el servicio, facturar y cumplir obligaciones legales") +
         ", conforme a los artículos 28 y 60 de la Constitución y los principios fijados por la Sala Constitucional del TSJ en la sentencia N° 1318 "
         "de 2011. No vende ni cede datos a terceros, salvo a los proveedores necesarios para el servicio o por orden de autoridad competente.",
         "Cada parte guarda reserva de la información confidencial de la otra durante la vigencia y por tres (3) años después."]),
        ("Propiedad intelectual", [
         f"{p['nombre']}, su código, diseño, documentación, marcas y signos distintivos son propiedad de GIGADEVS, protegidos por la Ley sobre el "
         f"Derecho de Autor y el artículo 98 de la Constitución. La información que {parte} registra es de su propiedad; GIGADEVS solo la usa para "
         "prestar el servicio y puede elaborar estadísticas anónimas y agregadas para mejorarlo."]),
        ("Prevención de legitimación de capitales y financiamiento al terrorismo", [
         f"{parte} declara que los fondos con que paga provienen de actividades lícitas y no guardan relación con los delitos previstos en la Ley "
         "Orgánica contra la Delincuencia Organizada y Financiamiento al Terrorismo. GIGADEVS podrá solicitar información de debida diligencia, "
         "abstenerse de contratar, suspender o terminar el servicio ante indicios razonables o coincidencias en listas de sanciones, y cooperar con "
         "las autoridades competentes cuando la ley lo exija."]),
        ("Seguridad informática", [
         "Las partes reconocen la aplicación de la Ley Especial contra los Delitos Informáticos. El acceso indebido, el sabotaje, la interceptación, el "
         "uso de credenciales ajenas o la vulneración de los controles de licencia son incumplimiento grave, sin perjuicio de las acciones penales."]),
        ("Limitación de responsabilidad y fuerza mayor", [
         f"La responsabilidad total de GIGADEVS no excederá lo pagado por {parte} en los doce (12) meses anteriores al hecho que la origine. GIGADEVS no "
         "responde por lucro cesante, daños indirectos, decisiones tomadas con la información del sistema, ni por fallas de servicios de terceros "
         f"({p['terceros']}). Ninguna parte responde por caso fortuito o fuerza mayor. " +
         ("" if juridica else "Esta limitación no afecta los derechos que la ley reconoce a la persona como usuaria y que no son renunciables.")]),
        ("Terminación y devolución de datos", [
         f"{parte} puede terminar la relación en cualquier momento dejando de renovar; GIGADEVS puede terminarla por incumplimiento grave, previa "
         "notificación con quince (15) días para subsanarlo, o de inmediato por las causas de prevención de legitimación de capitales o seguridad. Al "
         "terminar, la información se puede exportar durante treinta (30) días; luego se elimina de forma segura, salvo lo que la ley obligue a "
         "conservar. Los períodos ya iniciados no se reembolsan, salvo incumplimiento imputable a GIGADEVS."]),
        ("Modificaciones", [
         "GIGADEVS puede actualizar este contrato. Cada nueva versión, revisada y aprobada por su abogada, se informa al ingresar y se solicita su "
         "aceptación; si no se acepta, la relación termina al vencer el período pagado, sin penalidad."]),
        ("Notificaciones", [
         f"Las notificaciones se hacen por correo electrónico a la dirección registrada por {parte} y a {CONTACTO}, o por mensajes dentro del sistema, "
         "y se tienen por recibidas conforme al Decreto-Ley sobre Mensajes de Datos y Firmas Electrónicas."]),
        ("Ley aplicable y controversias", [
         "Este contrato se rige por las leyes de la República Bolivariana de Venezuela; en lo no previsto se aplican el Código Civil y el Código de "
         "Comercio. Las partes procurarán resolver sus diferencias de buena fe durante treinta (30) días y, de persistir, eligen como domicilio especial "
         "la ciudad de Caracas, a cuyos tribunales competentes se someten" + ("." if juridica else
         ", sin perjuicio del derecho de la persona usuaria de acudir ante los órganos de protección al consumidor que correspondan.")]),
    ]


def _secciones_terminos(p):
    return [
        ("Quiénes somos", [f"{p['nombre']} es un servicio de {EMPRESA} («Gigadevs»): {p['servicio']}. Contacto: {CONTACTO}."]),
        ("Aceptación", ["Al crear una cuenta, activar una licencia o suscripción, aceptar una invitación o usar el servicio, usted acepta estos "
                        "Términos, la Política de privacidad y la de cookies. Si no está de acuerdo, no use el servicio."]),
        ("Cuentas y seguridad", ["Cada cuenta es personal. Usted es responsable de custodiar su contraseña y su verificación en dos pasos, y de la "
                                 "actividad realizada con ellas. Avise de inmediato cualquier acceso no autorizado."]),
        ("Uso aceptable", ["Está prohibido usar el servicio para actividades ilícitas, para legitimar capitales o financiar el terrorismo, para enviar "
                           "spam o contenido ofensivo, para vulnerar la seguridad del sistema o de terceros, o para eludir sus controles de licencia "
                           "(Ley Especial contra los Delitos Informáticos)."]),
        ("Pagos y facturación", ["Los precios se muestran antes de pagar, en dólares con IVA incluido; el pago en bolívares se hace a la tasa oficial "
                                 "del BCV del día. Cada pago se factura conforme a las normas del SENIAT y la factura llega por correo."]),
        *[(t, [x]) for t, x in p["especiales"]],
        ("Disponibilidad y cambios", ["Trabajamos para mantener el servicio disponible y seguro; puede haber interrupciones por mantenimiento o causas "
                                      "ajenas. Podemos mejorar o cambiar funciones; los cambios relevantes se informan con anticipación."]),
        ("Propiedad intelectual", [f"El software, la marca {p['nombre']} y su documentación son de Gigadevs. Su información es suya."]),
        ("Responsabilidad", ["El servicio se presta con diligencia; Gigadevs no responde por daños indirectos ni por fallas de terceros, dentro de los "
                             "límites que permite la ley."]),
        ("Ley aplicable", ["Rigen las leyes de la República Bolivariana de Venezuela, con domicilio especial en Caracas."]),
    ]


def _secciones_privacidad(p):
    return [
        ("Responsable", [f"{EMPRESA} es responsable del tratamiento de los datos de las cuentas de {p['nombre']}" +
                         (" y encargado del tratamiento de la información que cada organización registra, que trata por cuenta de ella."
                          if p["persona_juridica"] else ".") + f" Contacto: {CONTACTO}."]),
        ("Marco aplicable", ["Venezuela no cuenta con una ley especial de protección de datos personales. Gigadevs aplica los artículos 28 y 60 de la "
                             "Constitución, la sentencia vinculante N° 1318 de 2011 de la Sala Constitucional del TSJ (consentimiento previo, libre, "
                             "informado, inequívoco y revocable; calidad, finalidad, seguridad y temporalidad de los datos) y la Ley Especial contra los "
                             "Delitos Informáticos."]),
        ("Qué datos tratamos", [f"Tratamos {p['datos']}."]),
        ("Para qué", ["Para prestar el servicio, autenticar a los usuarios, prevenir fraudes y accesos indebidos, facturar, dar soporte, enviar avisos "
                      "operativos y de seguridad, y cumplir obligaciones legales (tributarias y de prevención de legitimación de capitales). No usamos "
                      "los datos para publicidad de terceros ni los vendemos."]),
        ("Con quién se comparten", [f"Solo con los proveedores necesarios para el servicio: {p['terceros']}, obligados a confidencialidad, y con "
                                    "autoridades cuando una ley u orden competente lo exija. Algunos proveedores están fuera de Venezuela; los datos "
                                    "viajan cifrados."]),
        ("Seguridad", ["Contraseñas con algoritmo de resumen robusto, verificación en dos pasos obligatoria, cifrado de los secretos, conexiones "
                       "HTTPS, aislamiento por cliente, respaldos y bitácora inalterable de acciones."]),
        ("Conservación", ["Conservamos los datos mientras la cuenta esté activa y, después, solo el tiempo que exijan las leyes (por ejemplo, la "
                          "documentación tributaria) o para la defensa de derechos; luego se eliminan de forma segura."]),
        ("Sus derechos", ["Usted puede conocer, actualizar, rectificar y pedir la eliminación de sus datos, y revocar su consentimiento, escribiendo a "
                          f"{CONTACTO}. Responderemos en un plazo máximo de veinte (20) días hábiles. Si no recibe respuesta, puede ejercer la acción de "
                          "habeas data prevista en el artículo 28 de la Constitución."]),
        ("Menores de edad", ["El servicio no está dirigido a menores de edad."]),
        ("Cambios", ["Las nuevas versiones de esta política se informan al ingresar y, cuando corresponda, se solicita su aceptación."]),
    ]


def _secciones_cookies(p):
    return [
        ("Qué usamos", [f"{p['nombre']} usa solo cookies técnicas imprescindibles: la de sesión (para mantenerle identificado de forma segura) y la "
                        "de protección contra falsificación de solicitudes. No usamos cookies de publicidad ni de seguimiento de terceros."]),
        ("Cómo controlarlas", ["Puede borrarlas desde su navegador; si las bloquea, no podrá iniciar sesión."]),
    ]


GENERADORES = {"contrato": _secciones_contrato, "terminos": _secciones_terminos, "privacidad": _secciones_privacidad, "cookies": _secciones_cookies}


def plantilla(sistema: str, tipo: str) -> list[tuple[str, list[str]]]:
    if sistema not in PERFILES or tipo not in GENERADORES: raise ValueError("Documento legal inválido")
    return GENERADORES[tipo](PERFILES[sistema])


def titulo(sistema: str, tipo: str) -> str:
    return f"{TIPOS[tipo]} de {PERFILES[sistema]['nombre']}"


def texto_canonico(titulo_doc: str, secciones) -> str:
    partes = [titulo_doc]
    for i, (t, ps) in enumerate(secciones, 1):
        partes.append(f"{i}. {t}"); partes.extend(ps)
    return "\n".join(partes)


def huella(titulo_doc: str, secciones) -> str:
    return sha256(texto_canonico(titulo_doc, secciones).encode()).hexdigest()
