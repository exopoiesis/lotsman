from __future__ import annotations

from dataclasses import dataclass

import grpc

from lotsman.v1 import lotsman_pb2, lotsman_pb2_grpc
from marina.router import parse_job_id


class HostError(Exception):
    pass


@dataclass
class HostEntry:
    name: str
    target: str
    channel: grpc.Channel
    stub: lotsman_pb2_grpc.LotsmanServiceStub


class Hub:
    def __init__(self) -> None:
        self.hosts: dict[str, HostEntry] = {}

    def host_add(self, name: str, target: str) -> None:
        if name in self.hosts:
            raise HostError(f"host {name!r} already registered")
        channel = grpc.insecure_channel(target)
        stub = lotsman_pb2_grpc.LotsmanServiceStub(channel)
        self.hosts[name] = HostEntry(name=name, target=target, channel=channel, stub=stub)

    def host_remove(self, name: str) -> None:
        entry = self.hosts.pop(name, None)
        if entry is not None:
            entry.channel.close()

    def host_list(self) -> list[str]:
        return list(self.hosts)

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

    def shutdown(self) -> None:
        for entry in self.hosts.values():
            entry.channel.close()
        self.hosts.clear()
