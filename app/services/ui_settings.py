from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting
from app.texts import install_text_overrides, set_text_override_runtime
from app.ui_customization import (
    BUTTON_DEFINITIONS,
    UI_OPTION_DEFAULTS,
    ButtonOverride,
    install_button_overrides,
    install_ui_options,
    set_button_override_runtime,
    set_ui_option_runtime,
)

BUTTON_PREFIX = "ui_button:"
TEXT_PREFIX = "ui_text:"
OPTION_PREFIX = "ui_option:"


def button_setting_key(button_key: str) -> str:
    if button_key not in BUTTON_DEFINITIONS:
        raise KeyError(button_key)
    return f"{BUTTON_PREFIX}{button_key}"


def text_setting_key(language: str, text_key: str) -> str:
    if language not in {"es", "en"}:
        raise ValueError("Unsupported language")
    return f"{TEXT_PREFIX}{language}:{text_key}"


def option_setting_key(option_key: str) -> str:
    if option_key not in UI_OPTION_DEFAULTS:
        raise KeyError(option_key)
    return f"{OPTION_PREFIX}{option_key}"


async def load_ui_settings(session: AsyncSession) -> None:
    rows = (
        await session.scalars(
            select(AppSetting).where(
                AppSetting.key.like("ui_button:%")
                | AppSetting.key.like("ui_text:%")
                | AppSetting.key.like("ui_option:%")
            )
        )
    ).all()
    button_overrides: dict[str, ButtonOverride] = {}
    text_overrides: dict[tuple[str, str], str] = {}
    options: dict[str, bool] = {}

    for row in rows:
        if row.key.startswith(BUTTON_PREFIX):
            key = row.key.removeprefix(BUTTON_PREFIX)
            if key not in BUTTON_DEFINITIONS:
                continue
            try:
                button_overrides[key] = ButtonOverride.from_json(row.value)
            except (TypeError, ValueError):
                continue
        elif row.key.startswith(TEXT_PREFIX):
            suffix = row.key.removeprefix(TEXT_PREFIX)
            language, separator, text_key = suffix.partition(":")
            if separator and language in {"es", "en"} and text_key:
                text_overrides[(language, text_key)] = row.value
        elif row.key.startswith(OPTION_PREFIX):
            key = row.key.removeprefix(OPTION_PREFIX)
            if key in UI_OPTION_DEFAULTS:
                options[key] = row.value.strip().lower() in {"1", "true", "yes", "si", "sí", "on"}

    install_button_overrides(button_overrides)
    install_text_overrides(text_overrides)
    install_ui_options(options)


async def save_button_override(
    session: AsyncSession,
    button_key: str,
    override: ButtonOverride,
) -> None:
    key = button_setting_key(button_key)
    row = await session.get(AppSetting, key)
    raw = override.to_json()
    if row is None:
        session.add(AppSetting(key=key, value=raw))
    else:
        row.value = raw
    await session.commit()
    set_button_override_runtime(button_key, override)


async def reset_button_override(session: AsyncSession, button_key: str) -> None:
    key = button_setting_key(button_key)
    row = await session.get(AppSetting, key)
    if row is not None:
        await session.delete(row)
        await session.commit()
    set_button_override_runtime(button_key, None)


async def save_text_override(
    session: AsyncSession,
    language: str,
    text_key: str,
    template: str,
) -> None:
    key = text_setting_key(language, text_key)
    row = await session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=template))
    else:
        row.value = template
    await session.commit()
    set_text_override_runtime(language, text_key, template)


async def reset_text_override(session: AsyncSession, language: str, text_key: str) -> None:
    key = text_setting_key(language, text_key)
    row = await session.get(AppSetting, key)
    if row is not None:
        await session.delete(row)
        await session.commit()
    set_text_override_runtime(language, text_key, None)


async def save_ui_option(session: AsyncSession, option_key: str, enabled: bool) -> None:
    key = option_setting_key(option_key)
    raw = "true" if enabled else "false"
    row = await session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=raw))
    else:
        row.value = raw
    await session.commit()
    set_ui_option_runtime(option_key, enabled)


async def reset_all_ui_settings(session: AsyncSession) -> None:
    await session.execute(
        delete(AppSetting).where(
            AppSetting.key.like("ui_button:%")
            | AppSetting.key.like("ui_text:%")
            | AppSetting.key.like("ui_option:%")
        )
    )
    await session.commit()
    install_button_overrides({})
    install_text_overrides({})
    install_ui_options({})
