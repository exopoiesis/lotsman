from __future__ import annotations

from marina.seas.base import Sea


class SeaRegistryError(Exception):
    pass


_REGISTRY: dict[str, Sea] = {}


def register_sea(sea: Sea) -> None:
    """Register a Sea instance under its `.name`. Raises on duplicate."""
    if sea.name in _REGISTRY:
        raise SeaRegistryError(f"sea {sea.name!r} already registered")
    _REGISTRY[sea.name] = sea


def get_sea(name: str) -> Sea:
    if name not in _REGISTRY:
        raise SeaRegistryError(f"unknown sea: {name!r}")
    return _REGISTRY[name]


def list_seas() -> list[str]:
    return sorted(_REGISTRY)


def clear_seas() -> None:
    """Test helper — wipe registry between tests."""
    _REGISTRY.clear()
