# Maestro multi producto

El ERP conserva su flujo comercial y de licencias. El apartado **Maestro multi producto** agrega suscripciones para Criteriseg, Bot_Whatsapp y Control financiero; cada producto conserva unidad licenciada y colores propios. Los planes nuevos quedan en **Cotizar** hasta definir sus precios.

## Tipos de suscripción

- **CriteriSeg**: organización, con despliegue en nube o cerrado. Este producto sustituye a SICAS en el catálogo comercial.
- **Enlace MIT**: organización, con despliegue en nube o cerrado. Para instalaciones cerradas, el cliente debe publicar el servicio solo en su red privada/VPN o detrás de firewall; el campo de entorno no configura la red por sí mismo.
- **Quincena VE**: cuenta individual. La referencia de sujeto es el correo verificado de la cuenta y el máximo de usuarios se fija en uno.

## Firma y despliegue de licencias

El servicio de licencias del ERP firma el contenido de cada suscripción con Ed25519. Configure su clave privada en el servicio maestro y distribuya únicamente la clave pública, en base64url, como `PRODUCT_LICENSE_PUBLIC_KEY` en cada producto. El token exportado desde Maestro tiene formato `payload.firma`; no distribuya la clave privada a los productos licenciados.

- **CriteriSeg**: `PRODUCT_LICENSE_ENFORCE=1`, `PRODUCT_LICENSE_PUBLIC_KEY=<clave pública>` y `PRODUCT_LICENSE_SUBJECT=<referencia de organización>`. Para un token con entorno `cerrado`, defina `PRODUCT_LICENSE_CLOSED_CIDRS` con las redes CIDR autorizadas, separadas por comas.
- **Enlace MIT**: `PRODUCT_LICENSE_ENFORCE=1`, la misma variable de clave pública y `PRODUCT_LICENSE_SUBJECT=<referencia de organización>`. Configure también `DEBUG=False` y `ALLOWED_HOSTS` con los hosts internos necesarios. Para entorno `cerrado`, configure `PRODUCT_LICENSE_CLOSED_CIDRS`; el perímetro VPN/firewall también debe impedir conexiones externas y, si hay proxy inverso, filtrar allí las IP públicas antes de llegar a la aplicación.
- **Quincena VE**: acepta `PRODUCT_LICENSE_PUBLIC_KEY=<clave pública>` o consulta la clave pública de firma publicada por `PRODUCT_LICENSE_SERVER_URL` (por defecto `https://erp-licencias.onrender.com`). Para exigir licencia, configure `CONTROL_FINANCIERO_LICENSE_ENFORCE=1`; queda desactivada por defecto para no interrumpir cuentas existentes. El titular pega el token en **Más → Licencia individual**. La activación requiere correo verificado y referencia sujeta al correo de la cuenta; `GET /api/license/status` informa el estado.

Una licencia de entorno cerrado es un token firmado y limitado por vencimiento. CriteriSeg y Enlace MIT también limitan ese entorno a las redes CIDR configuradas. Una suspensión hecha desde el Maestro no puede revocar inmediatamente un token que ya esté aislado de la red; el bloqueo se aplicará cuando expire o cuando el despliegue cerrado consulte nuevamente al servidor. Planifique vencimientos y renovación acordes al intervalo de conexión permitido.

## Operación

Desde el panel ERP, el rol `plataforma` puede revisar los productos, crear/activar planes, registrar suscripciones, cambiar estado, entorno, fechas y límites, y descargar la licencia firmada. El panel conserva el ERP como producto existente y registra las suscripciones de los otros sistemas en las tablas `lic_productos`, `lic_planes_producto` y `lic_suscripciones` del almacén de licencias.

## Selector de sistemas y módulos por sistema

Al iniciar sesión, el Maestro solo ve el selector de sistemas, sin menú lateral. Al elegir un sistema, el menú lateral muestra solo los módulos de ese sistema (registro en `app/sistemas.py`), y la elección queda en la sesión.

- **Contax** (central): clientes, licencias, cobros y precios, presupuestos, contratos, facturación, Gigadevs en Contax, normativa legal y plataforma.
- **CriteriSeg, Enlace MIT y Quincena VE**: suscripciones y planes, presupuestos, contratos y facturación propios, y la normativa de Contax.

Cada sistema emite **sus propios** presupuestos, contratos y facturas (columna `sistema` en `lic_presupuestos`, `lic_contratos` y `lic_facturas`; los registros anteriores son de Contax). Todos usan el mismo emisor (Gigadevs Corp), la misma serie y los mismos números de control, y las mismas cláusulas legales de Contax, que nombran el sistema contratado.

## Gigadevs en Contax

Contax lleva la contabilidad, la administración y la parte fiscal de Gigadevs con una licencia **All in One de pago único** (10 años, el máximo del servidor, renovable sin costo). Configure en `erp-web` la variable `GIGADEVS_USUARIO` con el nombre de usuario o correo responsable; luego emita la licencia desde **Contax → Gigadevs en Contax**.

Cada sistema tiene su logo en `app/static/marca/` (SVG para la app y PNG de 192 px para correos y documentos). El de Enlace MIT viene del repositorio bot_whatsapp. Presupuestos, contratos y facturas llevan el logo, el color y el nombre del sistema que vende; el emisor sigue siendo Gigadevs Corp.

## Usuarios del cliente: puestos y módulos

En Organización → Usuarios, el administrador invita a un usuario o, si el usuario o el correo ya existen en su organización, mantiene la cuenta y actualiza su puesto, sus módulos y sus empresas. Hay 17 puestos (Gerente General, Contador, Analista Fiscal, Tesorero, Facturador, Almacenista, Vendedor, Consulta, etc.), cada uno con su matriz de permisos en `app/security.py`. Los módulos marcados por usuario (columna `usuarios.modulos`, migración 012) acotan dónde puede trabajar; el plan de la licencia sigue mandando. El Administrador siempre tiene todo.

## Surtia

Surtia (repositorio Bot_Integrador) es un producto más del Maestro: suscripción por empresa (sujeto = RIF registrado en Surtia), plan «empresa» en modalidad Cotizar y módulos (bot de pedidos, presupuestos, conversaciones, órdenes de compra en PDF, integración con el sistema administrativo y avisos por correo). La licencia se carga en Surtia → Consola → Empresas; `SURTIA_LICENSE_ENFORCE=1` activa el control.

## Correos con la marca de cada sistema

Los correos que el servidor de licencias envía por un sistema (presupuesto, contrato para firmar, factura) usan la plantilla de ese sistema: su logo, su color, su nombre en el asunto y su pie («un sistema de Gigadevs Corp · normativa legal de Contax®»). La marca de cada sistema está en `MARCAS` de `app/plantilla_correo.py`.
