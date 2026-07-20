# Shop Ultra Bot — paquete actualizado para GitHub y Render

> Para trasladar una tienda que ya está funcionando, comienza por `PASOS_RAPIDOS_GITHUB_RENDER.txt` y `GUIA_RENDER_GITHUB.md`. Usa `COPIAR_DATOS_DEL_BOT_ANTERIOR.bat` para copiar la base local y `PUBLICAR_EN_GITHUB.bat` para subir únicamente el código. Nunca publiques `.env`, `data/shop.db`, `data/providers.json` ni `MIGRACION_RENDER`.

## Corrección del panel administrativo de productos

El panel `/admin → Productos` ahora utiliza páginas internas de 35 elementos.
Esto evita el error `Bad Request: reply markup is too long` cuando la base contiene
muchos productos locales, externos, activos e inactivos. Los productos locales se
muestran primero para facilitar la carga de stock.

La tienda pública no cambia y mantiene el catálogo continuo configurado para los
clientes. Además, el sistema ya no elimina el mensaje anterior antes de comprobar
que Telegram aceptó el nuevo teclado.

La guía específica está en:

```text
ACTUALIZAR_PANEL_PRODUCTOS_SIN_PERDER_DATOS.txt
```


Para desplegar este proyecto en Render y trasladar una tienda en funcionamiento sin perder clientes, saldos, stock ni historial, lee primero [GUIA_RENDER_GITHUB.md](GUIA_RENDER_GITHUB.md).

Archivos principales:

- `render.yaml`: Blueprint de Render.
- `render_entrypoint.py`: espera e importa la base anterior antes de iniciar Telegram.
- `PREPARAR_MIGRACION_RENDER.bat`: genera una copia coherente de SQLite y el `.env` para Render.
- `SUBIR_DATA_A_RENDER.bat`: transfiere la base al disco persistente mediante SCP.
- `PUBLICAR_EN_GITHUB.bat`: publica la carpeta completa sin incluir secretos ni DATA.

---

# Shop Ultra Bot — entrega con instrucciones, emojis Premium y Multi-API

Bot de tienda para Telegram desarrollado con Python, aiogram 3 y SQLite. Mantiene productos locales y externos en un catálogo unificado, conserva entregas e historial, admite varias conexiones ProdSeller y Canboso Buyer API, y agrega apariencia configurable, emojis Premium, anuncios, reembolsos y ajustes de saldo administrativos.

## Entrega con instrucciones y emojis Premium persistentes

Al completar una compra, el bot muestra primero las instrucciones de activación del
producto y después el correo, cuenta, clave, código o enlace entregado. Las instrucciones
se guardan también en la orden para que vuelvan a aparecer al reenviar la compra desde
Historial, aunque posteriormente se edite el producto.

Las descripciones e instrucciones nuevas conservan las entidades `custom_emoji` de
Telegram. Para configurarlas, envía el texto desde el panel administrativo usando el
selector de emojis Premium. El bot almacena el identificador interno y lo reconstruye
como una entidad animada al mostrar la ficha y la entrega.

Los productos creados con una versión anterior solo contienen el carácter visual del
emoji, no su identificador Premium. Para animarlos, vuelve a guardar su Descripción o
Instrucciones desde `/admin` usando nuevamente el selector Premium.

La guía de actualización específica es:

```text
ACTUALIZAR_ENTREGA_INSTRUCCIONES_EMOJIS_SIN_PERDER_DATOS.txt
```


## Compra por cantidad, stock multilínea y actividad del cliente

Antes de confirmar una compra, el cliente puede elegir la cantidad con botones de
disminuir, aumentar o cantidad personalizada. El bot muestra el precio unitario,
la cantidad seleccionada y el monto total. Las compras múltiples descuentan el
total exacto, entregan todas las unidades y las conservan en el historial.

Para cargar unidades de stock que ocupan varias líneas, separa cada unidad mediante
una línea que contenga únicamente `--`. Si no existe ese delimitador, se conserva
el formato anterior de una unidad por línea.

Cada producto admite descripción e instrucciones opcionales. El botón Ajustes
muestra estadísticas del cliente y el apartado Mis recargas muestra depósitos y
compras pagadas con balance.

La guía específica de actualización es:

```text
ACTUALIZAR_ENTREGA_INSTRUCCIONES_EMOJIS_SIN_PERDER_DATOS.txt
```

## Novedades de esta actualización

- Selector de cantidad antes de comprar, con botones `-1`, `+1`, compra por cantidad y entrada personalizada.
- Compra múltiple para productos locales y proveedores API compatibles.
- Stock multilínea separado mediante una línea que contenga únicamente `--`.
- Descripción e instrucciones mostradas en bloques separados; las instrucciones son opcionales.
- Ajustes funcionales con estadísticas, recargas acreditadas y compras pagadas con el wallet.
- Se conservan el catálogo sin páginas, la personalización visual, los emojis Premium y las integraciones Multi-API.

La guía específica para conservar todos los datos está en:

```text
ACTUALIZAR_ENTREGA_INSTRUCCIONES_EMOJIS_SIN_PERDER_DATOS.txt
```

## Catálogo público completo en una sola lista

La tienda pública ya no divide los productos en páginas. Todos los productos activos,
sean locales o de proveedores API, se consultan y se muestran en un único teclado:

- no aparece el contador `1/2`;
- no aparecen flechas de navegación;
- cada producto conserva su emoji Premium, color, precio y disponibilidad;
- los botones antiguos `shop:1`, `shop:2`, etc. abren igualmente la lista completa;
- el panel administrativo conserva su propia paginación para tareas de gestión.

Esta modificación no cambia la estructura de `data/shop.db` y no requiere convertir
la base de datos.

## Datos que no debes perder

La información activa del bot no está dentro del código Python. Está en:

```text
.env
    Token, administradores, Binance y configuración general.

data/shop.db
    Usuarios, saldos, productos, stock, compras, entregas, historial y depósitos.

data/shop.db-wal
data/shop.db-shm
    Archivos auxiliares de SQLite. Si existen, se copian junto con shop.db.

data/providers.json
    Proveedores externos, URLs y API Keys.
```

No borres ni reemplaces `.env` ni `data/` al actualizar una instalación que ya tiene clientes.


## Actualización recomendada para esta versión

Lee primero:

```text
ACTUALIZAR_ENTREGA_INSTRUCCIONES_EMOJIS_SIN_PERDER_DATOS.txt
```

La forma más segura de actualizar un bot en funcionamiento es:

1. detener completamente la instalación anterior;
2. ejecutar `verificar_datos.bat` y `respaldar_datos.bat`;
3. descomprimir este proyecto en una carpeta nueva;
4. copiar desde el bot anterior únicamente `.env` y toda la carpeta `data\`;
5. ejecutar `instalar_requerimientos.bat` y después `iniciar_bot.bat`;
6. volver a ejecutar `verificar_datos.bat` y comparar las cantidades.

La guía específica para esta actualización está en:

```text
ACTUALIZAR_ENTREGA_INSTRUCCIONES_EMOJIS_SIN_PERDER_DATOS.txt
```

No copies el código, el entorno virtual ni los BAT antiguos sobre esta versión. No ejecutes dos instalaciones simultáneamente con el mismo token.

## Apariencia, botones y emojis Premium

Ruta administrativa:

```text
/admin → Apariencia y emojis
```

El panel permite:

- modificar el texto de los botones en español e inglés;
- asignar un emoji normal o un custom emoji de Telegram Premium;
- seleccionar estilo azul, verde, rojo o predeterminado;
- editar los mensajes públicos y apartados principales;
- conservar formato HTML, enlaces y custom emojis en los mensajes;
- activar o desactivar una vista animada en el menú y el catálogo;
- probar un emoji Premium dentro de un mensaje y de un botón real;
- restaurar la apariencia sin tocar usuarios, saldos, productos, ventas o APIs.

Cada producto incluye además:

```text
/admin → Productos → producto → Emoji
/admin → Productos → producto → Color del botón
```

Al abrir una ficha se puede mostrar una foto, GIF, sticker animado o sticker de video. Cuando el producto usa un custom emoji, el bot lo incorpora al botón y también lo representa como entidad animada dentro del texto del menú, del catálogo y de la ficha.

Telegram limita el estilo de los botones a azul (`primary`), verde (`success`), rojo (`danger`) o el estilo predeterminado. No existen colores HEX arbitrarios para estos botones. La reproducción del icono dentro del botón depende del cliente de Telegram; por eso la versión incluye la representación animada adicional dentro del mensaje. Un sticker animado normal no puede incrustarse en un botón, pero sí enviarse como media al abrir el producto.

## Actualización en una carpeta nueva

1. Detén completamente el bot anterior.
2. Descomprime el ZIP completo en una carpeta nueva.
3. Copia manualmente desde la instalación anterior:

```text
.env
toda la carpeta data/
```

4. No copies `.venv` ni carpetas `__pycache__`.
5. Ejecuta `instalar_requerimientos.bat`.
6. Ejecuta `iniciar_bot.bat` en la carpeta nueva.
7. Ejecuta `verificar_datos.bat`.
8. No ejecutes simultáneamente ambas instalaciones con el mismo token.

Este método evita depender de `migrar_datos_del_bot_anterior.bat`.

## Catálogo unificado

Los clientes ven una única lista con productos locales y productos externos. No se muestra el proveedor, el costo ni la frase “Disponible vía API”. El cliente solo ve nombre, precio y disponibilidad.

El administrador sí puede ver:

```text
origen/proveedor
costo externo
stock externo
ID del producto externo
última sincronización
```

Los precios públicos de los productos externos pueden modificarse desde `/admin` y no son sobrescritos por sincronizaciones posteriores.

## Múltiples proveedores API

Ejecuta:

```text
configurar_apis.bat
```

Cada proveedor tiene:

```text
nombre
código interno único
Base URL
API Key
encabezado de autenticación
margen inicial
intervalo de sincronización
caché y timeout
```

Las conexiones se guardan en:

```text
data/providers.json
```

No cambies el código interno de un proveedor después de vender productos vinculados a él.

La versión incluida implementa dos adaptadores:

```text
prodseller_v1
    GET  /products
    GET  /products/{id}
    GET  /balance
    POST /orders
    GET  /orders/{id}

canboso_buyer_v1
    GET  /api/telegram-buyer/products?key=...
    GET  /api/telegram-buyer/balance?key=...
    POST /api/telegram-buyer/purchase
```

Canboso admite productos que solicitan el correo del cliente y una duración. El bot pide esos datos antes de confirmar la compra, calcula el total según los meses seleccionados y guarda la entrega en el historial. Una API con endpoints o respuestas JSON diferentes necesita otro adaptador específico.

### Configurar Canboso

1. Ejecuta `configurar_apis.bat`.
2. Pulsa **Nuevo**.
3. En **Tipo de API**, selecciona `canboso_buyer_v1`.
4. Usa un nombre, por ejemplo `Canboso`, y un código interno estable, por ejemplo `canboso`.
5. Base URL: `https://canboso.com`.
6. Pega la API Key nueva con formato `tgb_...`.
7. Guarda, reinicia con `iniciar_bot.bat` y abre `/admin → Proveedores API`.
8. Ejecuta **Probar conexión**, **Sincronizar catálogo** y luego **Seleccionar productos**.

La clave se guarda únicamente en `data/providers.json`. No la pegues en el código ni compartas ese archivo.

## Selección y avisos de productos externos

Ruta:

```text
/admin → Proveedores API → proveedor
```

Funciones:

- sincronizar catálogo;
- seleccionar productos uno por uno;
- activar todos los productos disponibles;
- cambiar precio, nombre, descripción, emoji, imagen y duración;
- activar o desactivar publicación automática de nuevos productos.

Comportamiento de las notificaciones:

- Al activar manualmente un producto externo disponible, se avisa a los usuarios.
- Al activar varios productos, se envía un aviso unificado.
- Si un producto activo recupera disponibilidad, se puede avisar como reposición.
- Si el proveedor agrega productos y la publicación automática está desactivada, se avisa a los administradores y los productos permanecen ocultos.
- Si la publicación automática está activada, los productos nuevos disponibles se publican y se avisa a los usuarios.

La sincronización solo actualiza los campos pertenecientes al proveedor:

```text
costo
stock
disponibilidad
imagen remota
fecha de sincronización
```

Nunca sobrescribe:

```text
precio de venta
nombre visible
descripción
emoji
foto, GIF o sticker local
estado activo/inactivo de productos existentes
```

## Anuncios a todos los usuarios

Ruta:

```text
/admin → Anuncios
```

El administrador escribe el contenido, revisa una vista previa y confirma. El bot registra el envío y reporta:

```text
usuarios revisados
enviados
bloqueados/no disponibles
fallidos
```

Cada anuncio incluye un botón para abrir la tienda.

## Reembolsos completos y prorrateados

Ruta:

```text
/admin → Reembolsos y saldo → Reembolsar una compra
```

La compra se puede buscar por:

```text
ID numérico de Telegram
código de orden
```

Para un servicio de 30 días comprado por 20 USDT y utilizado durante 15 días:

```text
20 × (30 - 15) / 30 = 10 USDT
```

El bot calcula el monto no utilizado, resta cualquier reembolso previo, acredita el saldo y registra la operación. También existe reembolso total del monto pendiente.

Para guardar la duración de un producto:

```text
/admin → Productos → producto → Duración
```

La duración puede configurarse para productos locales o externos.

El bot no puede determinar por sí mismo cuántos días utilizó realmente un cliente. El administrador introduce los días usados; el cálculo monetario y las validaciones sí son automáticos.

Los reembolsos no eliminan la entrega ni la compra del historial. Se conserva la trazabilidad y se marca el monto reembolsado.

## Dar o descontar saldo por ID de Telegram

Ruta:

```text
/admin → Reembolsos y saldo → Dar o ajustar saldo
```

El administrador introduce:

```text
ID de Telegram
monto positivo o negativo
motivo
```

El sistema evita dejar el saldo por debajo de cero y registra saldo anterior, ajuste, saldo posterior, administrador y motivo.

## Historial del cliente

La sección Historial conserva:

- compras locales y externas;
- contenido entregado;
- botón para reenviar una entrega;
- depósitos;
- reembolsos aplicados;
- créditos y débitos administrativos recientes.

Los correos, enlaces, cuentas o claves se envían en mensajes independientes y no se eliminan al navegar por el bot.

## Stock local masivo y bloques multilínea

Desde la ficha de un producto local puedes pegar miles de unidades o adjuntar un archivo `.txt`, `.csv` o `.log`. Se omiten duplicados y se informa a los usuarios cuando se agrega stock a un producto activo.

Para códigos simples, usa un elemento por línea. Para cuentas o entregas que ocupen varias líneas, separa cada unidad con una línea que contenga únicamente `--`:

```text
correo1@ejemplo.com
clave1
nota de la primera cuenta
--
correo2@ejemplo.com
clave2
nota de la segunda cuenta
```

El ejemplo agrega exactamente dos unidades de stock.

## Selección de cantidad y ficha del producto

Al pulsar **Comprar**, el cliente puede disminuir o aumentar la cantidad, introducir una cantidad personalizada y revisar el total antes de confirmar. El bot valida el saldo y el stock exacto. La ficha del producto separa **Descripción** e **Instrucciones**; las instrucciones son opcionales y se editan desde `/admin → Productos → producto → Instrucciones`.

## Ajustes, estadísticas y actividad

El botón **Ajustes** muestra órdenes, ítems comprados, total gastado, última orden y total recargado. **Mis recargas** muestra tanto las recargas acreditadas mediante Binance Pay como las compras pagadas con el balance del wallet.

## Compras externas

Antes de cada compra, el bot vuelve a consultar disponibilidad y costo. Después:

1. reserva el saldo local del cliente;
2. crea la orden externa;
3. recibe la clave, correo, enlace o conjunto de claves;
4. guarda la compra y el pedido externo;
5. entrega el contenido en un mensaje independiente;
6. lo deja disponible en Historial.

Errores explícitos de autenticación, saldo del proveedor, producto inexistente, falta de stock o límite de solicitudes devuelven el saldo local. Una caída de conexión después de enviar un pedido queda en revisión manual para evitar una compra duplicada. Canboso no publica un endpoint de consulta de estado en la documentación suministrada; por ello, una respuesta ambigua debe revisarse en el panel del proveedor antes de reembolsar.

Ruta de revisión:

```text
/admin → Proveedores API → proveedor → Pedidos pendientes
```

## Instalación limpia

1. Ejecuta `configurar.bat`.
2. Completa token, administradores, tienda y Binance.
3. Ejecuta `configurar_apis.bat` para proveedores externos.
4. Ejecuta `iniciar_bot.bat`.

## Respaldo y verificación

`respaldar_datos.bat` crea una carpeta con fecha dentro de `backups/`. La base se copia con la API de respaldo de SQLite y se valida con `PRAGMA integrity_check`.

`verificar_datos.bat` muestra:

```text
usuarios
saldo total
productos activos
stock local disponible
compras e historial
depósitos
pedidos externos
reembolsos
ajustes de saldo
anuncios
integridad SQLite
```

## Archivos principales

```text
.env                                      configuración privada
data/shop.db                              base de datos principal
data/providers.json                       proveedores y API Keys
app/                                      código del bot
tools/backup_bot_data.py                  respaldo seguro
tools/inspect_database.py                 verificación de datos
ACTUALIZAR_ENTREGA_INSTRUCCIONES_EMOJIS_SIN_PERDER_DATOS.txt guía de esta versión
```

## Validación local

```bash
python -m pytest -q
python -m ruff check .
python -m compileall -q app tools tests
```

Las pruebas usan bases temporales y servidores API simulados. La operación real depende del token, las API Keys, el saldo del proveedor y la disponibilidad de los servicios externos. La integración Canboso se implementó contra la documentación Buyer API 1.2.0 entregada por el proveedor.
