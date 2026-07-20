from __future__ import annotations

from app.services.provider_registry import ProviderConfig, ProviderRegistry, ProviderRuntime


class _DummyClient:
    async def close(self) -> None:
        return None


def _runtime(code: str, name: str) -> ProviderRuntime:
    return ProviderRuntime(
        config=ProviderConfig(
            code=code,
            name=name,
            base_url="https://provider.example/v1",
            api_key="secret",
        ),
        client=_DummyClient(),  # type: ignore[arg-type]
    )


def test_admin_provider_names_can_be_built_from_registry_values() -> None:
    registry = ProviderRegistry(
        [
            _runtime("provider_two", "Proveedor Dos"),
            _runtime("provider_one", "Proveedor Uno"),
        ]
    )

    provider_names = {runtime.config.code: runtime.config.name for runtime in registry.values()}

    assert provider_names == {
        "provider_two": "Proveedor Dos",
        "provider_one": "Proveedor Uno",
    }
    assert dict(registry.items()) == {
        "provider_two": registry.get("provider_two"),
        "provider_one": registry.get("provider_one"),
    }
