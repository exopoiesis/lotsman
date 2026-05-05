from __future__ import annotations

from collections.abc import Iterator

import pytest

from marina.seas.registry import (
    SeaRegistryError,
    clear_seas,
    get_sea,
    list_seas,
    register_sea,
)

pytestmark = pytest.mark.unit


class FakeSea:
    """Duck-typed Sea — registry only needs `.name`."""

    def __init__(self, name: str) -> None:
        self.name = name


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    clear_seas()
    yield
    clear_seas()


def test_register_and_get() -> None:
    sea = FakeSea("gomer")
    register_sea(sea)  # type: ignore[arg-type]
    assert get_sea("gomer") is sea


def test_get_unknown_raises() -> None:
    with pytest.raises(SeaRegistryError, match="unknown sea"):
        get_sea("nonexistent")


def test_double_register_raises() -> None:
    register_sea(FakeSea("gomer"))  # type: ignore[arg-type]
    with pytest.raises(SeaRegistryError, match="already registered"):
        register_sea(FakeSea("gomer"))  # type: ignore[arg-type]


def test_list_seas_sorted() -> None:
    register_sea(FakeSea("vast"))  # type: ignore[arg-type]
    register_sea(FakeSea("gomer"))  # type: ignore[arg-type]
    register_sea(FakeSea("loki"))  # type: ignore[arg-type]
    assert list_seas() == ["gomer", "loki", "vast"]


def test_list_seas_empty() -> None:
    assert list_seas() == []


def test_clear_seas_wipes_everything() -> None:
    register_sea(FakeSea("a"))  # type: ignore[arg-type]
    register_sea(FakeSea("b"))  # type: ignore[arg-type]
    clear_seas()
    assert list_seas() == []
    with pytest.raises(SeaRegistryError):
        get_sea("a")
