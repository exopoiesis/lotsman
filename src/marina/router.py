from __future__ import annotations


class JobIdError(ValueError):
    pass


def parse_job_id(job_id: str) -> tuple[str, str]:
    if "/" not in job_id:
        raise JobIdError(f"jobId missing host prefix: {job_id!r}")
    host, _, suffix = job_id.partition("/")
    if not host:
        raise JobIdError(f"jobId has empty host: {job_id!r}")
    if not suffix:
        raise JobIdError(f"jobId has empty suffix: {job_id!r}")
    return host, suffix
