from __future__ import annotations

from google.protobuf.message import Message

JOB_STATE_UNSPECIFIED: int
PENDING: int
RUNNING: int
DONE: int
FAILED: int
KILLED: int
ORPHANED: int


class _ProtoMessage(Message):
    def HasField(self, field_name: str) -> bool: ...


class RunRequest(_ProtoMessage):
    script: str
    name: str
    wd: str
    env: dict[str, str]
    def __init__(
        self,
        script: str = "",
        name: str | None = None,
        wd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None: ...


class RunResponse(_ProtoMessage):
    job_id: str
    state: int
    def __init__(self, job_id: str = "", state: int = 0) -> None: ...


class StatusRequest(_ProtoMessage):
    job_id: str
    def __init__(self, job_id: str = "") -> None: ...


class StatusResponse(_ProtoMessage):
    job_id: str
    state: int
    exit_code: int
    started_at_unix_ms: int
    finished_at_unix_ms: int
    def __init__(self, job_id: str = "", state: int = 0) -> None: ...


class KillRequest(_ProtoMessage):
    job_id: str
    grace_sec: float
    force: bool
    def __init__(
        self,
        job_id: str = "",
        grace_sec: float | None = None,
        force: bool | None = None,
    ) -> None: ...


class KillResponse(_ProtoMessage):
    job_id: str
    killed: bool
    state: int
    exit_code: int
    def __init__(
        self,
        job_id: str = "",
        killed: bool = False,
        state: int = 0,
    ) -> None: ...


class LogsRequest(_ProtoMessage):
    job_id: str
    tail_lines: int
    include_stderr: bool
    def __init__(
        self,
        job_id: str = "",
        tail_lines: int | None = None,
        include_stderr: bool = False,
    ) -> None: ...


class LogsResponse(_ProtoMessage):
    job_id: str
    stdout: bytes
    stderr: bytes
    stdout_total_bytes: int
    stderr_total_bytes: int
    def __init__(
        self,
        job_id: str = "",
        stdout: bytes = b"",
        stderr: bytes = b"",
        stdout_total_bytes: int = 0,
        stderr_total_bytes: int = 0,
    ) -> None: ...


class TailFollowRequest(_ProtoMessage):
    job_id: str
    include_stderr: bool
    from_offset_stdout: int
    from_offset_stderr: int
    def __init__(
        self,
        job_id: str = "",
        include_stderr: bool = False,
        from_offset_stdout: int | None = None,
        from_offset_stderr: int | None = None,
    ) -> None: ...


class LogChunk(_ProtoMessage):
    stdout: bytes
    stderr: bytes
    stdout_offset_after: int
    stderr_offset_after: int
    job_terminal: bool
    state: int
    def __init__(
        self,
        stdout: bytes = b"",
        stderr: bytes = b"",
        stdout_offset_after: int = 0,
        stderr_offset_after: int = 0,
        job_terminal: bool = False,
        state: int = 0,
    ) -> None: ...


class WhoamiRequest(_ProtoMessage):
    def __init__(self) -> None: ...


class WhoamiResponse(_ProtoMessage):
    lotsman_version: str
    tool: str
    tool_version: str
    image: str
    image_tag: str
    default_omp: int
    default_npool: int
    mpirun_required: bool
    known_pitfalls: list[str]


class Event(_ProtoMessage):
    job_id: str
    watchdog_name: str
    event_type: str
    unix_ms: int
    detail: str
    severity: str
    data: dict[str, str]
    def __init__(
        self,
        job_id: str = "",
        watchdog_name: str = "",
        event_type: str = "",
        unix_ms: int = 0,
        detail: str = "",
        severity: str = "",
        data: dict[str, str] | None = None,
    ) -> None: ...


class EventsRequest(_ProtoMessage):
    job_id: str
    since_unix_ms: int
    def __init__(self, job_id: str = "", since_unix_ms: int = 0) -> None: ...


class Watchdog(_ProtoMessage):
    name: str
    fired: bool
    action: str
    interval_sec: float
    def __init__(
        self,
        name: str = "",
        fired: bool = False,
        action: str = "",
        interval_sec: float = 0.0,
    ) -> None: ...


class WatchdogListRequest(_ProtoMessage):
    job_id: str
    def __init__(self, job_id: str = "") -> None: ...


class WatchdogListResponse(_ProtoMessage):
    job_id: str
    watchdogs: list[Watchdog]
    def __init__(
        self,
        job_id: str = "",
        watchdogs: list[Watchdog] | None = None,
    ) -> None: ...


class WatchdogHistoryRequest(_ProtoMessage):
    job_id: str
    since_unix_ms: int
    def __init__(self, job_id: str = "", since_unix_ms: int = 0) -> None: ...


class WatchdogHistoryResponse(_ProtoMessage):
    job_id: str
    events: list[Event]
    def __init__(self, job_id: str = "", events: list[Event] | None = None) -> None: ...


class EventsHistoryAllRequest(_ProtoMessage):
    since_unix_ms: int
    def __init__(self, since_unix_ms: int = 0) -> None: ...


class EventsHistoryAllResponse(_ProtoMessage):
    events: list[Event]
    def __init__(self, events: list[Event] | None = None) -> None: ...


class UploadRequest(_ProtoMessage):
    path: str
    content: bytes
    create_parents: bool
    overwrite: bool
    executable: bool
    def __init__(
        self,
        path: str = "",
        content: bytes = b"",
        create_parents: bool = False,
        overwrite: bool = False,
        executable: bool = False,
    ) -> None: ...


class UploadResponse(_ProtoMessage):
    path: str
    bytes_written: int
    sha256: str
    def __init__(
        self,
        path: str = "",
        bytes_written: int = 0,
        sha256: str = "",
    ) -> None: ...


class MkdirRequest(_ProtoMessage):
    path: str
    parents: bool
    exist_ok: bool
    def __init__(
        self,
        path: str = "",
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None: ...


class MkdirResponse(_ProtoMessage):
    path: str
    def __init__(self, path: str = "") -> None: ...


class LsRequest(_ProtoMessage):
    path: str
    def __init__(self, path: str = "") -> None: ...


class DirEntry(_ProtoMessage):
    name: str
    path: str
    is_dir: bool
    size_bytes: int
    mtime_unix_ms: int
    def __init__(
        self,
        name: str = "",
        path: str = "",
        is_dir: bool = False,
        size_bytes: int = 0,
        mtime_unix_ms: int = 0,
    ) -> None: ...


class LsResponse(_ProtoMessage):
    path: str
    entries: list[DirEntry]
    def __init__(
        self,
        path: str = "",
        entries: list[DirEntry] | None = None,
    ) -> None: ...


class StatRequest(_ProtoMessage):
    path: str
    def __init__(self, path: str = "") -> None: ...


class StatResponse(_ProtoMessage):
    path: str
    exists: bool
    is_dir: bool
    size_bytes: int
    mtime_unix_ms: int
    def __init__(
        self,
        path: str = "",
        exists: bool = False,
        is_dir: bool = False,
        size_bytes: int = 0,
        mtime_unix_ms: int = 0,
    ) -> None: ...


class CatRequest(_ProtoMessage):
    path: str
    max_bytes: int
    def __init__(self, path: str = "", max_bytes: int | None = None) -> None: ...


class CatResponse(_ProtoMessage):
    path: str
    content: bytes
    total_bytes: int
    truncated: bool
    def __init__(
        self,
        path: str = "",
        content: bytes = b"",
        total_bytes: int = 0,
        truncated: bool = False,
    ) -> None: ...


class DiskFreeRequest(_ProtoMessage):
    path: str
    def __init__(self, path: str = "") -> None: ...


class DiskFreeResponse(_ProtoMessage):
    path: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    def __init__(
        self,
        path: str = "",
        total_bytes: int = 0,
        used_bytes: int = 0,
        free_bytes: int = 0,
    ) -> None: ...


class HarvestEntry(_ProtoMessage):
    path: str
    size_bytes: int
    included: bool
    reason: str
    def __init__(
        self,
        path: str = "",
        size_bytes: int = 0,
        included: bool = False,
        reason: str = "",
    ) -> None: ...


class HarvestInventoryRequest(_ProtoMessage):
    job_id: str
    mode: str
    def __init__(self, job_id: str = "", mode: str = "") -> None: ...


class HarvestInventoryResponse(_ProtoMessage):
    job_id: str
    mode: str
    entries: list[HarvestEntry]
    included_bytes: int
    def __init__(
        self,
        job_id: str = "",
        mode: str = "",
        entries: list[HarvestEntry] | None = None,
        included_bytes: int = 0,
    ) -> None: ...


class HarvestRequest(_ProtoMessage):
    job_id: str
    mode: str
    format: str
    def __init__(
        self,
        job_id: str = "",
        mode: str = "",
        format: str = "",
    ) -> None: ...


class HarvestResponse(_ProtoMessage):
    job_id: str
    mode: str
    format: str
    archive_path: str
    archive_bytes: int
    sha256: str
    entries: list[HarvestEntry]
    def __init__(
        self,
        job_id: str = "",
        mode: str = "",
        format: str = "",
        archive_path: str = "",
        archive_bytes: int = 0,
        sha256: str = "",
        entries: list[HarvestEntry] | None = None,
    ) -> None: ...


class DownloadRequest(_ProtoMessage):
    path: str
    max_bytes: int
    def __init__(self, path: str = "", max_bytes: int | None = None) -> None: ...


class DownloadResponse(_ProtoMessage):
    path: str
    content: bytes
    total_bytes: int
    truncated: bool
    def __init__(
        self,
        path: str = "",
        content: bytes = b"",
        total_bytes: int = 0,
        truncated: bool = False,
    ) -> None: ...


class DownloadGlobRequest(_ProtoMessage):
    pattern: str
    format: str
    confirm_size_gb: float
    def __init__(
        self,
        pattern: str = "",
        format: str = "",
        confirm_size_gb: float = 0.0,
    ) -> None: ...


class DownloadGlobResponse(_ProtoMessage):
    pattern: str
    format: str
    archive_path: str
    archive_bytes: int
    sha256: str
    entries: list[HarvestEntry]
    def __init__(
        self,
        pattern: str = "",
        format: str = "",
        archive_path: str = "",
        archive_bytes: int = 0,
        sha256: str = "",
        entries: list[HarvestEntry] | None = None,
    ) -> None: ...
