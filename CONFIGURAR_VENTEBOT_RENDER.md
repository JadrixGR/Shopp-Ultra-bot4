# Configurar VenteBot Reseller API en Render

Esta integración usa VenteBot Reseller API 1.2 y autentica mediante
`X-Reseller-Key`. La clave se guarda como variable privada de Render y nunca debe
escribirse en GitHub.

## 1. Publicar el código

Desde CMD:

```cmd
cd /d "D:\PROYECTOS TELEGRAM\TIENDA TELEGRAM\Github2\shop_ultra_bot_render_github_actualizado"
git add app tools tests .env.example .env.render.example data/providers.example.json README.md CONFIGURAR_VENTEBOT_RENDER.md
git commit -m "Agregar proveedor VenteBot Reseller API"
git push origin main
```

## 2. Crear las variables privadas

En Render abre el `Background Worker` del bot y entra en:

```text
Environment → Add Environment Variable
```

Agrega:

```text
VENTEBOT_ENABLED=true
VENTEBOT_BASE_URL=https://ventetelegrambotrailway-production.up.railway.app
VENTEBOT_API_KEY=PEGA_AQUI_UNA_CLAVE_NUEVA
VENTEBOT_MARKUP_PERCENT=20
```

Opcionalmente puedes ajustar:

```text
VENTEBOT_AUTO_SYNC_MINUTES=10
VENTEBOT_CACHE_SECONDS=60
VENTEBOT_TIMEOUT_SECONDS=20
VENTEBOT_ALLOW_BELOW_COST=false
VENTEBOT_ORDER_POLL_ATTEMPTS=4
VENTEBOT_ORDER_POLL_DELAY_SECONDS=2
```

No agregues `/api/swagger`, `/api/reseller` ni `/openapi.json` al final de la Base
URL. El adaptador agrega internamente `/api/reseller`.

## 3. Desplegar

Pulsa `Save Changes`. Si Render no inicia automáticamente un despliegue, usa:

```text
Manual Deploy → Deploy latest commit
```

Al arrancar, el bot sincroniza esas variables con el archivo privado persistente:

```text
/var/data/providers.json
```

No borra ni reemplaza los demás proveedores configurados.

## 4. Probar desde Telegram

En el bot entra en:

```text
/admin → Proveedores API → VenteBot
```

Ejecuta, en este orden:

1. `Probar conexión` y comprueba balance, usuario y productos.
2. `Sincronizar catálogo`.
3. `Seleccionar productos` y activa solo los que deseas vender.
4. Revisa y modifica el precio público de cada producto si es necesario.

La tienda muestra el stock numérico entregado por VenteBot. Antes de comprar, el
bot vuelve a consultar el catálogo, solicita una cotización exacta, reserva el saldo
del cliente y crea una orden con una clave de idempotencia. Las entregas de
`account_data` se guardan en la compra y quedan disponibles en el historial.

Los productos cuyo `delivery_type` requiere activación pedirán primero el correo,
usuario o identificador que debe recibir la activación.

## 5. Verificación segura

Comprueba que la billetera del reseller tenga fondos antes de habilitar ventas. Haz
una primera compra con un producto económico y un usuario de prueba. Revisa en los
logs de Render que no haya respuestas `401`, `402`, `409` o `429`.

La API limita cada clave a 60 solicitudes por 60 segundos. La caché y la
sincronización incluidas respetan ese límite en el funcionamiento normal.

Si una clave fue compartida en un chat o captura, genera una nueva en VenteBot y
actualiza únicamente `VENTEBOT_API_KEY` en Render.
