from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.services.binance import BinancePayHistoryClient
from app.services.prodseller import ProdSellerClient
from app.services.provider_registry import ProviderRegistry

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AppContext:
    config: Settings
    session_factory: async_sessionmaker[AsyncSession]
    binance: BinancePayHistoryClient | None
    providers: ProviderRegistry = field(default_factory=ProviderRegistry)
    background_tasks: set[asyncio.Task[Any]] = field(default_factory=set)

    @property
    def prodseller(self) -> ProdSellerClient | None:
        """Compatibility alias used by callbacks from older releases."""

        runtime = self.providers.get("prodseller")
        return runtime.client if runtime is not None else None

    def spawn(self, awaitable: Awaitable[Any]) -> asyncio.Task[Any]:
        task = asyncio.create_task(awaitable)
        self.background_tasks.add(task)

        def _done(completed: asyncio.Task[Any]) -> None:
            self.background_tasks.discard(completed)
            if completed.cancelled():
                return
            try:
                completed.result()
            except Exception:
                logger.exception("Background task failed")

        task.add_done_callback(_done)
        return task

    async def shutdown_background_tasks(self) -> None:
        if not self.background_tasks:
            return
        tasks = tuple(self.background_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.background_tasks.clear()
