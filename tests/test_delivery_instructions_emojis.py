from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from aiogram.types import Message
from sqlalchemy import select

from app.database import create_engine_and_session_factory, init_database
from app.handlers.store import _delivery_instructions_block, _product_view_text
from app.models import Order, Product, User
from app.rich_text import capture_message_rich_text, render_rich_text
from app.services.catalog import add_stock_items
from app.services.purchases import purchase_product
from app.texts import t
from app.utils import h, money

CUSTOM_EMOJI_ID = "5368324170671202286"


def _custom_emoji_message(text: str, *, emoji_length: int = 2) -> Message:
    return Message.model_validate(
        {
            "message_id": 1,
            "date": datetime.now(UTC),
            "chat": {"id": 1, "type": "private"},
            "text": text,
            "entities": [
                {
                    "type": "custom_emoji",
                    "offset": 0,
                    "length": emoji_length,
                    "custom_emoji_id": CUSTOM_EMOJI_ID,
                }
            ],
        }
    )


def test_custom_emoji_is_captured_and_rendered_as_telegram_entity() -> None:
    text, entities = capture_message_rich_text(_custom_emoji_message("🚀 Activación"))

    assert text == "🚀 Activación"
    assert CUSTOM_EMOJI_ID in entities
    assert render_rich_text(text, entities) == (
        f'<tg-emoji emoji-id="{CUSTOM_EMOJI_ID}">🚀</tg-emoji> Activación'
    )


def test_product_description_and_instructions_keep_animated_custom_emoji() -> None:
    description, description_entities = capture_message_rich_text(
        _custom_emoji_message("🚀 Descripción premium")
    )
    instructions, instructions_entities = capture_message_rich_text(
        _custom_emoji_message("🚀 Paso de activación")
    )
    product = Product(
        name="Producto",
        description=description,
        description_entities=description_entities,
        instructions=instructions,
        instructions_entities=instructions_entities,
        price=Decimal("5.00"),
        button_emoji="📦",
        active=True,
    )

    rendered = _product_view_text(
        language="es",
        product=product,
        stock_text="3",
        name_limit=300,
        description_limit=1800,
        instructions_limit=1100,
    )

    assert rendered.count(f'<tg-emoji emoji-id="{CUSTOM_EMOJI_ID}">') == 2
    assert "Descripción premium" in rendered
    assert "Paso de activación" in rendered


@pytest.mark.asyncio
async def test_purchase_saves_instruction_snapshot_and_returns_it_for_delivery() -> None:
    engine, factory = create_engine_and_session_factory("sqlite+aiosqlite:///:memory:")
    await init_database(engine)
    instructions, instruction_entities = capture_message_rich_text(
        _custom_emoji_message("🚀 Abre el enlace y activa tu cuenta")
    )

    async with factory() as session:
        user = User(
            telegram_id=777001,
            first_name="Buyer",
            balance=Decimal("20.00"),
            language="es",
        )
        product = Product(
            name="Producto con guía",
            description="Descripción",
            instructions=instructions,
            instructions_entities=instruction_entities,
            price=Decimal("4.00"),
            button_emoji="📦",
            active=True,
        )
        session.add_all([user, product])
        await session.commit()
        product_id = product.id
        await add_stock_items(session, product_id, ["KEY-DELIVERY"])

    async with factory() as session:
        result = await purchase_product(
            session,
            telegram_id=777001,
            product_id=product_id,
        )

    assert result.instructions == instructions
    assert result.instructions_entities == instruction_entities

    async with factory() as session:
        order = await session.scalar(select(Order).where(Order.order_code == result.order_code))
        assert order is not None
        assert order.instructions_snapshot == instructions
        assert order.instructions_entities_snapshot == instruction_entities

    instruction_block = _delivery_instructions_block(
        "es",
        result.instructions,
        result.instructions_entities,
    )
    delivery = t(
        "es",
        "purchase_success",
        order=h(result.order_code),
        name=result.product_name,
        price=money(result.price),
        balance=money(result.new_balance),
        instructions_block=instruction_block,
        payload=h(result.stock_payload),
    )
    assert delivery.index("Instrucciones de activación") < delivery.index("Tu producto")
    assert f'<tg-emoji emoji-id="{CUSTOM_EMOJI_ID}">' in delivery

    await engine.dispose()
