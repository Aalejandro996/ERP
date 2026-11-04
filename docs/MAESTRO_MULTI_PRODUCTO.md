# Maestro multi producto

El ERP conserva su flujo comercial y de licencias. El apartado **Maestro multi producto** agrega suscripciones para Criteriseg, Bot_Whatsapp y Control financiero; cada producto conserva unidad licenciada y colores propios. Los planes nuevos quedan en **Cotizar** hasta definir sus precios.

## Tipos de suscripción

- **Criteriseg**: organización, con despliegue en nube o cerrado. Este producto sustituye a SICAS en el catálogo comercial.
- **Bot_Whatsapp**: organización, con despliegue en nube o cerrado. Para instalaciones cerradas, el cliente debe publicar el servicio solo en su red privada/VPN o detrás de firewall; el campo de entorno no configura la red por sí mismo.
- **Control financiero**: cuenta individual. La referencia de sujeto es el correo verificado de la cuenta y el máximo de usuarios se fija en uno.

## Firma y despliegue de licencias

El servicio de licencias del ERP firma el contenido de cada suscripción con Ed25519. Configure su clave privada en el servicio maestro y distribuya únicamente la clave pública, en base64url, como `PRODUCT_LICENSE_PUBLIC_KEY` en cada producto. El token exportado desde Maestro tiene formato `payload.firma`; no distribuya la clave privada a los productos licenciados.

- **Criteriseg**: `PRODUCT_LICENSE_ENFORCE=1`, `PRODUCT_LICENSE_PUBLIC_KEY=<clave pública>` y `PRODUCT_LICENSE_SUBJECT=<referencia de organización>`. Para un token con entorno `cerrado`, defina `PRODUCT_LICENSE_CLOSED_CIDRS` con las redes CIDR autorizadas, separadas por comas.
- **Bot_Whatsapp**: `PRODUCT_LICENSE_ENFORCE=1`, la misma variable de clave pública y `PRODUCT_LICENSE_SUBJECT=<referencia de organización>`. Configure también `DEBUG=False` y `ALLOWED_HOSTS` con los hosts internos necesarios. Para entorno `cerrado`, configure `PRODUCT_LICENSE_CLOSED_CIDRS`; el perímetro VPN/firewall también debe impedir conexiones externas y, si hay proxy inverso, filtrar allí las IP públicas antes de llegar a la aplicación.
- **Control financiero**: `PRODUCT_LICENSE_PUBLIC_KEY=<clave pública>` y `CONTROL_FINANCIERO_LICENSE_ENFORCE=1` para exigir licencia a las cuentas. La cuenta activa su token firmado en `POST /api/license/activate` con `{ "token": "payload.firma" }`; requiere una sesión autenticada y correo verificado. `GET /api/license/status` informa el estado. La exigencia queda desactivada por defecto para no interrumpir instalaciones existentes.

Una licencia de entorno cerrado es un token firmado y limitado por vencimiento. Criteriseg y Bot_Whatsapp también limitan ese entorno a las redes CIDR configuradas. Una suspensión hecha desde el Maestro no puede revocar inmediatamente un token que ya esté aislado de la red; el bloqueo se aplicará cuando expire o cuando el despliegue cerrado consulte nuevamente al servidor. Planifique vencimientos y renovación acordes al intervalo de conexión permitido.

## Operación

Desde el panel ERP, el rol `plataforma` puede revisar los productos, crear/activar planes, registrar suscripciones, cambiar estado, entorno, fechas y límites, y descargar la licencia firmada. El panel conserva el ERP como producto existente y registra las suscripciones de los otros sistemas en las tablas `lic_productos`, `lic_planes_producto` y `lic_suscripciones` del almacén de licencias.
