from __future__ import annotations

import logging
import re
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from app.context import AppContext
from app.handlers.helpers import answer_or_replace
from app.keyboards import (
    external_purchase_cancel_keyboard,
    product_keyboard,
    purchase_confirmation_keyboard,
    quantity_selector_keyboard,
    simple_back,
    slot_duration_keyboard,
    store_keyboard,
    without_custom_emoji_icons,
)
from app.models import Product
from app.rich_text import ensure_html_block_before, render_rich_text
from app.services.catalog import ProductWithStock, get_product_with_stock, list_active_products
from app.services.external_purchases import (
    ExternalOrderManualReview,
    ExternalOrderPending,
    ExternalOrderRejected,
    ExternalOutOfStock,
    ExternalProviderAuthenticationFailed,
    ExternalProviderBalanceLow,
    ExternalProviderRateLimited,
    ExternalProviderUnavailable,
    ExternalPurchaseOptionsInvalid,
    ExternalRetailPriceBelowCost,
    purchase_provider_product,
    quote_external_purchase,
)
from app.services.prodseller import ProdSellerError, ProdSellerProduct
from app.services.provider_catalog import refresh_provider_product
from app.services.provider_options import product_provider_options
from app.services.purchases import (
    InsufficientBalance,
    InvalidQuantity,
    OutOfStock,
    ProductUnavailable,
    PurchaseResult,
    purchase_product,
)
from app.services.users import get_or_create_user
from app.states import ExternalPurchaseStates, PurchaseQuantityStates
from app.texts import t
from app.ui_customization import render_product_icon, strip_custom_emoji_entities
from app.ui_rendering import render_store_animated_preview
from app.utils import h, h_truncate, money

logger = logging.getLogger(__name__)
router = Router(name="store")


def _parse_int(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


_EMAIL_PATTERN = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}$")


def _valid_email(value: str) -> bool:
    return bool(_EMAIL_PATTERN.fullmatch(value.strip()))


def _product_instructions_block(
    language: str,
    product: Product,
    *,
    limit: int,
) -> str:
    instructions = (product.instructions or "").strip()
    if not instructions:
        return ""
    title = "Instrucciones" if language == "es" else "Instructions"
    return (
        "\n\n<blockquote>⚠️ <b>"
        + title
        + "</b>\n"
        + render_rich_text(
            instructions,
            product.instructions_entities,
            max_chars=limit,
        )
        + "</blockquote>"
    )


def _delivery_instructions_block(
    language: str,
    instructions: str,
    instructions_entities: str,
    *,
    limit: int = 1800,
) -> str:
    if not instructions.strip():
        return ""
    title = "Instrucciones de activación" if language == "es" else "Activation instructions"
    return (
        "📋 <b>"
        + title
        + "</b>\n"
        + render_rich_text(
            instructions,
            instructions_entities,
            max_chars=limit,
        )
        + "\n\n"
    )


def _product_view_text(
    *,
    language: str,
    product: Product,
    stock_text: str,
    name_limit: int,
    description_limit: int,
    instructions_limit: int,
) -> str:
    name = product.name
    description = product.description or "—"
    button_emoji = product.button_emoji
    price = product.price
    return (
        t(language, "product_header")
        + "\n\n"
        + t(
            language,
            "product_body",
            emoji=render_product_icon(button_emoji),
            name=h_truncate(name, name_limit),
            description=render_rich_text(
                description,
                product.description_entities,
                max_chars=description_limit,
            ),
            instructions_block=_product_instructions_block(
                language, product, limit=instructions_limit
            ),
            stock=h(stock_text),
            price=money(price),
        )
    )


def _maximum_purchase_quantity(item: ProductWithStock) -> int:
    if item.stock <= 0:
        return 0
    if not item.product.is_external:
        return min(100, item.stock)

    options = product_provider_options(item.product)
    maximum = options.max_requested_quantity
    if item.external_stock_known:
        maximum = min(maximum, item.stock // max(1, options.quantity_fixed))
    return max(0, maximum)


def _quantity_selector_text(
    language: str,
    item: ProductWithStock,
    quantity: int,
) -> str:
    total = Decimal(item.product.price) * int(quantity)
    return t(
        language,
        "quantity_selector",
        emoji=render_product_icon(item.product.button_emoji),
        name=h_truncate(item.product.name, 300),
        unit_price=money(item.product.price),
        stock=h(item.stock_text(language)),
        quantity=quantity,
        total=money(total),
    )


def _purchase_options_from_state(data: dict[str, object]) -> dict[str, object]:
    options: dict[str, object] = {}
    email = str(data.get("external_customer_email") or "").strip()
    if email:
        options["customer_email"] = email
    months = data.get("external_slot_months")
    if months is not None:
        options["slot_months"] = months
    return options


async def _load_external_remote(
    ctx: AppContext,
    item: ProductWithStock,
    *,
    force_refresh: bool = False,
) -> tuple[object, ProdSellerProduct] | None:
    product = item.product
    runtime = ctx.providers.get(product.provider_code)
    if runtime is None or not product.external_product_id:
        return None
    remote = await runtime.client.get_product(
        product.external_product_id,
        force_refresh=force_refresh,
    )
    return runtime, remote


async def _show_external_confirmation(
    target: Message | CallbackQuery,
    *,
    state: FSMContext,
    item: ProductWithStock,
    remote: ProdSellerProduct,
    language: str,
    balance: Decimal,
    page: int,
) -> None:
    data = await state.get_data()
    options = _purchase_options_from_state(data)
    selected_quantity = max(1, _parse_int(str(data.get("external_purchase_quantity") or 1), 1))
    quote = quote_external_purchase(
        retail_unit_price=Decimal(item.product.price),
        provider_product=remote,
        purchase_options=options,
        requested_quantity=selected_quantity,
    )
    await state.update_data(
        external_product_id=item.product.id,
        external_page=page,
        external_provider_code=item.product.provider_code,
        external_purchase_quantity=quote.requested_quantity,
    )
    await state.set_state(ExternalPurchaseStates.waiting_confirmation)

    details: list[str] = []
    email = str(options.get("customer_email") or "").strip()
    if email:
        details.append(
            f"Correo: <code>{h(email)}</code>"
            if language == "es"
            else f"Email: <code>{h(email)}</code>"
        )
    months = options.get("slot_months")
    if months is not None:
        details.append(
            f"Duración: <b>{int(months)} mes(es)</b>"
            if language == "es"
            else f"Duration: <b>{int(months)} month(s)</b>"
        )
    if quote.requested_quantity > 1:
        details.append(
            f"Cantidad seleccionada: <b>{quote.requested_quantity}</b>"
            if language == "es"
            else f"Selected quantity: <b>{quote.requested_quantity}</b>"
        )

    text = t(
        language,
        "buy_confirm",
        name=h(item.product.name),
        price=money(quote.local_price),
        balance=money(balance),
    )
    if details:
        text += "\n\n" + "\n".join(details)
    await answer_or_replace(
        target,
        text,
        purchase_confirmation_keyboard(
            language,
            product_id=item.product.id,
            page=page,
            quantity=quote.requested_quantity,
        ),
    )


async def _notify_admins(bot: Bot, ctx: AppContext, text: str) -> None:
    for admin_id in ctx.config.admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            logger.exception("Could not notify admin %s about provider purchase", admin_id)


async def _refresh_external_item(
    ctx: AppContext,
    product_id: int,
    *,
    force_refresh: bool = False,
) -> ProductWithStock | None:
    async with ctx.session_factory() as session:
        item = await get_product_with_stock(session, product_id)
        if item is not None and item.product.is_external:
            runtime = ctx.providers.get(item.product.provider_code)
            if runtime is not None:
                try:
                    await refresh_provider_product(
                        session,
                        runtime.client,
                        item.product,
                        provider_code=runtime.config.code,
                        force_refresh=force_refresh,
                    )
                    item = await get_product_with_stock(session, product_id)
                except ProdSellerError:
                    logger.exception("Could not refresh external product %s", product_id)
        return item


@router.callback_query(F.data.startswith("shop:"))
async def shop_handler(callback: CallbackQuery, ctx: AppContext) -> None:
    await callback.answer()
    async with ctx.session_factory() as session:
        user = await get_or_create_user(session, callback.from_user)
        products, total = await list_active_products(session, page_size=None)
    text = t(user.language, "shop_title")
    animated_preview = render_store_animated_preview(products, user.language)
    if animated_preview:
        text += "\n\n" + t(user.language, "store_animated_preview_title") + "\n" + animated_preview
    if not products:
        text += "\n\n" + t(user.language, "shop_empty")
    await answer_or_replace(
        callback,
        text,
        store_keyboard(
            user.language,
            products,
            page=0,
            total=total,
            page_size=None,
        ),
    )


async def _send_product_view(
    callback: CallbackQuery,
    ctx: AppContext,
    product_id: int,
    page: int,
    *,
    answer_callback: bool = True,
) -> None:
    item = await _refresh_external_item(ctx, product_id)
    async with ctx.session_factory() as session:
        user = await get_or_create_user(session, callback.from_user)
    if item is None or not item.product.active:
        if answer_callback:
            await callback.answer(t(user.language, "product_unavailable"), show_alert=True)
        return

    product = item.product
    stock_text = item.stock_text(user.language)
    text = _product_view_text(
        language=user.language,
        product=product,
        stock_text=stock_text,
        name_limit=300,
        description_limit=1800,
        instructions_limit=1100,
    )
    if answer_callback:
        await callback.answer()
    markup = product_keyboard(
        user.language,
        product_id=product.id,
        page=page,
        price=product.price,
        stock=item.stock,
    )

    message = callback.message
    media_type = product.media_type
    media_file_id = product.media_file_id
    if not media_file_id and product.provider_image_url:
        media_type = "photo"
        media_file_id = product.provider_image_url

    if media_type == "photo" and media_file_id and message is not None:
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        caption = _product_view_text(
            language=user.language,
            product=product,
            stock_text=stock_text,
            name_limit=150,
            description_limit=300,
            instructions_limit=230,
        )
        try:
            await message.answer_photo(
                photo=media_file_id,
                caption=caption,
                reply_markup=markup,
            )
            return
        except TelegramBadRequest as exc:
            if "<tg-emoji" in caption or any(
                item.icon_custom_emoji_id for row in markup.inline_keyboard for item in row
            ):
                try:
                    await message.answer_photo(
                        photo=media_file_id,
                        caption=strip_custom_emoji_entities(caption),
                        reply_markup=without_custom_emoji_icons(markup),
                    )
                    return
                except TelegramBadRequest:
                    pass
            logger.warning(
                "Telegram rejected product image for product %s: %s",
                product.id,
                exc,
            )

    if media_type == "animation" and media_file_id and message is not None:
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        caption = _product_view_text(
            language=user.language,
            product=product,
            stock_text=stock_text,
            name_limit=150,
            description_limit=300,
            instructions_limit=230,
        )
        try:
            await message.answer_animation(
                animation=media_file_id,
                caption=caption,
                reply_markup=markup,
            )
            return
        except TelegramBadRequest as exc:
            if "<tg-emoji" in caption or any(
                item.icon_custom_emoji_id for row in markup.inline_keyboard for item in row
            ):
                try:
                    await message.answer_animation(
                        animation=media_file_id,
                        caption=strip_custom_emoji_entities(caption),
                        reply_markup=without_custom_emoji_icons(markup),
                    )
                    return
                except TelegramBadRequest:
                    pass
            logger.warning(
                "Telegram rejected product animation for product %s: %s",
                product.id,
                exc,
            )

    if media_type == "sticker" and media_file_id and message is not None:
        try:
            await message.answer_sticker(media_file_id)
        except TelegramBadRequest:
            pass
    await answer_or_replace(callback, text, markup)


@router.callback_query(F.data.startswith("product:"))
async def product_handler(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    await state.clear()
    parts = callback.data.split(":")
    product_id = _parse_int(parts[1]) if len(parts) > 1 else 0
    page = _parse_int(parts[2]) if len(parts) > 2 else 0
    await _send_product_view(callback, ctx, product_id, page)


async def _show_quantity_selector(
    target: Message | CallbackQuery,
    *,
    ctx: AppContext,
    product_id: int,
    page: int,
    quantity: int,
    answer_callback: bool = True,
) -> None:
    item = await _refresh_external_item(ctx, product_id)
    async with ctx.session_factory() as session:
        user = await get_or_create_user(session, target.from_user)

    if item is None or not item.product.active:
        if isinstance(target, CallbackQuery) and answer_callback:
            await target.answer(t(user.language, "product_unavailable"), show_alert=True)
        elif isinstance(target, Message):
            await target.answer(t(user.language, "product_unavailable"))
        return
    if item.product.is_external and ctx.providers.get(item.product.provider_code) is None:
        unavailable = (
            "Este producto no está disponible temporalmente."
            if user.language == "es"
            else "This product is temporarily unavailable."
        )
        if isinstance(target, CallbackQuery) and answer_callback:
            await target.answer(unavailable, show_alert=True)
        elif isinstance(target, Message):
            await target.answer(unavailable)
        return

    maximum = _maximum_purchase_quantity(item)
    if maximum < 1:
        if isinstance(target, CallbackQuery) and answer_callback:
            await target.answer(t(user.language, "out_of_stock"), show_alert=True)
        elif isinstance(target, Message):
            await target.answer(t(user.language, "out_of_stock"))
        return
    selected = max(1, min(int(quantity), maximum))
    if isinstance(target, CallbackQuery) and answer_callback:
        await target.answer()
    await answer_or_replace(
        target,
        _quantity_selector_text(user.language, item, selected),
        quantity_selector_keyboard(
            user.language,
            product_id=product_id,
            page=page,
            quantity=selected,
            max_quantity=maximum,
        ),
    )


@router.callback_query(F.data.startswith("buy:"))
async def buy_handler(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    parts = callback.data.split(":")
    product_id = _parse_int(parts[1]) if len(parts) > 1 else 0
    page = _parse_int(parts[2]) if len(parts) > 2 else 0
    await state.clear()
    await _show_quantity_selector(
        callback,
        ctx=ctx,
        product_id=product_id,
        page=page,
        quantity=1,
    )


@router.callback_query(F.data.startswith("buyqty:"))
async def buy_quantity_handler(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    parts = callback.data.split(":")
    product_id = _parse_int(parts[1]) if len(parts) > 1 else 0
    quantity = _parse_int(parts[2], 1) if len(parts) > 2 else 1
    page = _parse_int(parts[3]) if len(parts) > 3 else 0
    await state.clear()
    await _show_quantity_selector(
        callback,
        ctx=ctx,
        product_id=product_id,
        page=page,
        quantity=quantity,
    )


@router.callback_query(F.data.startswith("buyqtycustom:"))
async def buy_custom_quantity_start(
    callback: CallbackQuery, state: FSMContext, ctx: AppContext
) -> None:
    parts = callback.data.split(":")
    product_id = _parse_int(parts[1]) if len(parts) > 1 else 0
    page = _parse_int(parts[2]) if len(parts) > 2 else 0
    item = await _refresh_external_item(ctx, product_id)
    async with ctx.session_factory() as session:
        user = await get_or_create_user(session, callback.from_user)
    if item is None or not item.product.active:
        await callback.answer(t(user.language, "product_unavailable"), show_alert=True)
        return
    maximum = _maximum_purchase_quantity(item)
    if maximum < 1:
        await callback.answer(t(user.language, "out_of_stock"), show_alert=True)
        return

    await state.set_state(PurchaseQuantityStates.waiting_custom_quantity)
    await state.update_data(
        quantity_product_id=product_id,
        quantity_page=page,
        quantity_maximum=maximum,
    )
    await callback.answer()
    await answer_or_replace(
        callback,
        t(user.language, "quantity_prompt", maximum=maximum),
        simple_back(user.language, f"buy:{product_id}:{page}"),
    )


@router.message(PurchaseQuantityStates.waiting_custom_quantity)
async def buy_custom_quantity_receive(message: Message, state: FSMContext, ctx: AppContext) -> None:
    data = await state.get_data()
    product_id = _parse_int(str(data.get("quantity_product_id") or 0))
    page = _parse_int(str(data.get("quantity_page") or 0))
    item = await _refresh_external_item(ctx, product_id)
    async with ctx.session_factory() as session:
        user = await get_or_create_user(session, message.from_user)
    if item is None or not item.product.active:
        await state.clear()
        await message.answer(t(user.language, "product_unavailable"))
        return

    maximum = _maximum_purchase_quantity(item)
    try:
        quantity = int((message.text or "").strip())
    except ValueError:
        quantity = 0
    if quantity < 1 or quantity > maximum:
        await message.answer(t(user.language, "quantity_invalid", maximum=maximum))
        return

    await state.clear()
    await _show_quantity_selector(
        message,
        ctx=ctx,
        product_id=product_id,
        page=page,
        quantity=quantity,
        answer_callback=False,
    )


@router.callback_query(F.data.startswith("buyexecute:"))
async def buy_execute_handler(
    callback: CallbackQuery, bot: Bot, state: FSMContext, ctx: AppContext
) -> None:
    parts = callback.data.split(":")
    product_id = _parse_int(parts[1]) if len(parts) > 1 else 0
    quantity = _parse_int(parts[2], 1) if len(parts) > 2 else 1
    page = _parse_int(parts[3]) if len(parts) > 3 else 0

    item = await _refresh_external_item(ctx, product_id)
    async with ctx.session_factory() as session:
        user = await get_or_create_user(session, callback.from_user)
    if item is None or not item.product.active:
        await callback.answer(t(user.language, "product_unavailable"), show_alert=True)
        return
    maximum = _maximum_purchase_quantity(item)
    if quantity < 1 or quantity > maximum:
        await callback.answer(
            t(user.language, "quantity_invalid", maximum=maximum), show_alert=True
        )
        await _show_quantity_selector(
            callback,
            ctx=ctx,
            product_id=product_id,
            page=page,
            quantity=max(1, min(quantity, max(1, maximum))),
            answer_callback=False,
        )
        return

    await state.clear()
    if not item.product.is_external:
        await _execute_purchase(
            callback,
            bot,
            state,
            ctx,
            product_id=product_id,
            page=page,
            quantity=quantity,
        )
        return

    if ctx.providers.get(item.product.provider_code) is None:
        await callback.answer(
            "Producto no disponible temporalmente"
            if user.language == "es"
            else "Product temporarily unavailable",
            show_alert=True,
        )
        return

    options = product_provider_options(item.product)
    await state.update_data(
        external_product_id=product_id,
        external_page=page,
        external_provider_code=item.product.provider_code,
        external_purchase_quantity=quantity,
    )
    if options.requires_customer_email:
        await state.set_state(ExternalPurchaseStates.waiting_customer_email)
        await callback.answer()
        prompt = (
            "📧 <b>Correo para la activación</b>\n\n"
            "Envía el correo que debe recibir el producto o la invitación. "
            "Revisa que esté escrito correctamente antes de continuar."
            if user.language == "es"
            else "📧 <b>Activation email</b>\n\n"
            "Send the email that should receive the product or invitation. "
            "Check it carefully before continuing."
        )
        await answer_or_replace(
            callback,
            prompt,
            external_purchase_cancel_keyboard(user.language, product_id=product_id, page=page),
        )
        return
    if options.requires_slot_months:
        await state.set_state(ExternalPurchaseStates.waiting_slot_months)
        await callback.answer()
        prompt = (
            "📅 <b>Elige la duración</b>\n\nSelecciona cuántos meses deseas comprar."
            if user.language == "es"
            else "📅 <b>Choose the duration</b>\n\nSelect how many months you want to buy."
        )
        await answer_or_replace(
            callback,
            prompt,
            slot_duration_keyboard(
                user.language,
                product_id=product_id,
                page=page,
                durations=options.slot_durations,
            ),
        )
        return

    await _execute_purchase(
        callback,
        bot,
        state,
        ctx,
        product_id=product_id,
        page=page,
        quantity=quantity,
    )


@router.message(ExternalPurchaseStates.waiting_customer_email)
async def external_customer_email_handler(
    message: Message, state: FSMContext, ctx: AppContext
) -> None:
    data = await state.get_data()
    product_id = _parse_int(str(data.get("external_product_id") or 0))
    page = _parse_int(str(data.get("external_page") or 0))
    email = (message.text or "").strip()

    async with ctx.session_factory() as session:
        user = await get_or_create_user(session, message.from_user)
    if not _valid_email(email):
        await message.answer(
            "❌ Correo inválido. Envía un correo como <code>usuario@dominio.com</code>."
            if user.language == "es"
            else "❌ Invalid email. Send an address such as <code>user@example.com</code>.",
            reply_markup=external_purchase_cancel_keyboard(
                user.language, product_id=product_id, page=page
            ),
        )
        return

    item = await _refresh_external_item(ctx, product_id)
    if item is None or not item.product.active or not item.product.is_external:
        await state.clear()
        await message.answer(t(user.language, "product_unavailable"))
        return
    try:
        loaded = await _load_external_remote(ctx, item)
    except ProdSellerError:
        logger.exception("Could not load provider requirements after email")
        await state.clear()
        await message.answer(
            "⚠️ El proveedor no responde. Intenta nuevamente desde la tienda."
            if user.language == "es"
            else "⚠️ The provider is not responding. Start again from the store."
        )
        return
    if loaded is None:
        await state.clear()
        await message.answer(t(user.language, "product_unavailable"))
        return
    _runtime, remote = loaded
    await state.update_data(external_customer_email=email)

    if remote.requires_slot_months:
        await state.set_state(ExternalPurchaseStates.waiting_slot_months)
        await message.answer(
            "📅 <b>Elige la duración</b>\n\nSelecciona cuántos meses deseas comprar."
            if user.language == "es"
            else "📅 <b>Choose the duration</b>\n\nSelect how many months you want to buy.",
            reply_markup=slot_duration_keyboard(
                user.language,
                product_id=product_id,
                page=page,
                durations=remote.slot_durations,
            ),
        )
        return

    try:
        await _show_external_confirmation(
            message,
            state=state,
            item=item,
            remote=remote,
            language=user.language,
            balance=Decimal(user.balance),
            page=page,
        )
    except ExternalPurchaseOptionsInvalid as exc:
        await state.clear()
        await message.answer(f"❌ {h(exc)}")


@router.callback_query(F.data.startswith("buymonths:"))
async def external_slot_months_handler(
    callback: CallbackQuery, state: FSMContext, ctx: AppContext
) -> None:
    parts = callback.data.split(":")
    product_id = _parse_int(parts[1]) if len(parts) > 1 else 0
    months = _parse_int(parts[2]) if len(parts) > 2 else 0
    page = _parse_int(parts[3]) if len(parts) > 3 else 0
    data = await state.get_data()
    if _parse_int(str(data.get("external_product_id") or 0)) != product_id:
        await state.clear()
        await callback.answer("Compra vencida. Abre el producto nuevamente.", show_alert=True)
        return

    item = await _refresh_external_item(ctx, product_id)
    async with ctx.session_factory() as session:
        user = await get_or_create_user(session, callback.from_user)
    if item is None or not item.product.active or not item.product.is_external:
        await state.clear()
        await callback.answer(t(user.language, "product_unavailable"), show_alert=True)
        return
    try:
        loaded = await _load_external_remote(ctx, item)
    except ProdSellerError:
        logger.exception("Could not load provider requirements after month selection")
        await state.clear()
        await callback.answer(
            "El proveedor no responde. Intenta más tarde."
            if user.language == "es"
            else "The provider is not responding. Try later.",
            show_alert=True,
        )
        return
    if loaded is None:
        await state.clear()
        await callback.answer(t(user.language, "product_unavailable"), show_alert=True)
        return
    _runtime, remote = loaded
    allowed = remote.slot_durations or (1,)
    if months not in allowed:
        await callback.answer(
            "Duración no permitida." if user.language == "es" else "Duration not allowed.",
            show_alert=True,
        )
        return

    await state.update_data(external_slot_months=months)
    try:
        await _show_external_confirmation(
            callback,
            state=state,
            item=item,
            remote=remote,
            language=user.language,
            balance=Decimal(user.balance),
            page=page,
        )
    except ExternalPurchaseOptionsInvalid as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer()


async def _send_purchase_delivery(
    callback: CallbackQuery,
    *,
    language: str,
    result: PurchaseResult,
) -> None:
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(
            t(language, "purchase_confirmed_summary"),
            reply_markup=None,
        )
    except TelegramBadRequest:
        await callback.message.answer(t(language, "purchase_confirmed_summary"))

    instructions_block = _delivery_instructions_block(
        language,
        result.instructions,
        result.instructions_entities,
    )
    delivery = t(
        language,
        "purchase_success",
        order=h(result.order_code),
        name=h_truncate(result.product_name, 300),
        price=money(result.price),
        balance=money(result.new_balance),
        instructions_block=instructions_block,
        payload=h(result.stock_payload),
    )
    delivery = ensure_html_block_before(
        delivery,
        instructions_block,
        markers=("📦 <b>Tu producto:</b>", "📦 <b>Your product:</b>"),
    )
    if len(delivery) <= 4000:
        try:
            await callback.message.answer(delivery)
        except TelegramBadRequest:
            if "<tg-emoji" not in delivery:
                raise
            await callback.message.answer(strip_custom_emoji_entities(delivery))
    else:
        summary = t(
            language,
            "purchase_success_file",
            order=h(result.order_code),
            name=h_truncate(result.product_name, 300),
            price=money(result.price),
            balance=money(result.new_balance),
            instructions_block=instructions_block,
        )
        summary = ensure_html_block_before(
            summary,
            instructions_block,
            markers=("📎",),
        )
        try:
            await callback.message.answer(summary)
        except TelegramBadRequest:
            if "<tg-emoji" not in summary:
                raise
            await callback.message.answer(strip_custom_emoji_entities(summary))
        await callback.message.answer_document(
            BufferedInputFile(
                result.stock_payload.encode("utf-8"),
                filename=f"{result.order_code}.txt",
            ),
            caption=t(language, "product_file_caption", order=h(result.order_code)),
        )

    await callback.message.answer(
        t(language, "purchase_continue"),
        reply_markup=simple_back(language, "shop:0"),
    )


async def _external_purchase_failure_message(
    callback: CallbackQuery,
    *,
    language: str,
    text_es: str,
    text_en: str,
) -> None:
    if callback.message is not None:
        await callback.message.answer(
            text_es if language == "es" else text_en,
            reply_markup=simple_back(language, "shop:0"),
        )


async def _execute_purchase(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    ctx: AppContext,
    *,
    product_id: int,
    page: int,
    quantity: int,
) -> None:
    async with ctx.session_factory() as session:
        user = await get_or_create_user(session, callback.from_user)
        item = await get_product_with_stock(session, product_id)

    if item is None or not item.product.active:
        await callback.answer(t(user.language, "product_unavailable"), show_alert=True)
        return

    if not item.product.is_external:
        await state.clear()
        try:
            async with ctx.session_factory() as session:
                result = await purchase_product(
                    session,
                    telegram_id=callback.from_user.id,
                    product_id=product_id,
                    quantity=quantity,
                )
                user = await get_or_create_user(session, callback.from_user)
        except InvalidQuantity:
            await callback.answer(
                t(user.language, "quantity_invalid", maximum=max(1, item.stock)),
                show_alert=True,
            )
            return
        except InsufficientBalance:
            await callback.answer(t(user.language, "insufficient"), show_alert=True)
            return
        except OutOfStock:
            await callback.answer(t(user.language, "out_of_stock"), show_alert=True)
            await _send_product_view(callback, ctx, product_id, page, answer_callback=False)
            return
        except ProductUnavailable:
            await callback.answer(t(user.language, "product_unavailable"), show_alert=True)
            return

        await callback.answer()
        await _send_purchase_delivery(callback, language=user.language, result=result)
        return

    runtime = ctx.providers.get(item.product.provider_code)
    if runtime is None:
        await callback.answer(
            "Producto no disponible temporalmente"
            if user.language == "es"
            else "Product temporarily unavailable",
            show_alert=True,
        )
        return

    state_data = await state.get_data()
    purchase_options: dict[str, object] = {}
    if _parse_int(str(state_data.get("external_product_id") or 0)) == product_id:
        purchase_options = _purchase_options_from_state(state_data)
        quantity = max(
            1,
            _parse_int(str(state_data.get("external_purchase_quantity") or quantity), quantity),
        )
    await state.clear()

    await callback.answer("Procesando…" if user.language == "es" else "Processing…")
    if callback.message is not None:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await callback.message.answer(
            "⏳ <b>Procesando el pedido con el proveedor…</b>\nNo pulses comprar nuevamente."
            if user.language == "es"
            else "⏳ <b>Processing the provider order…</b>\nDo not press buy again."
        )

    try:
        result = await purchase_provider_product(
            ctx.session_factory,
            runtime.client,
            provider_code=runtime.config.code,
            telegram_id=callback.from_user.id,
            product_id=product_id,
            allow_below_cost=runtime.config.allow_below_cost,
            poll_attempts=runtime.config.order_poll_attempts,
            poll_delay_seconds=runtime.config.order_poll_delay_seconds,
            purchase_options=purchase_options,
            requested_quantity=quantity,
        )
    except ExternalPurchaseOptionsInvalid:
        await _external_purchase_failure_message(
            callback,
            language=user.language,
            text_es=(
                "❌ Faltan datos para este producto. Abre el producto nuevamente y completa "
                "el correo o la duración solicitada."
            ),
            text_en=(
                "❌ Required purchase data is missing. Open the product again and complete "
                "the requested email or duration."
            ),
        )
        return
    except InsufficientBalance:
        await _external_purchase_failure_message(
            callback,
            language=user.language,
            text_es="❌ Saldo insuficiente para completar la compra.",
            text_en="❌ Insufficient balance to complete the purchase.",
        )
        return
    except ExternalOutOfStock:
        await _external_purchase_failure_message(
            callback,
            language=user.language,
            text_es="❌ El proveedor se quedó sin stock. No se realizó el cobro o el saldo fue devuelto.",
            text_en="❌ The provider is out of stock. No charge was made or the balance was refunded.",
        )
        return
    except ExternalRetailPriceBelowCost as exc:
        await _external_purchase_failure_message(
            callback,
            language=user.language,
            text_es="⚠️ El producto fue pausado temporalmente porque su costo cambió. No se descontó saldo.",
            text_en="⚠️ The product was temporarily paused because its cost changed. No balance was deducted.",
        )
        await _notify_admins(
            bot,
            ctx,
            "⚠️ <b>Precio de venta menor que el costo del proveedor</b>\n\n"
            f"Producto local: <code>{product_id}</code>\n"
            f"Precio tienda: <b>${money(exc.retail_price)}</b>\n"
            f"Costo proveedor: <b>${money(exc.provider_cost)}</b>\n"
            "Actualiza manualmente el precio desde /admin.",
        )
        return
    except ExternalProviderBalanceLow:
        await _external_purchase_failure_message(
            callback,
            language=user.language,
            text_es="❌ El proveedor no tiene saldo suficiente. El saldo del cliente fue devuelto.",
            text_en="❌ The provider account has insufficient balance. Your balance was refunded.",
        )
        await _notify_admins(
            bot,
            ctx,
            "❌ <b>Saldo insuficiente en el proveedor</b>\n\n"
            f"Cliente: <code>{callback.from_user.id}</code>\n"
            f"Producto local: <code>{product_id}</code>\n"
            "Recarga el balance del proveedor antes de reactivar ventas.",
        )
        return
    except ExternalProviderAuthenticationFailed:
        await _external_purchase_failure_message(
            callback,
            language=user.language,
            text_es="❌ La conexión con el proveedor está mal configurada. El saldo fue devuelto.",
            text_en="❌ The provider connection is misconfigured. Your balance was refunded.",
        )
        await _notify_admins(
            bot,
            ctx,
            f"❌ <b>{h(runtime.config.name)} rechazó la API Key</b>\nRevisa su clave en configurar_apis.bat.",
        )
        return
    except ExternalProviderRateLimited:
        await _external_purchase_failure_message(
            callback,
            language=user.language,
            text_es="⚠️ El proveedor alcanzó el límite de solicitudes. El saldo fue devuelto; intenta más tarde.",
            text_en="⚠️ The provider rate limit was reached. Your balance was refunded; try later.",
        )
        return
    except ExternalProviderUnavailable:
        await _external_purchase_failure_message(
            callback,
            language=user.language,
            text_es="⚠️ El proveedor no responde. No se completó la compra; cualquier reserva segura fue devuelta.",
            text_en="⚠️ The provider is not responding. The purchase was not completed; any safe reservation was refunded.",
        )
        return
    except ExternalOrderRejected as exc:
        await _external_purchase_failure_message(
            callback,
            language=user.language,
            text_es="❌ El proveedor rechazó el pedido. El saldo fue devuelto.",
            text_en="❌ The provider rejected the order. Your balance was refunded.",
        )
        await _notify_admins(
            bot,
            ctx,
            "❌ <b>Pedido de proveedor rechazado</b>\n"
            f"Cliente: <code>{callback.from_user.id}</code>\n"
            f"Producto: <code>{product_id}</code>\n"
            f"Error: <code>{h(exc)}</code>",
        )
        return
    except ExternalOrderPending as exc:
        await _external_purchase_failure_message(
            callback,
            language=user.language,
            text_es=(
                "🕓 El proveedor aceptó el pedido, pero la entrega sigue pendiente. "
                f"Código: <code>{h(exc.purchase_code)}</code>. No repitas la compra; "
                "el administrador revisará la orden."
            ),
            text_en=(
                "🕓 The provider accepted the order, but delivery is still pending. "
                f"Code: <code>{h(exc.purchase_code)}</code>. Do not buy again; "
                "the administrator will review it."
            ),
        )
        await _notify_admins(
            bot,
            ctx,
            "🕓 <b>Pedido de proveedor pendiente</b>\n\n"
            f"Compra interna: <code>#{exc.purchase_id}</code>\n"
            f"Código: <code>{h(exc.purchase_code)}</code>\n"
            f"Order ID proveedor: <code>{h(exc.provider_order_id)}</code>\n"
            f"Estado: <code>{h(exc.status)}</code>\n"
            "Revísalo en /admin → Proveedores API → Pedidos pendientes.",
        )
        return
    except ExternalOrderManualReview as exc:
        await _external_purchase_failure_message(
            callback,
            language=user.language,
            text_es=(
                "⚠️ No se pudo confirmar si el proveedor creó la orden. "
                f"Código: <code>{h(exc.purchase_code)}</code>. Tu saldo quedó reservado "
                "para evitar una compra duplicada. Contacta soporte si no recibes respuesta."
            ),
            text_en=(
                "⚠️ It was not possible to confirm whether the provider created the order. "
                f"Code: <code>{h(exc.purchase_code)}</code>. Your balance remains reserved "
                "to prevent a duplicate purchase. Contact support if needed."
            ),
        )
        await _notify_admins(
            bot,
            ctx,
            "⚠️ <b>Pedido de proveedor requiere revisión manual</b>\n\n"
            f"Compra interna: <code>#{exc.purchase_id}</code>\n"
            f"Código: <code>{h(exc.purchase_code)}</code>\n"
            f"Cliente: <code>{callback.from_user.id}</code>\n"
            f"Motivo: <code>{h_truncate(exc.reason, 1500)}</code>\n"
            "No repitas el POST sin comprobar primero el panel del proveedor.",
        )
        return
    except ProductUnavailable:
        await _external_purchase_failure_message(
            callback,
            language=user.language,
            text_es="❌ El producto ya no está disponible.",
            text_en="❌ The product is no longer available.",
        )
        return

    await _send_purchase_delivery(callback, language=user.language, result=result)
    await _notify_admins(
        bot,
        ctx,
        f"✅ <b>Venta entregada por {h(runtime.config.name)}</b>\n\n"
        f"Cliente: <code>{callback.from_user.id}</code>\n"
        f"Producto: <b>{h_truncate(result.product_name, 300)}</b>\n"
        f"Orden local: <code>{h(result.order_code)}</code>\n"
        f"Venta: <b>${money(result.price)}</b>",
    )


@router.callback_query(F.data.startswith("buyconfirm:"))
async def buy_confirm_handler(
    callback: CallbackQuery, bot: Bot, state: FSMContext, ctx: AppContext
) -> None:
    parts = callback.data.split(":")
    product_id = _parse_int(parts[1]) if len(parts) > 1 else 0
    page = _parse_int(parts[2]) if len(parts) > 2 else 0
    quantity = _parse_int(parts[3], 1) if len(parts) > 3 else 1
    await _execute_purchase(
        callback,
        bot,
        state,
        ctx,
        product_id=product_id,
        page=page,
        quantity=quantity,
    )
