"""Textos legales de Contax® (Gigadevs Corp): cláusulas del contrato, Términos y condiciones, Política de privacidad y
Política de cookies. Una sola fuente para el contrato y las páginas públicas.

Marco venezolano de referencia (citado en los textos):
  * Constitución de la República Bolivariana de Venezuela (CRBV): arts. 28 (habeas data), 60 (honor, vida privada y
    límites al uso de la informática), 98 (propiedad intelectual) y 112 (libertad económica).
  * Código Civil: arts. 1.133, 1.159, 1.160 y 1.264 (contratos y cumplimiento). Código de Comercio.
  * Ley Orgánica contra la Delincuencia Organizada y Financiamiento al Terrorismo (LOCDOFT), G.O. N° 39.912 del 30/04/2012,
    y los lineamientos de la Oficina Nacional contra la Delincuencia Organizada y Financiamiento al Terrorismo (ONCDOFT).
  * Ley Especial contra los Delitos Informáticos, G.O. N° 37.313 del 30/10/2001.
  * Decreto con Fuerza de Ley sobre Mensajes de Datos y Firmas Electrónicas (Decreto N° 1.204), G.O. N° 37.148 del 28/02/2001.
  * Ley sobre el Derecho de Autor, G.O. Extraordinaria N° 4.638 del 01/10/1993 (protección de los programas de computación).
  * Código Orgánico Tributario (G.O. Extraordinaria N° 6.507 del 29/01/2020), Ley y Reglamento del IVA, Ley de IGTF y la
    Providencia Administrativa SNAT/2011/00071 (G.O. N° 39.795 del 08/11/2011) sobre emisión de facturas, y demás
    providencias del SENIAT publicadas en Gaceta Oficial.
  * Convenio Cambiario N° 1 del BCV (G.O. Extraordinaria N° 6.405 del 07/09/2018): pacto de obligaciones en moneda extranjera.

⚠ Redactado como base de trabajo por el equipo de producto. Debe revisarlo un abogado venezolano antes de usarse con
clientes; las referencias normativas deben verificarse contra la Gaceta Oficial vigente."""
from datetime import date

VERSION = "1.0 · 31/10/2026"
EMPRESA = "Gigadevs Corp"

# ---------------------------------------------------------------------------------------------------------------- contrato
# Cada cláusula: (clave, título, [párrafos]). Los textos admiten {variables} del contrato.
CLAUSULAS_BASE = [
    ("partes", "Partes y definiciones", [
        "Entre {proveedor}, en adelante «GIGADEVS», y {cliente}, inscrita en el Registro de Información Fiscal bajo el N° {cliente_rif}, con domicilio en "
        "{cliente_domicilio}, representada en este acto por {representante}, titular de la cédula de identidad N° {representante_ci}, en su carácter de "
        "{representante_cargo}, en adelante «EL CLIENTE», se celebra el presente contrato, que se regirá por las cláusulas siguientes.",
        "«Contax®» es la plataforma de gestión empresarial propiedad de GIGADEVS, marca registrada de Gigadevs Corp. «Servicios» son las licencias, "
        "implementaciones, integraciones, soporte y desarrollos descritos en el presupuesto N° {presupuesto}, que forma parte integrante de este contrato."]),
    ("objeto", "Objeto", [
        "GIGADEVS se obliga a prestar a EL CLIENTE los Servicios siguientes: {objeto}. EL CLIENTE se obliga a pagar el precio convenido y a cumplir las "
        "obligaciones aquí establecidas."]),
    ("precio", "Precio, forma de pago y moneda", [
        "El precio total es de {monto} (USD), impuesto al valor agregado incluido, pagadero así: {forma_pago}. Las partes convienen el precio en moneda "
        "extranjera conforme al Convenio Cambiario N° 1 del Banco Central de Venezuela; el pago podrá hacerse en dólares, en USDT o en bolívares al tipo de "
        "cambio oficial publicado por el BCV para el día del pago.",
        "GIGADEVS emitirá la factura de cada pago conforme a la Providencia Administrativa SNAT/2011/00071 y demás normas del SENIAT. Si EL CLIENTE es "
        "agente de retención, entregará los comprobantes de retención en los plazos legales. El Impuesto a las Grandes Transacciones Financieras (IGTF) que "
        "corresponda por pagos en divisas será asumido por quien la ley designe como contribuyente."]),
    ("vigencia", "Vigencia y renovación", [
        "Este contrato entra en vigencia el {inicio} y tendrá una duración hasta el {fin}. Podrá renovarse por períodos iguales mediante el pago de la "
        "renovación a los precios vigentes de GIGADEVS, que se notificarán con al menos treinta (30) días de anticipación."]),
    ("licencia", "Licencia de uso y propiedad intelectual", [
        "GIGADEVS otorga a EL CLIENTE una licencia de uso no exclusiva, intransferible y temporal sobre Contax® para los módulos, usuarios, empresas y "
        "equipos contratados. El software, su código, diseño, documentación y la marca Contax® son propiedad de GIGADEVS y están protegidos por la Ley "
        "sobre el Derecho de Autor y el artículo 98 de la Constitución. Queda prohibido copiar, descompilar, sublicenciar o explotar el software fuera de "
        "lo aquí permitido.",
        "Los datos que EL CLIENTE registra en Contax® son de su exclusiva propiedad. Los desarrollos a la medida se integran a la plataforma y quedan "
        "licenciados a EL CLIENTE durante la vigencia del contrato, salvo pacto escrito distinto."]),
    ("obligaciones_gigadevs", "Obligaciones de GIGADEVS", [
        "a) Mantener la plataforma disponible, con un objetivo de disponibilidad mensual de 99 %, salvo mantenimientos programados notificados y causas "
        "ajenas a su control. b) Prestar soporte por los canales informados en horario laboral. c) Aplicar respaldos periódicos de la base de datos. "
        "d) Mantener actualizados los parámetros legales del sistema (alícuotas, unidad tributaria, tasas, tarifas) dentro de un plazo razonable desde su "
        "publicación en Gaceta Oficial. e) Guardar confidencialidad y proteger la información de EL CLIENTE conforme a este contrato."]),
    ("obligaciones_cliente", "Obligaciones de EL CLIENTE", [
        "a) Suministrar datos fiscales y de identificación veraces y mantenerlos actualizados. b) Custodiar las credenciales y los segundos factores de "
        "autenticación de sus usuarios, que son personales e intransferibles. c) Usar Contax® solo para fines lícitos. d) Pagar el precio en los plazos "
        "convenidos. e) Revisar la información que emite antes de presentarla ante el SENIAT u otros organismos."]),
    ("tributario", "Cumplimiento tributario y Gacetas Oficiales", [
        "Contax® es una herramienta para que EL CLIENTE cumpla sus deberes formales conforme al Código Orgánico Tributario, la Ley del IVA, la Ley de IGTF "
        "y las providencias del SENIAT. La determinación, declaración y pago de los tributos, así como la veracidad de lo facturado, son responsabilidad "
        "exclusiva de EL CLIENTE en su condición de contribuyente. GIGADEVS incorporará los cambios normativos publicados en Gaceta Oficial; mientras se "
        "implementan, EL CLIENTE aplicará la norma vigente por los medios que corresponda."]),
    ("lcft", "Prevención de legitimación de capitales y financiamiento al terrorismo (LC/FT/FPADM)", [
        "EL CLIENTE declara bajo fe de juramento que los fondos con que paga este contrato provienen de actividades lícitas y que no guardan relación con "
        "los delitos previstos en la Ley Orgánica contra la Delincuencia Organizada y Financiamiento al Terrorismo (LOCDOFT). Se obliga a suministrar la "
        "información y los documentos que GIGADEVS le solicite en su política de debida diligencia «Conozca a su Cliente» (registro mercantil, RIF, "
        "identificación de representantes y beneficiarios finales).",
        "GIGADEVS podrá abstenerse de contratar, suspender el servicio o terminar este contrato de pleno derecho si EL CLIENTE, sus accionistas o "
        "representantes figuran en listas de sanciones nacionales o internacionales, o si existen indicios razonables de operaciones relacionadas con "
        "legitimación de capitales, financiamiento al terrorismo o a la proliferación de armas de destrucción masiva, y cooperará con la Oficina Nacional "
        "contra la Delincuencia Organizada y Financiamiento al Terrorismo (ONCDOFT) y demás autoridades competentes cuando la ley lo exija, sin que ello "
        "genere responsabilidad para GIGADEVS."]),
    ("datos", "Confidencialidad y protección de datos", [
        "Cada parte guardará reserva de la información confidencial de la otra durante la vigencia del contrato y por tres (3) años después. En el "
        "tratamiento de datos personales GIGADEVS actúa por cuenta de EL CLIENTE, solo para prestar los Servicios, con las medidas de seguridad descritas "
        "en su Política de privacidad, y respeta los derechos reconocidos en los artículos 28 y 60 de la Constitución (habeas data, honor, intimidad y "
        "límites al uso de la informática). GIGADEVS no vende ni cede los datos de EL CLIENTE a terceros, salvo requerimiento de autoridad competente."]),
    ("seguridad", "Seguridad informática y delitos informáticos", [
        "Las partes reconocen la aplicación de la Ley Especial contra los Delitos Informáticos. Constituyen incumplimiento grave, sin perjuicio de las "
        "acciones penales: el acceso indebido a la plataforma o a datos de terceros, el sabotaje o daño a sistemas, la interceptación de comunicaciones, "
        "el uso de credenciales ajenas y la vulneración de los controles de licencia. GIGADEVS mantiene autenticación en dos pasos obligatoria, cifrado "
        "de la información sensible, aislamiento de datos por cliente y una bitácora inalterable; notificará a EL CLIENTE, sin demora indebida, los "
        "incidentes de seguridad que afecten sus datos y las medidas adoptadas."]),
    ("electronico", "Mensajes de datos y firma electrónica", [
        "Conforme al Decreto con Fuerza de Ley sobre Mensajes de Datos y Firmas Electrónicas, las partes reconocen plena validez y eficacia probatoria a "
        "los mensajes de datos intercambiados por correo electrónico, WhatsApp y la plataforma, así como a la aceptación electrónica de este contrato, "
        "que queda registrada con el nombre y la cédula de quien acepta, la fecha y hora, la dirección IP y la huella digital (SHA-256) del texto aceptado. "
        "Las notificaciones se harán a los correos indicados en este contrato."]),
    ("responsabilidad", "Limitación de responsabilidad y fuerza mayor", [
        "La responsabilidad total de GIGADEVS por cualquier causa derivada de este contrato no excederá el monto efectivamente pagado por EL CLIENTE en "
        "los doce (12) meses anteriores al hecho que la origine. GIGADEVS no responde por lucro cesante, daños indirectos, decisiones tomadas por EL "
        "CLIENTE con la información del sistema, sanciones derivadas de datos suministrados por EL CLIENTE, ni por fallas de servicios de terceros "
        "(proveedores de internet, energía, Meta, WasenderAPI, Binance, bancos). Ninguna parte responde por incumplimientos causados por caso fortuito o "
        "fuerza mayor."]),
    ("terminacion", "Terminación y devolución de datos", [
        "Cualquiera de las partes podrá terminar este contrato por incumplimiento grave de la otra, previa notificación con quince (15) días para "
        "subsanarlo. GIGADEVS podrá suspender el servicio por falta de pago con diez (10) días de aviso. Al terminar el contrato, EL CLIENTE dispondrá de "
        "treinta (30) días para exportar sus datos; vencido ese plazo GIGADEVS podrá eliminarlos de forma segura, salvo las copias que la ley obligue a "
        "conservar. Los montos pagados por períodos ya iniciados no son reembolsables, salvo incumplimiento imputable a GIGADEVS."]),
    ("ley", "Ley aplicable, domicilio y solución de controversias", [
        "Este contrato se rige por las leyes de la República Bolivariana de Venezuela; en lo no previsto se aplicarán el Código Civil y el Código de "
        "Comercio. Las partes procurarán resolver sus diferencias de buena fe mediante conversaciones directas durante treinta (30) días. De persistir, "
        "{jurisdiccion}"]),
]
JURISDICCION_TRIBUNALES = "eligen como domicilio especial la ciudad de Caracas, a cuyos tribunales competentes declaran someterse."
JURISDICCION_ARBITRAJE = ("las someterán a arbitraje de derecho, conforme a la Ley de Arbitraje Comercial, administrado por el Centro Empresarial de "
                          "Conciliación y Arbitraje (CEDCA) en Caracas; el laudo será definitivo y obligatorio para las partes.")

CLAUSULAS_OPCIONALES = {
    "whatsapp": ("Bot de WhatsApp y servicios de terceros", [
        "El bot de WhatsApp funciona sobre el número de EL CLIENTE mediante la API oficial de Meta o mediante WasenderAPI, servicios de terceros cuyas "
        "condiciones EL CLIENTE acepta y contrata directamente. Las conexiones no oficiales pueden ser restringidas o bloqueadas por WhatsApp, en especial "
        "ante envíos masivos; GIGADEVS no responde por tales bloqueos. EL CLIENTE es responsable del contenido de los mensajes y de contar con el "
        "consentimiento de sus contactos, y se obliga a no usar el bot para publicidad no solicitada."]),
    "profit": ("Integración con Profit Plus", [
        "La integración con Profit Plus es de SOLO LECTURA: Contax® consulta artículos, existencias, precios y clientes y no escribe, modifica ni envía "
        "datos a Profit Plus. Las licencias de Profit Plus, del motor de base de datos y del servidor son de EL CLIENTE, quien otorgará un usuario de "
        "lectura. Los cambios de versión de Profit Plus que alteren su estructura de datos podrán requerir una adaptación que se cotizará por separado."]),
    "soporte": ("Soporte y mantenimiento mensual", [
        "El soporte y mantenimiento se presta por mensualidades de {soporte_mensual} (USD, IVA incluido), pagaderas por adelantado dentro de los cinco "
        "(5) primeros días de cada mes. Incluye la atención de consultas y la corrección de fallas; no incluye nuevos desarrollos."]),
    "cambios": ("Cambios y mejoras a la medida", [
        "Los cambios a la medida se solicitan por escrito; GIGADEVS estima su alcance y plazo antes de iniciarlos. Las horas incluidas no utilizadas no "
        "se acumulan para el período siguiente. Las mejoras de uso general que se incorporen a la plataforma podrán ofrecerse a otros clientes."]),
    "migracion": ("Migración de datos", [
        "EL CLIENTE entrega los datos de su sistema anterior y los valida fila por fila en la herramienta de migración antes de cargarlos; la exactitud "
        "de los saldos iniciales y su asiento de apertura son responsabilidad de EL CLIENTE y de su contador."]),
    "arbitraje": ("Arbitraje", ["Las partes convienen el arbitraje como medio de solución de controversias según la cláusula de ley aplicable."]),
}
NOMBRE_OPCIONAL = {"whatsapp": "Bot de WhatsApp y terceros", "profit": "Integración con Profit Plus (solo lectura)", "soporte": "Soporte mensual",
                   "cambios": "Cambios a la medida", "migracion": "Migración de datos", "arbitraje": "Arbitraje (CEDCA) en lugar de tribunales"}
ORDINALES = ("PRIMERA", "SEGUNDA", "TERCERA", "CUARTA", "QUINTA", "SEXTA", "SÉPTIMA", "OCTAVA", "NOVENA", "DÉCIMA", "UNDÉCIMA", "DUODÉCIMA",
             "DECIMOTERCERA", "DECIMOCUARTA", "DECIMOQUINTA", "DECIMOSEXTA", "DECIMOSÉPTIMA", "DECIMOCTAVA", "DECIMONOVENA", "VIGÉSIMA", "VIGÉSIMA PRIMERA")


def sugerir_opcionales(texto: str) -> list[str]:
    """Cláusulas opcionales que corresponden según lo que dice el presupuesto."""
    t = texto.lower()
    out = []
    if "whatsapp" in t or "bot" in t: out.append("whatsapp")
    if "profit" in t: out.append("profit")
    if "soporte" in t and "mensual" in t: out.append("soporte")
    if "cambio" in t or "a la medida" in t: out.append("cambios")
    if "migraci" in t: out.append("migracion")
    return out


class _Seguro(dict):
    def __missing__(self, k): return "________"


def clausulas(c: dict) -> list[tuple[str, list[str]]]:
    """Cláusulas numeradas del contrato con los datos rellenados. c: datos del contrato (dict del servidor de licencias)."""
    fmt = lambda f: date.fromisoformat(f).strftime("%d/%m/%Y") if f else "________"
    v = _Seguro(proveedor=f"{EMPRESA}" + (f", RIF {c['emisor_rif']}" if c.get("emisor_rif") else ""), cliente=c.get("cliente_nombre") or "________",
                cliente_rif=c.get("cliente_rif") or "________", cliente_domicilio=c.get("cliente_domicilio") or "________",
                representante=c.get("representante") or "________", representante_ci=c.get("representante_ci") or "________",
                representante_cargo=c.get("representante_cargo") or "representante legal", presupuesto=c.get("presupuesto_txt") or "________",
                objeto=c.get("objeto") or "________", monto="$" + _monto(c.get("monto")), forma_pago=c.get("forma_pago") or "________",
                inicio=fmt(c.get("inicio")), fin=fmt(c.get("fin")), soporte_mensual="$" + _monto(c.get("soporte_mensual") or "0"),
                jurisdiccion=JURISDICCION_ARBITRAJE if "arbitraje" in (c.get("opcionales") or []) else JURISDICCION_TRIBUNALES)
    lista = [(t, [p.format_map(v) for p in ps]) for k, t, ps in CLAUSULAS_BASE]
    pos = len(lista) - 1   # las opcionales van antes de «Ley aplicable»
    for k in c.get("opcionales") or []:
        if k in CLAUSULAS_OPCIONALES and k != "arbitraje":
            t, ps = CLAUSULAS_OPCIONALES[k]
            lista.insert(pos, (t, [p.format_map(v) for p in ps])); pos += 1
    if (c.get("especiales") or "").strip():
        lista.insert(pos, ("Estipulaciones especiales", [x.strip() for x in c["especiales"].splitlines() if x.strip()]))
    return [(f"{ORDINALES[i]}: {t}", ps) for i, (t, ps) in enumerate(lista)]


def _monto(v) -> str:
    from decimal import Decimal
    return f"{Decimal(str(v or 0)):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def texto_plano(c: dict) -> str:
    """Texto canónico del contrato: su SHA-256 es la huella que se registra al aceptarlo electrónicamente."""
    partes = [f"CONTRATO N° {c.get('numero_txt')} · {c.get('cliente_nombre')} · {c.get('cliente_rif')}"]
    for t, ps in clausulas(c):
        partes.append(t); partes.extend(ps)
    return "\n".join(partes)


# ---------------------------------------------------------------------------------------------------------- páginas legales
TERMINOS = [
    ("Quiénes somos", ["Contax® es una plataforma web de facturación, contabilidad, administración, inventario y auditoría para empresas venezolanas, "
                       "propiedad de Gigadevs Corp («Gigadevs»). Contax® es una marca registrada de Gigadevs Corp. Contacto: gigadevsc@gmail.com · "
                       "WhatsApp +58 424 258 8321."]),
    ("Aceptación", ["Al crear una cuenta, activar una licencia, aceptar una invitación o usar Contax® usted acepta estos Términos, la Política de privacidad "
                    "y la Política de cookies, que tienen validez como mensajes de datos conforme al Decreto con Fuerza de Ley sobre Mensajes de Datos y "
                    "Firmas Electrónicas. Si actúa en nombre de una empresa, declara tener facultades para obligarla."]),
    ("Licencia y planes", ["Gigadevs otorga una licencia de uso no exclusiva, intransferible y temporal según el plan contratado (módulos, usuarios, "
                           "empresas y equipos). La demo gratuita dura 72 horas y se concede una vez por organización. El software y la marca están "
                           "protegidos por la Ley sobre el Derecho de Autor; se prohíbe copiarlos, descompilarlos o revenderlos."]),
    ("Precios, pagos y facturación", ["Los precios se expresan en dólares e incluyen IVA salvo indicación contraria; pueden pagarse en USDT o en bolívares a "
                                      "la tasa oficial del BCV del día de pago. Gigadevs emite la factura fiscal conforme a las normas del SENIAT. Las "
                                      "licencias no son reembolsables una vez activadas, salvo falla imputable a Gigadevs que impida el uso del servicio."]),
    ("Su responsabilidad tributaria", ["Contax® ayuda a cumplir la Providencia SNAT/2011/00071, la Ley del IVA, la Ley de IGTF y demás normas, pero la "
                                       "veracidad de lo que factura y declara y el pago de sus tributos son responsabilidad del contribuyente. Gigadevs "
                                       "actualiza los parámetros legales publicados en Gaceta Oficial en un plazo razonable."]),
    ("Uso aceptable", ["Está prohibido usar Contax® para actividades ilícitas, para legitimar capitales o financiar el terrorismo (LOCDOFT), para enviar "
                       "mensajes masivos no solicitados por WhatsApp, o para acceder sin autorización a datos o sistemas, conductas sancionadas por la "
                       "Ley Especial contra los Delitos Informáticos. Gigadevs podrá suspender la cuenta y colaborar con la ONCDOFT y demás autoridades "
                       "cuando la ley lo exija, y aplica una política de debida diligencia «Conozca a su Cliente»."]),
    ("Cuentas y seguridad", ["Cada usuario es personal. La verificación en dos pasos es obligatoria. Usted custodia sus claves y códigos de respaldo y "
                             "debe avisar de inmediato cualquier uso no autorizado. Cada navegador autorizado cuenta como un equipo de su licencia."]),
    ("Servicios de terceros", ["Algunas funciones dependen de terceros (WhatsApp/Meta, WasenderAPI, Binance, Brevo, DolarAPI/BCV, Render). Su uso está "
                               "sujeto a las condiciones de esos proveedores y Gigadevs no responde por sus interrupciones ni bloqueos."]),
    ("Disponibilidad y responsabilidad", ["Gigadevs procura una disponibilidad de 99 % mensual y realiza respaldos periódicos. Su responsabilidad máxima se "
                                          "limita a lo pagado en los últimos doce (12) meses; no responde por daños indirectos ni lucro cesante."]),
    ("Terminación", ["Puede cancelar su suscripción al final del período. Al terminar dispondrá de 30 días para exportar sus datos, luego se eliminarán "
                     "de forma segura salvo lo que la ley obligue a conservar."]),
    ("Ley aplicable", ["Estos Términos se rigen por las leyes de la República Bolivariana de Venezuela. Las controversias se someterán a los tribunales "
                       "competentes de Caracas, salvo que un contrato firmado entre las partes disponga otra cosa."]),
    ("Cambios", ["Gigadevs puede actualizar estos Términos; los cambios importantes se avisarán por correo o en la plataforma con al menos 15 días de "
                 "anticipación. La versión vigente es la publicada en esta página."]),
]
PRIVACIDAD = [
    ("Responsable", ["Gigadevs Corp es responsable de los datos de las cuentas de usuario de Contax® y encargado del tratamiento de los datos que cada "
                     "empresa cliente registra (sus clientes, proveedores, facturas y libros), que pertenecen a esa empresa."]),
    ("Datos que tratamos", ["Datos de cuenta: nombre de usuario, correo, rol, contraseña (guardada con Argon2id, nunca legible) y el secreto de la "
                            "verificación en dos pasos (cifrado). Datos de seguridad: dirección IP, navegador, fecha de acceso y la bitácora de "
                            "operaciones. Datos de facturación de la licencia: razón social, RIF, dirección y pagos. Datos de negocio que usted registra."]),
    ("Para qué los usamos", ["Prestar el servicio, autenticar usuarios, prevenir fraudes y accesos indebidos, emitir facturas, cumplir obligaciones "
                             "tributarias y de prevención LC/FT, dar soporte y avisarle de cambios. No vendemos datos ni los usamos para publicidad de "
                             "terceros."]),
    ("Base y derechos", ["Tratamos los datos para ejecutar el contrato, cumplir la ley y por interés legítimo en la seguridad. Conforme a los artículos 28 "
                         "y 60 de la Constitución usted puede acceder a sus datos, conocer su uso, pedir su actualización, rectificación o destrucción "
                         "cuando proceda (habeas data), escribiendo a gigadevsc@gmail.com. Responderemos en un plazo máximo de 15 días hábiles."]),
    ("Con quién los compartimos", ["Solo con proveedores necesarios para operar (alojamiento en Render, correo con Brevo, pagos con Binance, mensajería "
                                   "con Meta o WasenderAPI si la empresa lo activa), bajo deber de confidencialidad, y con autoridades cuando una ley o "
                                   "una orden competente lo exija (por ejemplo SENIAT u ONCDOFT). Algunos proveedores están fuera de Venezuela."]),
    ("Seguridad", ["Conexión cifrada (HTTPS), verificación en dos pasos obligatoria, cifrado de secretos, aislamiento de los datos de cada cliente en la base "
                   "de datos (Row-Level Security), bitácora encadenada e inalterable, límites de intentos y respaldos. Notificaremos los incidentes que "
                   "afecten sus datos sin demora indebida."]),
    ("Conservación", ["Conservamos los datos mientras dure el servicio y, después, el tiempo que exijan las leyes tributarias y mercantiles (los "
                      "registros contables y fiscales, al menos durante el lapso de prescripción tributaria). Luego se eliminan de forma segura."]),
    ("Menores", ["Contax® es un servicio para empresas y no está dirigido a menores de edad."]),
]
COOKIES = [
    ("Qué usamos", ["Contax® usa solo cookies TÉCNICAS, imprescindibles para que el servicio funcione con seguridad. No usamos cookies de publicidad, "
                    "de seguimiento ni de analítica de terceros."]),
    ("Detalle", ["«__Host-erp» (sesión): mantiene su sesión iniciada; caduca a los 30 minutos sin actividad y como máximo a las 8 horas. Incluye la "
                 "protección contra falsificación de solicitudes (CSRF).",
                 "«__Host-erp-equipo» (equipo autorizado): identifica el navegador como uno de los equipos permitidos por su licencia; en nuestra base solo "
                 "se guarda su huella SHA-256.",
                 "Ambas son seguras (solo HTTPS), no accesibles desde JavaScript (HttpOnly) y exclusivas de nuestro dominio."]),
    ("Cómo controlarlas", ["Puede borrarlas desde la configuración de su navegador; si lo hace, deberá iniciar sesión de nuevo y el navegador podría "
                           "requerir una nueva autorización como equipo. Como son estrictamente necesarias, no se pueden desactivar sin dejar de usar "
                           "el servicio."]),
    ("Sitio de Gigadevs", ["El sitio informativo de Gigadevs no instala cookies propias; los botones hacia WhatsApp, Instagram o Facebook abren esos "
                           "servicios, que aplican sus propias políticas."]),
]
PAGINAS = {"terminos": ("Términos y condiciones", TERMINOS), "privacidad": ("Política de privacidad", PRIVACIDAD), "cookies": ("Política de cookies", COOKIES)}
