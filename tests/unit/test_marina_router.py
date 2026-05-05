from __future__ import annotations

import pytest

from marina.router import JobIdError, parse_job_id

pytestmark = pytest.mark.unit


def test_parse_job_id_valid():
    host, suffix = parse_job_id("w3/01HPQR3NX9ABC123XYZ")
    assert host == "w3"
    assert suffix == "01HPQR3NX9ABC123XYZ"


def test_parse_job_id_no_slash_raises():
    with pytest.raises(JobIdError):
        parse_job_id("nohost")


def test_parse_job_id_empty_host_raises():
    with pytest.raises(JobIdError):
        parse_job_id("/suffix")


def test_parse_job_id_empty_suffix_raises():
    with pytest.raises(JobIdError):
        parse_job_id("host/")


def test_parse_job_id_extra_slashes_keeps_full_suffix():
    host, suffix = parse_job_id("host/a/b/c")
    assert host == "host"
    assert suffix == "a/b/c"
