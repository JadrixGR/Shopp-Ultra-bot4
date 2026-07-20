from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.database import create_engine_and_session_factory, init_database
from app.keyboards import main_menu, store_keyboard
from app.models import AppSetting, Product
from app.product_icons import pack_product_emoji
from app.services.catalog import ProductWithStock
from app.services.ui_settings import (
    load_ui_settings,
    reset_all_ui_settings,
    save_button_override,
    save_text_override,
    save_ui_option,
)
from app.texts import install_text_overrides, t
from app.ui_customization import (
    ButtonOverride,
    get_ui_option,
    install_button_overrides,
    install_ui_options,
    render_custom_emoji,
    resolve_button,
    strip_custom_emoji_entities,
)
from app.ui_rendering import render_store_animated_preview


@pytest.fixture(autouse=True)
def reset_runtime_appearance() -> None:
    install_button_overrides({})
    install_ui_options({})
    install_text_overrides({})
    yield
    install_button_overrides({})
    install_ui_options({})
    install_text_overrides({})


def test_custom_emoji_html_rendering_and_fallback() -> None:
    packed = pack_product_emoji("5368324170671202286", "1️⃣")

    rendered = render_custom_emoji(packed)

    assert rendered == '<tg-emoji emoji-id="5368324170671202286">1️⃣</tg-emoji>'
    assert strip_custom_emoji_entities(rendered) == "1️⃣"


@pytest.mark.asyncio
async def test_button_appearance_is_persistent_and_applied_to_main_menu() -> None:
    engine, factory = create_engine_and_session_factory("sqlite+aiosqlite:///:memory:")
    await init_database(engine)
    packed = pack_product_emoji("5368324170671202286", "1️⃣")
    override = ButtonOverride(
        label_es="Catálogo Ultra",
        label_en="Ultra catalog",
        icon=packed,
        style="danger",
    )

    async with factory() as session:
        await save_button_override(session, "main_store", override)
        row = await session.get(AppSetting, "ui_button:main_store")
        assert row is not None

    install_button_overrides({})
    async with factory() as session:
        await load_ui_settings(session)

    presentation = resolve_button("main_store", "es")
    markup = main_menu("es", is_admin=False)
    store_button = markup.inline_keyboard[0][0]

    assert presentation.label == "Catálogo Ultra"
    assert store_button.text == "Catálogo Ultra"
    assert store_button.style == "danger"
    assert store_button.icon_custom_emoji_id == "5368324170671202286"

    async with factory() as session:
        await reset_all_ui_settings(session)
    await engine.dispose()


@pytest.mark.asyncio
async def test_text_and_animation_options_are_persistent() -> None:
    engine, factory = create_engine_and_session_factory("sqlite+aiosqlite:///:memory:")
    await init_database(engine)

    async with factory() as session:
        await save_text_override(
            session,
            "es",
            "shop_title",
            "<b>Mi catálogo personalizado</b>",
        )
        await save_ui_option(session, "animated_store_preview", False)

    install_text_overrides({})
    install_ui_options({})
    async with factory() as session:
        await load_ui_settings(session)

    assert t("es", "shop_title") == "<b>Mi catálogo personalizado</b>"
    assert get_ui_option("animated_store_preview") is False

    await engine.dispose()


def test_store_preview_renders_animated_product_entities() -> None:
    packed = pack_product_emoji("5368324170671202286", "1️⃣")
    product = Product(
        id=10,
        name="Gemini AI Pro",
        description="",
        price=Decimal("1.50"),
        button_emoji=packed,
        active=True,
    )
    rows = [ProductWithStock(product=product, stock=13)]

    preview = render_store_animated_preview(rows, "es")
    markup = store_keyboard("es", rows, page=0, total=1)

    assert '<tg-emoji emoji-id="5368324170671202286">' in preview
    assert "Gemini AI Pro" in preview
    assert markup.inline_keyboard[0][0].icon_custom_emoji_id == "5368324170671202286"


def test_store_preview_can_be_disabled_without_disabling_button_icon() -> None:
    packed = pack_product_emoji("5368324170671202286", "1️⃣")
    product = Product(
        id=11,
        name="Producto",
        description="",
        price=Decimal("2.00"),
        button_emoji=packed,
        active=True,
    )
    rows = [ProductWithStock(product=product, stock=1)]
    install_ui_options({"animated_store_preview": False})

    assert render_store_animated_preview(rows, "es") == ""
    assert (
        store_keyboard("es", rows, page=0, total=1).inline_keyboard[0][0].icon_custom_emoji_id
        == "5368324170671202286"
    )


def test_store_preview_stays_inside_message_safe_length_for_large_catalogs() -> None:
    packed = pack_product_emoji("5368324170671202286", "1️⃣")
    rows = [
        ProductWithStock(
            product=Product(
                id=index,
                name=f"Producto animado con nombre extenso número {index}",
                description="",
                price=Decimal("1.50"),
                button_emoji=packed,
                active=True,
            ),
            stock=10,
        )
        for index in range(1, 151)
    ]

    preview = render_store_animated_preview(rows, "es")

    assert len(preview) <= 2802
    assert preview.endswith("…")


@pytest.mark.asyncio
async def test_old_product_rows_survive_button_style_migration(tmp_path) -> None:
    database = tmp_path / "old_shop.db"
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE products (
                id INTEGER PRIMARY KEY,
                name VARCHAR(180) NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                price NUMERIC(18, 2) NOT NULL,
                button_emoji VARCHAR(32) NOT NULL DEFAULT '🛍️',
                media_type VARCHAR(16),
                media_file_id TEXT,
                active BOOLEAN NOT NULL DEFAULT 1,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            );
            INSERT INTO products (
                id, name, description, price, button_emoji, active, created_at, updated_at
            ) VALUES (
                1, 'Producto anterior', 'Conservar', 3.50, '📦', 1,
                '2026-01-01 00:00:00', '2026-01-01 00:00:00'
            );
            """
        )
        connection.commit()
    finally:
        connection.close()

    engine, factory = create_engine_and_session_factory(
        f"sqlite+aiosqlite:///{database.as_posix()}"
    )
    await init_database(engine)

    async with engine.connect() as sql_connection:
        columns = {
            row[1]
            for row in (await sql_connection.execute(text('PRAGMA table_info("products")'))).all()
        }
        assert "button_style" in columns

    async with factory() as session:
        product = await session.scalar(select(Product).where(Product.id == 1))
        assert product is not None
        assert product.name == "Producto anterior"
        assert Decimal(product.price) == Decimal("3.50")
        assert product.button_style == "primary"

    await engine.dispose()
