from __future__ import annotations

import secrets
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Broadcast, utcnow
from app.services.notifications import BroadcastResult


@dataclass(frozen=True, slots=True)
class BroadcastRecord:
    id: int
    code: str


def _broadcast_code() -> str:
    return f"BC-{secrets.token_hex(5).upper()}"


async def create_broadcast(
    session: AsyncSession,
    *,
    admin_telegram_id: int,
    kind: str,
    text: str,
) -> BroadcastRecord:
    record = Broadcast(
        broadcast_code=_broadcast_code(),
        admin_telegram_id=admin_telegram_id,
        kind=kind[:24],
        text=text[:4000],
        status="processing",
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return BroadcastRecord(id=record.id, code=record.broadcast_code)


async def complete_broadcast(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    broadcast_id: int,
    result: BroadcastResult,
) -> None:
    async with session_factory() as session:
        record = await session.scalar(select(Broadcast).where(Broadcast.id == broadcast_id))
        if record is None:
            return
        record.status = "completed"
        record.attempted = result.attempted
        record.sent = result.sent
        record.blocked = result.blocked
        record.failed = result.failed
        record.completed_at = utcnow()
        await session.commit()


async def fail_broadcast(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    broadcast_id: int,
) -> None:
    async with session_factory() as session:
        record = await session.scalar(select(Broadcast).where(Broadcast.id == broadcast_id))
        if record is None:
            return
        record.status = "failed"
        record.completed_at = utcnow()
        await session.commit()
