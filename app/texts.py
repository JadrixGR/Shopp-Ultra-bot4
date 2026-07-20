from __future__ import annotations

import logging
from string import Formatter

TEXTS: dict[str, dict[str, str]] = {
    "es": {
        "welcome": "🔥 <b>Bienvenido a {store}</b>\n\n🔊 Tu balance: <b>${balance}</b>",
        "menu_store": "🎯 Tienda",
        "menu_wallet": "💰 Recargar Wallet",
        "menu_settings": "⚡ Ajustes",
        "menu_support": "🔔 Soporte",
        "menu_history": "🧿 Historial",
        "menu_language": "🌐 Lenguaje",
        "menu_admin": "🛠 Administración",
        "back_menu": "❌ Volver al menú",
        "back": "❌ Volver",
        "refresh": "🔄 Actualizar",
        "shop_title": "📊 <b>Elige tu producto:</b>",
        "menu_animated_preview_title": "<b>Accesos animados</b>",
        "store_animated_preview_title": "<b>Vista animada del catálogo</b>",
        "shop_empty": "📭 No hay productos disponibles en este momento.",
        "product_header": "🛡️ <b>Productos disponibles</b>",
        "product_body": (
            "{emoji} <b>{name}</b>\n\n"
            "🟢 Producto oficial de la tienda\n"
            "⚡ Entrega automática instantánea\n\n"
            "<blockquote>🛡️ <b>Descripción</b>\n{description}</blockquote>"
            "{instructions_block}\n\n"
            "📊 Stock disponible: <b>{stock}</b>\n"
            "💵 Precio: <b>${price}</b>"
        ),
        "quantity_selector": (
            "{emoji} <b>{name}</b>\n\n"
            "💰 Precio base: <b>{unit_price} USDT</b>\n"
            "📊 Stock disponible: <b>{stock}</b>\n\n"
            "🤝 Cantidad seleccionada: <b>{quantity}</b>\n"
            "🔊 Monto total: <b>{total} USDT</b>\n\n"
            "Selecciona la cantidad"
        ),
        "quantity_prompt": (
            "➕ <b>Cantidad personalizada</b>\n\n"
            "Envía un número entero entre <b>1</b> y <b>{maximum}</b>."
        ),
        "quantity_invalid": "❌ Cantidad inválida. Envía un número entre 1 y {maximum}.",
        "buy": "🛒 COMPRAR | ${price}",
        "sold_out": "🔴 AGOTADO",
        "buy_confirm": (
            "🧾 <b>Confirmar compra</b>\n\n"
            "Producto: <b>{name}</b>\n"
            "Precio: <b>${price}</b>\n"
            "Tu balance: <b>${balance}</b>\n\n"
            "La entrega se realizará inmediatamente después de confirmar."
        ),
        "confirm": "✅ Confirmar compra",
        "purchase_success": (
            "✅ <b>Compra completada</b>\n\n"
            "Orden: <code>{order}</code>\n"
            "Producto: <b>{name}</b>\n"
            "Pagado: <b>${price}</b>\n"
            "Balance restante: <b>${balance}</b>\n\n"
            "{instructions_block}"
            "📦 <b>Tu producto:</b>\n<pre>{payload}</pre>\n\n"
            "Guarda este mensaje en un lugar seguro."
        ),
        "purchase_success_file": (
            "✅ <b>Compra completada</b>\n\n"
            "Orden: <code>{order}</code>\n"
            "Producto: <b>{name}</b>\n"
            "Pagado: <b>${price}</b>\n"
            "Balance restante: <b>${balance}</b>\n\n"
            "{instructions_block}"
            "📎 El producto se adjunta como archivo de texto por su longitud."
        ),
        "purchase_confirmed_summary": (
            "✅ <b>Compra confirmada</b>\n\n"
            "La entrega fue enviada en un mensaje separado y no será reemplazada por el menú."
        ),
        "purchase_continue": "El producto también quedó guardado en <b>Historial</b>.",
        "history_delivery": (
            "📦 <b>Entrega guardada</b>\n\n"
            "Orden: <code>{order}</code>\n"
            "Producto: <b>{name}</b>\n"
            "Pagado: <b>${price}</b>\n"
            "Fecha: <b>{date}</b>\n\n"
            "{instructions_block}"
            "<b>Contenido adquirido:</b>\n<pre>{payload}</pre>"
        ),
        "history_delivery_file": (
            "📦 <b>Entrega guardada</b>\n\n"
            "Orden: <code>{order}</code>\n"
            "Producto: <b>{name}</b>\n"
            "Pagado: <b>${price}</b>\n"
            "Fecha: <b>{date}</b>\n\n"
            "{instructions_block}"
            "El contenido se adjunta como archivo de texto."
        ),
        "history_order_not_found": "No se encontró esa compra en tu historial.",
        "history_resend_notice": "Entrega enviada nuevamente.",
        "product_file_caption": "📦 Producto de la orden <code>{order}</code>",
        "insufficient": "❌ Balance insuficiente. Recarga tu wallet para continuar.",
        "out_of_stock": "❌ El producto se agotó antes de completar la compra.",
        "product_unavailable": "❌ El producto ya no está disponible.",
        "wallet_title": (
            "💰 <b>Depósito</b>\n\n"
            "🎁 <b>Niveles de bonus</b>\n{tiers}\n\n"
            "Elige el método de pago para comenzar."
        ),
        "binance_pay": "🟡 Depósito Binance Pay",
        "amount_prompt": (
            "💵 <b>Indica el monto a recargar en USDT</b>\n\n"
            "Mínimo: <b>${minimum}</b>\n"
            "Envía solo el número. Ejemplo: <code>25.00</code>"
        ),
        "amount_invalid": "❌ Monto inválido. Envía un número mayor o igual a ${minimum}.",
        "payment_instructions": (
            "💰 <b>Depósito Binance Pay</b>\n\n"
            "Pay ID: <code>{pay_id}</code>\n"
            "Nombre: <b>{pay_name}</b>\n\n"
            "📦 Envía exactamente <b>{amount} USDT</b> al Pay ID indicado.\n"
            "📋 Después copia el <b>Binance Order ID / Transaction ID</b> y envíalo aquí.\n\n"
            "⚠️ Solo se acreditan pagos recibidos por este Pay ID. Cada ID se puede usar una sola vez."
        ),
        "pay_not_configured": "❌ Binance Pay todavía no está configurado por el administrador.",
        "copy_pay_id": "📋 Copiar Pay ID",
        "where_order_id": "🆔 ¿Dónde encuentro el Order ID?",
        "order_id_help": (
            "🆔 <b>Cómo encontrar el ID</b>\n\n"
            "Abre Binance → Pay → Historial → selecciona el pago enviado. "
            "Copia <b>solo el número</b> del campo Order ID y pégalo en este chat. "
            "No necesitas escribir <code>M_P_</code>."
        ),
        "checking_payment": "🔎 Verificando el pago en Binance…",
        "verify_wait": "⏳ Espera {seconds} segundos antes de volver a verificar.",
        "verify_again": "🔄 Verificar nuevamente",
        "payment_id_invalid": (
            "❌ ID inválido. Envía únicamente el número del Order ID mostrado por Binance.\n\n"
            "Puedes corregirlo o pulsar <b>Cancelar transacción</b> para volver al menú."
        ),
        "deposit_not_pending": "Ese depósito ya no está pendiente.",
        "payment_not_found": (
            "❌ No se encontró ese pago todavía. Revisa el ID, espera unos segundos y vuelve a enviarlo."
        ),
        "payment_mismatch": (
            "❌ La transacción existe, pero no coincide con el Pay ID, el monto o la fecha de esta solicitud."
        ),
        "payment_mismatch_amount": "❌ El Order ID existe, pero el monto no coincide con esta recarga.",
        "payment_mismatch_amount_missing": "❌ Binance no devolvió un monto válido para ese Order ID.",
        "payment_mismatch_currency": "❌ Ese Order ID no corresponde a un ingreso en USDT.",
        "payment_mismatch_outgoing": "❌ Ese Order ID corresponde a un pago enviado, no a un pago recibido.",
        "payment_mismatch_too_old": "❌ Ese pago fue realizado antes de crear esta solicitud de recarga.",
        "payment_mismatch_future": "❌ La fecha de esa transacción no es válida. Revisa la hora del sistema.",
        "payment_mismatch_transaction_time": "❌ Binance no devolvió una fecha válida para ese pago.",
        "payment_mismatch_receiver_pay_id": "❌ Ese pago fue dirigido a otro Pay ID.",
        "payment_mismatch_unsupported_order_type": "❌ Ese Order ID no corresponde a una transferencia válida para recargar.",
        "payment_api_error": (
            "⚠️ Binance no pudo verificarse temporalmente. Tu solicitud quedó pendiente para revisión."
        ),
        "payment_manual_review": (
            "🕓 El pago quedó pendiente de revisión porque la verificación automática no está configurada."
        ),
        "payment_duplicate": "❌ Ese Transaction ID ya fue acreditado anteriormente.",
        "payment_success": (
            "✅ <b>Pago confirmado</b>\n\n"
            "Recarga: <b>${amount}</b>\n"
            "Bonus: <b>${bonus}</b>\n"
            "Acreditado: <b>${total}</b>\n"
            "Nuevo balance: <b>${balance}</b>"
        ),
        "cancel": "❌ Cancelar",
        "cancel_transaction": "❌ Cancelar transacción",
        "cancelled": "Operación cancelada.",
        "deposit_cancelled": "❌ Transacción de recarga cancelada. Volviste al menú principal.",
        "history_title": "🧿 <b>Historial</b>",
        "history_empty": "No tienes compras ni recargas registradas.",
        "history_orders": "\n\n🛍 <b>Compras recientes</b>\n{items}",
        "history_deposits": "\n\n💰 <b>Recargas recientes</b>\n{items}",
        "settings_title": (
            "⚡ <b>Ajustes</b>\n\n"
            "Usuario: {user}\n"
            "Telegram ID: <code>{telegram_id}</code>\n"
            "Idioma: <b>{language}</b>\n"
            "Balance: <b>${balance}</b>"
        ),
        "settings_statistics": (
            "📊 <b>Tus estadísticas</b>\n\n"
            "🎯 Órdenes: <b>{orders}</b>\n"
            "📊 Ítems comprados: <b>{items}</b>\n"
            "💰 Total gastado: <b>{spent} USDT</b>\n"
            "📅 Última orden: <b>{last_order}</b>\n"
            "💰 Recargas: <b>{topups} USDT</b>"
        ),
        "settings_activity_title": "🔊 <b>Mis recargas</b>",
        "settings_deposits_heading": "Recargas de pago 💰",
        "settings_purchases_heading": "Compras pagadas 🎯",
        "settings_no_deposits": "Todavía no tienes recargas registradas.",
        "settings_no_purchases": "Todavía no tienes compras registradas.",
        "language_title": "🌐 <b>Selecciona tu idioma</b>",
        "language_changed": "✅ Idioma actualizado.",
        "support_title": "🔔 <b>Soporte</b>\n\nPulsa el botón para contactar al administrador.",
        "contact_support": "💬 Contactar soporte",
        "notice_stock_new": (
            "{emoji} <b>Nuevo producto disponible</b>\n\n"
            "Producto: <b>{name}</b>\n"
            "Unidades agregadas: <b>{added}</b>\n"
            "Disponibles ahora: <b>{available}</b>\n"
            "Precio: <b>${price}</b>"
        ),
        "notice_stock_update": (
            "{emoji} <b>Stock actualizado</b>\n\n"
            "Producto: <b>{name}</b>\n"
            "Unidades agregadas: <b>{added}</b>\n"
            "Disponibles ahora: <b>{available}</b>\n"
            "Precio: <b>${price}</b>"
        ),
        "notice_product_new": "{emoji} <b>Nuevo producto disponible</b>\n\nProducto: <b>{name}</b>\nPrecio: <b>${price}</b>",
        "notice_product_restocked": "{emoji} <b>Producto disponible nuevamente</b>\n\nProducto: <b>{name}</b>\nPrecio: <b>${price}</b>",
        "notice_catalog_new": "🆕 <b>Nuevos productos disponibles</b>\n\n{items}",
        "notice_catalog_restocked": "📦 <b>Productos disponibles nuevamente</b>\n\n{items}",
        "notice_more_products": "• y {count} más",
        "announcement_title": "📣 <b>Anuncio de la tienda</b>",
        "noop": "Sin acción disponible.",
        "banned": "Tu acceso a la tienda está restringido.",
    },
    "en": {
        "welcome": "🔥 <b>Welcome to {store}</b>\n\n🔊 Your balance: <b>${balance}</b>",
        "menu_store": "🎯 Store",
        "menu_wallet": "💰 Top Up Wallet",
        "menu_settings": "⚡ Settings",
        "menu_support": "🔔 Support",
        "menu_history": "🧿 History",
        "menu_language": "🌐 Language",
        "menu_admin": "🛠 Administration",
        "back_menu": "❌ Back to menu",
        "back": "❌ Back",
        "refresh": "🔄 Refresh",
        "shop_title": "📊 <b>Choose a product:</b>",
        "menu_animated_preview_title": "<b>Animated shortcuts</b>",
        "store_animated_preview_title": "<b>Animated catalog preview</b>",
        "shop_empty": "📭 No products are available right now.",
        "product_header": "🛡️ <b>Available products</b>",
        "product_body": (
            "{emoji} <b>{name}</b>\n\n"
            "🟢 Official store product\n"
            "⚡ Instant automatic delivery\n\n"
            "<blockquote>🛡️ <b>Description</b>\n{description}</blockquote>"
            "{instructions_block}\n\n"
            "📊 Available stock: <b>{stock}</b>\n"
            "💵 Price: <b>${price}</b>"
        ),
        "quantity_selector": (
            "{emoji} <b>{name}</b>\n\n"
            "💰 Base price: <b>{unit_price} USDT</b>\n"
            "📊 Available stock: <b>{stock}</b>\n\n"
            "🤝 Selected quantity: <b>{quantity}</b>\n"
            "🔊 Total amount: <b>{total} USDT</b>\n\n"
            "Select the quantity"
        ),
        "quantity_prompt": (
            "➕ <b>Custom quantity</b>\n\nSend an integer between <b>1</b> and <b>{maximum}</b>."
        ),
        "quantity_invalid": "❌ Invalid quantity. Send a number between 1 and {maximum}.",
        "buy": "🛒 BUY | ${price}",
        "sold_out": "🔴 SOLD OUT",
        "buy_confirm": (
            "🧾 <b>Confirm purchase</b>\n\n"
            "Product: <b>{name}</b>\n"
            "Price: <b>${price}</b>\n"
            "Your balance: <b>${balance}</b>\n\n"
            "Delivery will be sent immediately after confirmation."
        ),
        "confirm": "✅ Confirm purchase",
        "purchase_success": (
            "✅ <b>Purchase completed</b>\n\n"
            "Order: <code>{order}</code>\n"
            "Product: <b>{name}</b>\n"
            "Paid: <b>${price}</b>\n"
            "Remaining balance: <b>${balance}</b>\n\n"
            "{instructions_block}"
            "📦 <b>Your product:</b>\n<pre>{payload}</pre>\n\n"
            "Keep this message in a safe place."
        ),
        "purchase_success_file": (
            "✅ <b>Purchase completed</b>\n\n"
            "Order: <code>{order}</code>\n"
            "Product: <b>{name}</b>\n"
            "Paid: <b>${price}</b>\n"
            "Remaining balance: <b>${balance}</b>\n\n"
            "{instructions_block}"
            "📎 The product is attached as a text file because of its length."
        ),
        "purchase_confirmed_summary": (
            "✅ <b>Purchase confirmed</b>\n\n"
            "The delivery was sent in a separate message and will not be replaced by the menu."
        ),
        "purchase_continue": "The product was also saved in <b>History</b>.",
        "history_delivery": (
            "📦 <b>Saved delivery</b>\n\n"
            "Order: <code>{order}</code>\n"
            "Product: <b>{name}</b>\n"
            "Paid: <b>${price}</b>\n"
            "Date: <b>{date}</b>\n\n"
            "{instructions_block}"
            "<b>Purchased content:</b>\n<pre>{payload}</pre>"
        ),
        "history_delivery_file": (
            "📦 <b>Saved delivery</b>\n\n"
            "Order: <code>{order}</code>\n"
            "Product: <b>{name}</b>\n"
            "Paid: <b>${price}</b>\n"
            "Date: <b>{date}</b>\n\n"
            "{instructions_block}"
            "The content is attached as a text file."
        ),
        "history_order_not_found": "That purchase was not found in your history.",
        "history_resend_notice": "Delivery sent again.",
        "product_file_caption": "📦 Product for order <code>{order}</code>",
        "insufficient": "❌ Insufficient balance. Top up your wallet to continue.",
        "out_of_stock": "❌ The product sold out before the purchase was completed.",
        "product_unavailable": "❌ The product is no longer available.",
        "wallet_title": (
            "💰 <b>Deposit</b>\n\n"
            "🎁 <b>Bonus levels</b>\n{tiers}\n\n"
            "Choose the payment method to begin."
        ),
        "binance_pay": "🟡 Binance Pay Deposit",
        "amount_prompt": (
            "💵 <b>Enter the amount to add in USDT</b>\n\n"
            "Minimum: <b>${minimum}</b>\n"
            "Send only the number. Example: <code>25.00</code>"
        ),
        "amount_invalid": "❌ Invalid amount. Send a number of at least ${minimum}.",
        "payment_instructions": (
            "💰 <b>Binance Pay Deposit</b>\n\n"
            "Pay ID: <code>{pay_id}</code>\n"
            "Name: <b>{pay_name}</b>\n\n"
            "📦 Send exactly <b>{amount} USDT</b> to the Pay ID above.\n"
            "📋 Then copy the <b>Binance Order ID / Transaction ID</b> and send it here.\n\n"
            "⚠️ Only payments received by this Pay ID are credited. Each ID can be used once."
        ),
        "pay_not_configured": "❌ Binance Pay has not been configured by the administrator yet.",
        "copy_pay_id": "📋 Copy Pay ID",
        "where_order_id": "🆔 Where is the Order ID?",
        "order_id_help": (
            "🆔 <b>How to find the ID</b>\n\n"
            "Open Binance → Pay → History → select the sent payment. "
            "Copy <b>only the numeric Order ID</b> and paste it in this chat. "
            "You do not need to type <code>M_P_</code>."
        ),
        "checking_payment": "🔎 Checking the payment on Binance…",
        "verify_wait": "⏳ Wait {seconds} seconds before checking again.",
        "verify_again": "🔄 Verify again",
        "payment_id_invalid": (
            "❌ Invalid ID. Send only the numeric Order ID shown by Binance.\n\n"
            "Correct it or press <b>Cancel transaction</b> to return to the menu."
        ),
        "deposit_not_pending": "That deposit is no longer pending.",
        "payment_not_found": "❌ Payment not found yet. Check the ID, wait a few seconds, and send it again.",
        "payment_mismatch": "❌ The transaction exists, but it does not match this request's Pay ID, amount, or time.",
        "payment_mismatch_amount": "❌ The Order ID exists, but its amount does not match this deposit.",
        "payment_mismatch_amount_missing": "❌ Binance did not return a valid amount for that Order ID.",
        "payment_mismatch_currency": "❌ That Order ID is not an incoming USDT payment.",
        "payment_mismatch_outgoing": "❌ That Order ID is an outgoing payment, not a received payment.",
        "payment_mismatch_too_old": "❌ That payment was made before this deposit request was created.",
        "payment_mismatch_future": "❌ The transaction date is invalid. Check the computer clock.",
        "payment_mismatch_transaction_time": "❌ Binance did not return a valid payment time.",
        "payment_mismatch_receiver_pay_id": "❌ That payment was sent to another Pay ID.",
        "payment_mismatch_unsupported_order_type": "❌ That Order ID is not a valid transfer for a wallet deposit.",
        "payment_api_error": "⚠️ Binance could not be checked temporarily. Your request is pending review.",
        "payment_manual_review": "🕓 The payment is pending review because automatic verification is not configured.",
        "payment_duplicate": "❌ That Transaction ID was already credited.",
        "payment_success": (
            "✅ <b>Payment confirmed</b>\n\n"
            "Deposit: <b>${amount}</b>\n"
            "Bonus: <b>${bonus}</b>\n"
            "Credited: <b>${total}</b>\n"
            "New balance: <b>${balance}</b>"
        ),
        "cancel": "❌ Cancel",
        "cancel_transaction": "❌ Cancel transaction",
        "cancelled": "Operation cancelled.",
        "deposit_cancelled": "❌ Deposit transaction cancelled. You returned to the main menu.",
        "history_title": "🧿 <b>History</b>",
        "history_empty": "You have no recorded purchases or deposits.",
        "history_orders": "\n\n🛍 <b>Recent purchases</b>\n{items}",
        "history_deposits": "\n\n💰 <b>Recent deposits</b>\n{items}",
        "settings_title": (
            "⚡ <b>Settings</b>\n\n"
            "User: {user}\n"
            "Telegram ID: <code>{telegram_id}</code>\n"
            "Language: <b>{language}</b>\n"
            "Balance: <b>${balance}</b>"
        ),
        "settings_statistics": (
            "📊 <b>Your statistics</b>\n\n"
            "🎯 Orders: <b>{orders}</b>\n"
            "📊 Items purchased: <b>{items}</b>\n"
            "💰 Total spent: <b>{spent} USDT</b>\n"
            "📅 Last order: <b>{last_order}</b>\n"
            "💰 Deposits: <b>{topups} USDT</b>"
        ),
        "settings_activity_title": "🔊 <b>My activity</b>",
        "settings_deposits_heading": "Wallet deposits 💰",
        "settings_purchases_heading": "Paid purchases 🎯",
        "settings_no_deposits": "You do not have any recorded deposits yet.",
        "settings_no_purchases": "You do not have any recorded purchases yet.",
        "language_title": "🌐 <b>Select your language</b>",
        "language_changed": "✅ Language updated.",
        "support_title": "🔔 <b>Support</b>\n\nPress the button to contact the administrator.",
        "contact_support": "💬 Contact support",
        "notice_stock_new": (
            "{emoji} <b>New product available</b>\n\n"
            "Product: <b>{name}</b>\n"
            "Units added: <b>{added}</b>\n"
            "Available now: <b>{available}</b>\n"
            "Price: <b>${price}</b>"
        ),
        "notice_stock_update": (
            "{emoji} <b>Stock updated</b>\n\n"
            "Product: <b>{name}</b>\n"
            "Units added: <b>{added}</b>\n"
            "Available now: <b>{available}</b>\n"
            "Price: <b>${price}</b>"
        ),
        "notice_product_new": "{emoji} <b>New product available</b>\n\nProduct: <b>{name}</b>\nPrice: <b>${price}</b>",
        "notice_product_restocked": "{emoji} <b>Product available again</b>\n\nProduct: <b>{name}</b>\nPrice: <b>${price}</b>",
        "notice_catalog_new": "🆕 <b>New products available</b>\n\n{items}",
        "notice_catalog_restocked": "📦 <b>Products available again</b>\n\n{items}",
        "notice_more_products": "• and {count} more",
        "announcement_title": "📣 <b>Store announcement</b>",
        "noop": "No action available.",
        "banned": "Your access to the store is restricted.",
    },
}


logger = logging.getLogger(__name__)
_TEXT_OVERRIDES: dict[tuple[str, str], str] = {}


def install_text_overrides(overrides: dict[tuple[str, str], str]) -> None:
    _TEXT_OVERRIDES.clear()
    for (language, key), value in overrides.items():
        if language in TEXTS and key in TEXTS[language]:
            _TEXT_OVERRIDES[(language, key)] = value


def set_text_override_runtime(language: str, key: str, value: str | None) -> None:
    if language not in TEXTS or key not in TEXTS[language]:
        raise KeyError(f"Unknown text template: {language}:{key}")
    marker = (language, key)
    if value is None:
        _TEXT_OVERRIDES.pop(marker, None)
    else:
        _TEXT_OVERRIDES[marker] = value


def get_default_text_template(language: str, key: str) -> str:
    selected = language if language in TEXTS else "es"
    return TEXTS[selected].get(key) or TEXTS["es"][key]


def get_text_template(language: str, key: str) -> str:
    selected = language if language in TEXTS else "es"
    return _TEXT_OVERRIDES.get((selected, key), get_default_text_template(selected, key))


def get_text_override(language: str, key: str) -> str | None:
    selected = language if language in TEXTS else "es"
    return _TEXT_OVERRIDES.get((selected, key))


def text_template_keys(language: str = "es") -> tuple[str, ...]:
    selected = language if language in TEXTS else "es"
    return tuple(TEXTS[selected])


def text_template_placeholders(template: str) -> frozenset[str]:
    names: set[str] = set()
    for _literal, field_name, _format_spec, _conversion in Formatter().parse(template):
        if not field_name:
            continue
        root = field_name.split(".", 1)[0].split("[", 1)[0]
        names.add(root)
    return frozenset(names)


def t(language: str, key: str, **kwargs: object) -> str:
    selected = language if language in TEXTS else "es"
    template = get_text_template(selected, key)
    try:
        return template.format(**kwargs)
    except (KeyError, ValueError, IndexError) as exc:
        logger.error("Invalid UI text override for %s:%s: %s", selected, key, exc)
        return get_default_text_template(selected, key).format(**kwargs)
