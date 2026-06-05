from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import grpc

from lotsman.v1 import lotsman_pb2


class LotsmanServiceStub:
    def __init__(self, channel: grpc.Channel) -> None: ...
    def Run(
        self, request: lotsman_pb2.RunRequest, timeout: float | None = None
    ) -> lotsman_pb2.RunResponse: ...
    def Status(
        self, request: lotsman_pb2.StatusRequest, timeout: float | None = None
    ) -> lotsman_pb2.StatusResponse: ...
    def Kill(
        self, request: lotsman_pb2.KillRequest, timeout: float | None = None
    ) -> lotsman_pb2.KillResponse: ...
    def Logs(
        self, request: lotsman_pb2.LogsRequest, timeout: float | None = None
    ) -> lotsman_pb2.LogsResponse: ...
    def TailFollow(
        self, request: lotsman_pb2.TailFollowRequest, timeout: float | None = None
    ) -> Iterator[lotsman_pb2.LogChunk]: ...
    def Whoami(
        self, request: lotsman_pb2.WhoamiRequest, timeout: float | None = None
    ) -> lotsman_pb2.WhoamiResponse: ...
    def Events(
        self, request: lotsman_pb2.EventsRequest, timeout: float | None = None
    ) -> Iterator[lotsman_pb2.Event]: ...
    def WatchdogList(
        self, request: lotsman_pb2.WatchdogListRequest, timeout: float | None = None
    ) -> lotsman_pb2.WatchdogListResponse: ...
    def WatchdogHistory(
        self, request: lotsman_pb2.WatchdogHistoryRequest, timeout: float | None = None
    ) -> lotsman_pb2.WatchdogHistoryResponse: ...
    def EventsHistoryAll(
        self, request: lotsman_pb2.EventsHistoryAllRequest, timeout: float | None = None
    ) -> lotsman_pb2.EventsHistoryAllResponse: ...
    def Upload(
        self, request: lotsman_pb2.UploadRequest, timeout: float | None = None
    ) -> lotsman_pb2.UploadResponse: ...
    def Mkdir(
        self, request: lotsman_pb2.MkdirRequest, timeout: float | None = None
    ) -> lotsman_pb2.MkdirResponse: ...
    def Ls(
        self, request: lotsman_pb2.LsRequest, timeout: float | None = None
    ) -> lotsman_pb2.LsResponse: ...
    def Stat(
        self, request: lotsman_pb2.StatRequest, timeout: float | None = None
    ) -> lotsman_pb2.StatResponse: ...
    def Cat(
        self, request: lotsman_pb2.CatRequest, timeout: float | None = None
    ) -> lotsman_pb2.CatResponse: ...
    def DiskFree(
        self, request: lotsman_pb2.DiskFreeRequest, timeout: float | None = None
    ) -> lotsman_pb2.DiskFreeResponse: ...
    def HarvestInventory(
        self,
        request: lotsman_pb2.HarvestInventoryRequest,
        timeout: float | None = None,
    ) -> lotsman_pb2.HarvestInventoryResponse: ...
    def Harvest(
        self, request: lotsman_pb2.HarvestRequest, timeout: float | None = None
    ) -> lotsman_pb2.HarvestResponse: ...
    def Download(
        self, request: lotsman_pb2.DownloadRequest, timeout: float | None = None
    ) -> lotsman_pb2.DownloadResponse: ...
    def DownloadGlob(
        self, request: lotsman_pb2.DownloadGlobRequest, timeout: float | None = None
    ) -> lotsman_pb2.DownloadGlobResponse: ...


class LotsmanServiceServicer:
    pass


def add_LotsmanServiceServicer_to_server(
    servicer: Any, server: grpc.Server
) -> None: ...
