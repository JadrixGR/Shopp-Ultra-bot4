# Shop Ultra Bot: migración segura a GitHub y Render

Esta edición contiene el código más reciente del bot, incluida la corrección de `ProviderRegistry`, y está preparada para ejecutarse como **Background Worker** de Render con SQLite en un **disco persistente**.

## Regla de seguridad

El repositorio mostrado es público. GitHub debe contener únicamente el código. Nunca deben publicarse:

```text
.env
data/shop.db
data/providers.json
MIGRACION_RENDER/
```

El archivo `.gitignore` ya excluye estos elementos cuando se usa Git. El script `PUBLICAR_EN_GITHUB.bat` comprueba adicionalmente que no estén registrados ni preparados para un commit.

No utilice el botón web **Upload files** para cargar manualmente `.env`, `data` o `MIGRACION_RENDER`. Ese método puede omitir las protecciones del flujo preparado.

## Archivos incluidos para la migración

```text
COPIAR_DATOS_DEL_BOT_ANTERIOR.bat
PREPARAR_MIGRACION_RENDER.bat
PUBLICAR_EN_GITHUB.bat
SUBIR_DATA_A_RENDER.bat
render.yaml
render_entrypoint.py
```

## 1. Detener y respaldar el bot actual

1. Pulse `Ctrl+C` en la consola del bot anterior.
2. Cierre la ventana.
3. Compruebe que no quede `python.exe` o `pythonw.exe` ejecutando el bot.
4. Ejecute `verificar_datos.bat` y guarde una captura.
5. Ejecute `respaldar_datos.bat`.
6. Duplique manualmente toda la carpeta del bot anterior.

No copie una base SQLite mientras el bot continúa escribiendo en ella.

## 2. Copiar la información al proyecto nuevo

Descomprima el ZIP en una carpeta nueva y ejecute:

```text
COPIAR_DATOS_DEL_BOT_ANTERIOR.bat
```

Indique la carpeta principal del bot anterior. La herramienta:

- copia `.env`;
- crea una copia coherente de `data/shop.db` mediante la API de respaldo de SQLite;
- copia y valida `data/providers.json`;
- compara usuarios, saldos, productos, stock, compras y depósitos;
- no copia los archivos temporales `shop.db-wal` y `shop.db-shm`.

### Información conservada

`data/shop.db` contiene:

- usuarios e IDs de Telegram;
- saldos;
- productos locales y externos;
- precios personalizados;
- stock disponible y vendido;
- compras e historial;
- cuentas, correos, contraseñas, códigos y enlaces entregados;
- depósitos y recargas;
- reembolsos y ajustes de saldo;
- anuncios;
- descripciones, instrucciones y emojis Premium;
- colores, textos y apariencia personalizada.

`data/providers.json` contiene:

- proveedores API;
- URLs y claves;
- márgenes;
- sincronización automática;
- selección y publicación de productos.

`.env` contiene:

- token del bot;
- administradores;
- Binance Pay y sus credenciales;
- nombre de la tienda y soporte;
- configuración operativa.

## 3. Probar la versión nueva localmente

Ejecute:

```text
instalar_requerimientos.bat
iniciar_bot.bat
```

Compruebe al menos:

- `/start`;
- `/admin`;
- `Productos` y `Agregar stock`;
- saldo de un cliente;
- historial de una compra anterior;
- proveedores API;
- emojis y apariencia.

Detenga el bot al finalizar. No deben ejecutarse dos instancias con el mismo token.

## 4. Preparar los datos privados para Render

Ejecute:

```text
PREPARAR_MIGRACION_RENDER.bat
```

Como los datos ya se copiaron a esta carpeta, puede pulsar Enter para usarla como origen. Se genera:

```text
MIGRACION_RENDER/
├── .env.render
├── import_once.zip
├── shop.db
├── providers.json
├── manifest.json
└── INSTRUCCIONES.txt
```

`.env.render` adapta las rutas a:

```text
DATABASE_URL=sqlite+aiosqlite:////var/data/shop.db
API_PROVIDERS_FILE=/var/data/providers.json
```

`import_once.zip` contiene la base y los proveedores para una importación única y validada en el disco persistente.

## 5. Publicar el código en GitHub

Ejecute:

```text
PUBLICAR_EN_GITHUB.bat
```

La URL predeterminada es:

```text
https://github.com/JadrixGR/Shopp-Ultra-bot4.git
```

El script usa Git, por lo que mantiene todas las carpetas (`app`, `tools`, `data` con solo ejemplos, etc.). GitHub sí admite estructuras de carpetas; no es necesario subir cada archivo por separado.

El script se detiene si detecta `.env`, una base `.db`, `providers.json` privado o `MIGRACION_RENDER` en el índice de Git.

## 6. Crear el servicio en Render

1. Abra Render Dashboard.
2. Seleccione **New > Blueprint**.
3. Conecte GitHub y seleccione `JadrixGR/Shopp-Ultra-bot4`.
4. Render detectará `render.yaml` en la raíz.
5. Introduzca `BOT_TOKEN` y `ADMIN_IDS` cuando se soliciten.
6. Confirme la creación.

El Blueprint define:

```text
Tipo: Background Worker
Runtime: Python
Plan: Starter
Disco: 1 GB
Punto de montaje: /var/data
Inicio: python render_entrypoint.py
```

El servicio permanecerá esperando `/var/data/import_once.zip` y no abrirá una tienda vacía antes de importar la base anterior.

## 7. Cargar las variables de entorno

En el servicio de Render:

```text
Environment > Add from .env
```

Seleccione:

```text
MIGRACION_RENDER/.env.render
```

Guarde los cambios. Este archivo es privado y no debe publicarse.

## 8. Subir SQLite y proveedores al disco persistente

Configure una clave SSH en Render y copie el destino desde:

```text
Servicio > Connect > SSH
```

Debe parecerse a:

```text
srv-xxxxxxxx@ssh.oregon.render.com
```

Después ejecute:

```text
SUBIR_DATA_A_RENDER.bat
```

El script transfiere `import_once.zip` a `/var/data`. `render_entrypoint.py`:

1. valida las rutas y el contenido del ZIP;
2. ejecuta `PRAGMA integrity_check`;
3. comprueba las tablas principales;
4. respalda cualquier base previa del disco;
5. reemplaza la base de manera atómica;
6. guarda un recibo de importación;
7. inicia el bot.

## 9. Verificación final

Revise **Logs**. Debe aparecer algo similar a:

```text
[Render] Paquete de migración detectado. Validando e importando...
[Render] Migración completada: usuarios=98, productos=..., compras=..., depósitos=...
[Render] Iniciando Shop Ultra Bot...
Starting @nombre_del_bot
```

Desde la Shell de Render puede ejecutar:

```bash
python tools/inspect_database.py /var/data/shop.db
```

Compare el resultado con la captura del bot anterior y verifique en Telegram:

- usuarios;
- saldo total;
- productos y stock;
- compras e historial;
- entregas anteriores;
- depósitos;
- proveedores API;
- personalización.

## Actualizaciones posteriores

En las actualizaciones futuras, publique solo cambios de código mediante Git. El disco persistente mantiene:

```text
/var/data/shop.db
/var/data/providers.json
```

No vuelva a subir `import_once.zip` salvo que desee reemplazar deliberadamente la base de Render por otra copia.

## Consideración de costo

SQLite requiere que el archivo se encuentre en almacenamiento persistente. El sistema de archivos normal de Render es efímero; por eso esta configuración utiliza un Background Worker pagado con disco persistente. No use una instancia gratuita sin disco para esta base, porque perdería los cambios en reinicios o despliegues.
