from __future__ import annotations

import pytest

from marina.seas.clore_sea import CloreSea
from marina.seas.docker_sea import DockerSea
from marina.seas.factory import SeaConfigError, build_sea
from marina.seas.vast_sea import VastSea
from marina.seas.verda_sea import VerdaSea

pytestmark = pytest.mark.unit


def _docker_raw(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        type="docker_sea",
        docker_context="gomer",
        gpu_model="RTX 4070",
        gpu_count=1,
        vram_gb=12,
        fp64_native=False,
        cpu_ghz=5.7,
        cpu_cores=8,
        ram_gb=32,
        disk_gb=500,
    )
    base.update(overrides)
    return base


def test_build_docker_sea_basic() -> None:
    sea = build_sea("gomer", "docker_sea", _docker_raw())
    assert isinstance(sea, DockerSea)
    assert sea.name == "gomer"
    assert sea.docker_context == "gomer"
    assert sea.capability.gpu_model == "RTX 4070"
    assert sea.capability.fp64_native is False
    # capability default applied
    assert sea.capability.reliability == 1.0


def test_build_docker_sea_passes_optionals() -> None:
    raw = _docker_raw(price_per_hour=0.05, reliability=0.92, inet_down_mbps=950.0)
    sea = build_sea("paid", "docker_sea", raw)
    assert sea.capability.price_per_hour == 0.05
    assert sea.capability.reliability == 0.92
    assert sea.capability.inet_down_mbps == 950.0


def test_build_docker_sea_missing_context_raises() -> None:
    raw = _docker_raw()
    raw.pop("docker_context")
    with pytest.raises(SeaConfigError, match="docker_context"):
        build_sea("broken", "docker_sea", raw)


def test_build_docker_sea_missing_capability_raises() -> None:
    raw = _docker_raw()
    raw.pop("vram_gb")
    raw.pop("gpu_count")
    with pytest.raises(SeaConfigError, match="missing capability fields"):
        build_sea("broken", "docker_sea", raw)


def test_build_unknown_sea_type_raises() -> None:
    with pytest.raises(SeaConfigError, match="unknown sea type"):
        build_sea("x", "vast", {})


def test_build_docker_sea_tolerates_unknown_fields() -> None:
    """Forward-compat: extra TOML fields shouldn't crash the factory."""
    raw = _docker_raw(future_field="ignore me", another=123)
    sea = build_sea("gomer", "docker_sea", raw)
    assert sea.name == "gomer"


def test_build_vast_sea_basic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAST_API_KEY", "secret")
    sea = build_sea("vast", "vast_sea", {"type": "vast_sea"})
    assert isinstance(sea, VastSea)
    assert sea.name == "vast"
    assert sea.api_key_env == "VAST_API_KEY"


def test_build_vast_sea_custom_api_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_VAST_KEY", "secret")
    raw = {"type": "vast_sea", "api_key_env": "MY_VAST_KEY", "poll_interval_s": 3}
    sea = build_sea("vast", "vast_sea", raw)
    assert sea.api_key_env == "MY_VAST_KEY"
    assert sea._poll_interval_s == 3.0


def test_build_verda_sea_basic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERDA_CLIENT_ID", "cid")
    monkeypatch.setenv("VERDA_CLIENT_SECRET", "csecret")
    raw = {
        "type": "verda_sea",
        "default_region": "ICE-01",
        "ssh_pubkey_path": "/keys/verda.pub",
        "default_disk_gb": 200,
        "poll_interval_s": 4,
    }
    sea = build_sea("verda", "verda_sea", raw)
    assert isinstance(sea, VerdaSea)
    assert sea.name == "verda"
    assert sea._region == "ICE-01"
    assert sea._ssh_pubkey_path == "/keys/verda.pub"
    assert sea._default_disk_gb == 200
    assert sea._poll_interval_s == 4.0
    # secret came from the env, not the TOML (like vast's api_key_env)
    assert sea._client_id == "cid"
    assert sea._client_secret == "csecret"


def test_build_verda_sea_custom_env_var_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MY_VERDA_ID", "the-id")
    monkeypatch.setenv("MY_VERDA_SECRET", "the-secret")
    raw = {
        "type": "verda_sea",
        "client_id_env": "MY_VERDA_ID",
        "client_secret_env": "MY_VERDA_SECRET",
    }
    sea = build_sea("verda", "verda_sea", raw)
    assert sea._client_id == "the-id"
    assert sea._client_secret == "the-secret"


def test_build_clore_sea_basic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLORE_API_KEY", "tok")
    raw = {
        "type": "clore_sea",
        "ssh_pubkey_path": "/keys/vast.pub",
        "default_currency": "CLORE-Blockchain",
        "poll_interval_s": 20,
    }
    sea = build_sea("clore", "clore_sea", raw)
    assert isinstance(sea, CloreSea)
    assert sea.name == "clore"
    assert sea._api_key == "tok"          # from env, not TOML
    assert sea._default_currency == "CLORE-Blockchain"
    assert sea._poll_interval_s == 20.0


def test_build_clore_sea_custom_api_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_CLORE_KEY", "k")
    raw = {"type": "clore_sea", "api_key_env": "MY_CLORE_KEY"}
    sea = build_sea("clore", "clore_sea", raw)
    assert sea.api_key_env == "MY_CLORE_KEY"
    assert sea._api_key == "k"


def test_build_vast_sea_passes_ssh_and_port_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VAST_API_KEY", "secret")
    raw = {
        "type": "vast_sea",
        "ssh_user": "ubuntu",
        "ssh_key_path": "/keys/vast",
        "container_grpc_port": 60000,
        "ssh_ready_timeout_s": 90,
    }
    sea = build_sea("vast", "vast_sea", raw)
    assert sea._ssh_user == "ubuntu"
    assert sea._ssh_key_path == "/keys/vast"
    assert sea._container_grpc_port == 60000
    assert sea._ssh_ready_timeout_s == 90.0
