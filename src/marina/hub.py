from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import grpc

from lotsman.v1 import lotsman_pb2, lotsman_pb2_grpc
from marina.router import parse_job_id
from marina.seas.base import CostBreakdown, HostHandle, Offer, Sea, SeaStatus


class HostError(Exception):
    pass


class SeaNotFoundError(Exception):
    pass


@dataclass
class HostEntry:
    name: str
    target: str
    channel: grpc.Channel
    stub: lotsman_pb2_grpc.LotsmanServiceStub
    sea: str | None = None  # set when host was created via a Sea


class Hub:
    """In-process router from MCP tool calls to per-host gRPC stubs.

    Owns:
      • host registry (name → gRPC channel/stub),
      • sea registry (name → Sea instance for provisioning).

    `host_add` / `host_remove` register existing endpoints (manual mode).
    `host_create` / `host_destroy` go through a Sea (provisions/tears down a
    container or VM and updates the host registry as a side effect).
    """

    def __init__(self, seas: Iterable[Sea] | None = None) -> None:
        self.hosts: dict[str, HostEntry] = {}
        self.seas: dict[str, Sea] = {}
        if seas:
            for sea in seas:
                self.sea_register(sea)

    # ---- sea registry ----

    def sea_register(self, sea: Sea) -> None:
        if sea.name in self.seas:
            raise SeaNotFoundError(f"sea {sea.name!r} already registered")
        self.seas[sea.name] = sea

    def sea_get(self, name: str) -> Sea:
        if name not in self.seas:
            raise SeaNotFoundError(f"unknown sea: {name!r}")
        return self.seas[name]

    def sea_list(self) -> list[str]:
        return sorted(self.seas)

    # ---- host registry (manual) ----

    def host_add(self, name: str, target: str, *, sea: str | None = None) -> None:
        if name in self.hosts:
            raise HostError(f"host {name!r} already registered")
        channel = grpc.insecure_channel(target)
        stub = lotsman_pb2_grpc.LotsmanServiceStub(channel)
        self.hosts[name] = HostEntry(
            name=name, target=target, channel=channel, stub=stub, sea=sea
        )

    def host_remove(self, name: str) -> None:
        entry = self.hosts.pop(name, None)
        if entry is not None:
            entry.channel.close()

    def host_list(self, sea: str | None = None) -> list[str]:
        if sea is None:
            return sorted(self.hosts)
        return sorted(name for name, e in self.hosts.items() if e.sea == sea)

    # ---- host lifecycle (sea-driven) ----

    def host_create(
        self,
        sea: str,
        image: str,
        *,
        name: str | None = None,
        offer_id: str | None = None,
        disk_gb: int | None = None,
        onstart: str | None = None,
        env: dict[str, str] | None = None,
    ) -> HostHandle:
        s = self.sea_get(sea)
        handle = s.create(
            image,
            offer_id=offer_id,
            name=name,
            disk_gb=disk_gb,
            onstart=onstart,
            env=env,
        )
        if handle.name in self.hosts:
            # Sea created a container but the name collides with an existing
            # registry entry — roll back to keep state consistent.
            try:
                s.destroy(handle.name, kill_running=True)
            except Exception:  # pragma: no cover — best-effort rollback
                pass
            raise HostError(
                f"host {handle.name!r} already registered; sea provisioned "
                f"a duplicate and was rolled back"
            )
        self.host_add(handle.name, handle.grpc_target, sea=handle.sea)
        return handle

    def host_destroy(
        self, name: str, *, kill_running: bool = False
    ) -> None:
        """Tear down a host.

        For sea-managed hosts the owning sea is asked to destroy first
        (e.g. `docker rm -f <container>`), then the local registry entry is
        closed. For manually-added hosts (no sea) we just close the channel
        and forget — Marina doesn't own the box, so we can't destroy it.
        """
        entry = self.hosts.get(name)
        if entry is None:
            raise HostError(f"unknown host: {name!r}")
        if entry.sea is not None:
            self.sea_get(entry.sea).destroy(name, kill_running=kill_running)
        self.host_remove(name)

    def host_stop(self, name: str) -> None:
        entry = self.hosts.get(name)
        if entry is None or entry.sea is None:
            raise HostError(f"host {name!r} not stop-able (no sea)")
        self.sea_get(entry.sea).stop(name)

    def host_start(self, name: str) -> None:
        entry = self.hosts.get(name)
        if entry is None or entry.sea is None:
            raise HostError(f"host {name!r} not start-able (no sea)")
        self.sea_get(entry.sea).start(name)
        # Sea may have re-resolved the gRPC target on restart; resync.
        new_handle = self._sea_handle(entry.sea, name)
        if new_handle is not None and new_handle.grpc_target != entry.target:
            self.host_remove(name)
            self.host_add(name, new_handle.grpc_target, sea=entry.sea)

    def _sea_handle(self, sea_name: str, host_name: str) -> HostHandle | None:
        for h in self.sea_get(sea_name).list_instances():
            if h.name == host_name:
                return h
        return None

    # ---- sea queries ----

    def sea_search(
        self,
        sea: str,
        filters: dict[str, Any] | None = None,
        limit: int = 20,
    ) -> list[Offer]:
        return self.sea_get(sea).search(filters=filters, limit=limit)

    def seas_search(
        self,
        filters: dict[str, Any] | None = None,
        limit_per_sea: int = 7,
    ) -> tuple[list[Offer], dict[str, str]]:
        """Search every marketplace sea with one filter and merge the offers.

        Queries each registered sea whose ``is_marketplace`` is true (the dynamic,
        filterable catalogs — vast / verda / clore — not owned docker hosts),
        taking the top ``limit_per_sea`` from each. Returns the merged offers
        (each carries its ``.sea``; caller sorts) plus a ``{sea: error}`` map for
        any sea that failed (no creds / unreachable / bad filter) so the failure
        is surfaced, never silently dropped.
        """
        # `order` is applied globally to the merged set by the caller; don't
        # forward it per-sea, or a sea that lacks that sort key (e.g. Verda has
        # no zcpu) would raise and be dropped from the cross-sea result.
        per_sea = {k: v for k, v in (filters or {}).items() if k != "order"}
        merged: list[Offer] = []
        errors: dict[str, str] = {}
        for name in self.sea_list():
            sea = self.seas[name]
            if not getattr(sea, "is_marketplace", False):
                continue
            try:
                merged.extend(sea.search(filters=per_sea, limit=limit_per_sea))
            except Exception as exc:  # noqa: BLE001 — one sea must not sink the rest
                errors[name] = str(exc)
        return merged, errors

    def sea_recommend(
        self,
        sea: str,
        workload: str,
        budget_per_hour: float | None = None,
        min_hours: int | None = None,
    ) -> list[Offer]:
        return self.sea_get(sea).recommend(
            workload, budget_per_hour=budget_per_hour, min_hours=min_hours
        )

    def sea_status(self, sea: str) -> SeaStatus:
        return self.sea_get(sea).status()

    def cost_summary(self, sea: str | None = None) -> CostBreakdown:
        if sea is not None:
            return self.sea_get(sea).cost_summary()
        # aggregate across all seas
        per_host: list[tuple[str, float]] = []
        balance: float | None = None
        burn_24h = 0.0
        for s in self.seas.values():
            cb = s.cost_summary()
            per_host.extend(cb.per_host)
            burn_24h += cb.burn_rate_24h
            if cb.balance is not None:
                balance = (balance or 0.0) + cb.balance
        return CostBreakdown(
            sea=None,
            total_per_hour=sum(c for _, c in per_host),
            per_host=tuple(per_host),
            balance=balance,
            burn_rate_24h=burn_24h,
        )

    # ---- per-job RPC routing ----

    def _stub_for(self, host_name: str) -> lotsman_pb2_grpc.LotsmanServiceStub:
        if host_name not in self.hosts:
            raise HostError(f"unknown host: {host_name!r}")
        return self.hosts[host_name].stub

    def _route(self, job_id: str) -> lotsman_pb2_grpc.LotsmanServiceStub:
        host, _ = parse_job_id(job_id)
        return self._stub_for(host)

    def run(self, host: str, script: str, name: str = "") -> lotsman_pb2.RunResponse:
        req = lotsman_pb2.RunRequest(script=script)
        if name:
            req.name = name
        return self._stub_for(host).Run(req)

    def status(self, job_id: str) -> lotsman_pb2.StatusResponse:
        return self._route(job_id).Status(lotsman_pb2.StatusRequest(job_id=job_id))

    def kill(
        self, job_id: str, grace_sec: float = 10.0, force: bool = False
    ) -> lotsman_pb2.KillResponse:
        req = lotsman_pb2.KillRequest(job_id=job_id, grace_sec=grace_sec, force=force)
        return self._route(job_id).Kill(req)

    def logs(
        self,
        job_id: str,
        tail_lines: int | None = None,
        include_stderr: bool = False,
    ) -> lotsman_pb2.LogsResponse:
        req = lotsman_pb2.LogsRequest(job_id=job_id, include_stderr=include_stderr)
        if tail_lines is not None:
            req.tail_lines = tail_lines
        return self._route(job_id).Logs(req)

    def whoami(self, host: str) -> lotsman_pb2.WhoamiResponse:
        return self._stub_for(host).Whoami(lotsman_pb2.WhoamiRequest())

    # ---- filesystem ----

    def upload(
        self,
        host: str,
        path: str,
        content: bytes,
        *,
        create_parents: bool = False,
        overwrite: bool = False,
        executable: bool = False,
    ) -> lotsman_pb2.UploadResponse:
        return self._stub_for(host).Upload(
            lotsman_pb2.UploadRequest(
                path=path,
                content=content,
                create_parents=create_parents,
                overwrite=overwrite,
                executable=executable,
            )
        )

    def mkdir(
        self,
        host: str,
        path: str,
        *,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> lotsman_pb2.MkdirResponse:
        return self._stub_for(host).Mkdir(
            lotsman_pb2.MkdirRequest(path=path, parents=parents, exist_ok=exist_ok)
        )

    def ls(self, host: str, path: str) -> lotsman_pb2.LsResponse:
        return self._stub_for(host).Ls(lotsman_pb2.LsRequest(path=path))

    def stat(self, host: str, path: str) -> lotsman_pb2.StatResponse:
        return self._stub_for(host).Stat(lotsman_pb2.StatRequest(path=path))

    def cat(
        self, host: str, path: str, max_bytes: int | None = None
    ) -> lotsman_pb2.CatResponse:
        req = lotsman_pb2.CatRequest(path=path)
        if max_bytes is not None:
            req.max_bytes = max_bytes
        return self._stub_for(host).Cat(req)

    def disk_free(self, host: str, path: str) -> lotsman_pb2.DiskFreeResponse:
        return self._stub_for(host).DiskFree(lotsman_pb2.DiskFreeRequest(path=path))

    # ---- harvest / download ----

    def harvest_inventory(
        self, job_id: str, mode: str = "essential"
    ) -> lotsman_pb2.HarvestInventoryResponse:
        return self._route(job_id).HarvestInventory(
            lotsman_pb2.HarvestInventoryRequest(job_id=job_id, mode=mode)
        )

    def harvest(
        self,
        job_id: str,
        mode: str = "essential",
        format: str = "tar.gz",
    ) -> lotsman_pb2.HarvestResponse:
        return self._route(job_id).Harvest(
            lotsman_pb2.HarvestRequest(job_id=job_id, mode=mode, format=format)
        )

    def download(
        self, host: str, path: str, max_bytes: int | None = None
    ) -> lotsman_pb2.DownloadResponse:
        req = lotsman_pb2.DownloadRequest(path=path)
        if max_bytes is not None:
            req.max_bytes = max_bytes
        return self._stub_for(host).Download(req)

    def download_glob(
        self,
        host: str,
        pattern: str,
        format: str = "tar.gz",
        confirm_size_gb: float = 0.0,
    ) -> lotsman_pb2.DownloadGlobResponse:
        return self._stub_for(host).DownloadGlob(
            lotsman_pb2.DownloadGlobRequest(
                pattern=pattern,
                format=format,
                confirm_size_gb=confirm_size_gb,
            )
        )

    # ---- watchdogs / events ----

    def watchdog_list(
        self, job_id: str
    ) -> lotsman_pb2.WatchdogListResponse:
        return self._route(job_id).WatchdogList(
            lotsman_pb2.WatchdogListRequest(job_id=job_id)
        )

    def watchdog_history(
        self, job_id: str, since_unix_ms: int = 0
    ) -> lotsman_pb2.WatchdogHistoryResponse:
        return self._route(job_id).WatchdogHistory(
            lotsman_pb2.WatchdogHistoryRequest(
                job_id=job_id, since_unix_ms=since_unix_ms
            )
        )

    def events(
        self, job_id: str, since_unix_ms: int = 0
    ) -> lotsman_pb2.WatchdogHistoryResponse:
        """Snapshot of past events for a job. Alias of watchdog_history."""
        return self.watchdog_history(job_id, since_unix_ms=since_unix_ms)

    def events_all(
        self, since_unix_ms: int = 0, hosts: list[str] | None = None
    ) -> dict[str, list[lotsman_pb2.Event]]:
        """Aggregate events across all (or filtered) hosts.

        One round-trip per host via EventsHistoryAll. A host whose RPC
        fails contributes an empty list (the failure is silently swallowed
        — this aggregator is observability, not a transactional barrier).
        """
        targets = hosts if hosts is not None else list(self.hosts)
        out: dict[str, list[lotsman_pb2.Event]] = {}
        for name in targets:
            entry = self.hosts.get(name)
            if entry is None:
                out[name] = []
                continue
            try:
                resp = entry.stub.EventsHistoryAll(
                    lotsman_pb2.EventsHistoryAllRequest(since_unix_ms=since_unix_ms)
                )
                out[name] = list(resp.events)
            except grpc.RpcError:
                out[name] = []
        return out

    def shutdown(self) -> None:
        for entry in self.hosts.values():
            entry.channel.close()
        self.hosts.clear()
        self.seas.clear()
