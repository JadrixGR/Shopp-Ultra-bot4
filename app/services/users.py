from __future__ import annotations

from decimal import Decimal

from aiogram.types import User as TelegramUser
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User


async def get_or_create_user(session: AsyncSession, telegram_user: TelegramUser) -> User:
    user = await session.scalar(select(User).where(User.telegram_id == telegram_user.id))
    username = telegram_user.username
    first_name = telegram_user.first_name or ""
    if user is None:
        user = User(
            telegram_id=telegram_user.id,
            username=username,
            first_name=first_name,
            language="es",
            balance=Decimal("0.00"),
        )
        session.add(user)
        await session.flush()
    else:
        user.username = username
        user.first_name = first_name
    await session.commit()
    return user


async def get_user_by_telegram_id(session: AsyncSession, telegram_id: int) -> User | None:
    return await session.scalar(select(User).where(User.telegram_id == telegram_id))


async def set_user_language(session: AsyncSession, telegram_id: int, language: str) -> None:
    if language not in {"es", "en"}:
        raise ValueError("Unsupported language")
    await session.execute(
        update(User).where(User.telegram_id == telegram_id).values(language=language)
    )
    await session.commit()
