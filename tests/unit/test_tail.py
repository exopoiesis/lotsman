from __future__ import annotations

import pytest

from lotsman.platform.logs import tail_bytes

pytestmark = pytest.mark.unit


def test_tail_bytes_returns_last_n_lines():
    assert tail_bytes(b"a\nb\nc\nd\n", 2) == b"c\nd\n"


def test_tail_bytes_keeps_trailing_no_newline():
    assert tail_bytes(b"a\nb\nc", 2) == b"b\nc"


def test_tail_bytes_n_zero_returns_empty():
    assert tail_bytes(b"a\nb\n", 0) == b""


def test_tail_bytes_n_negative_returns_empty():
    assert tail_bytes(b"a\nb\n", -3) == b""


def test_tail_bytes_n_larger_than_file_returns_all():
    assert tail_bytes(b"a\nb\n", 100) == b"a\nb\n"


def test_tail_bytes_empty_input():
    assert tail_bytes(b"", 5) == b""
