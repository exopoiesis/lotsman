from __future__ import annotations

import pytest

from lotsman.platform.sanitize import sanitize_script

pytestmark = pytest.mark.unit


def test_sanitize_replaces_em_dash():
    assert sanitize_script("echo —flag") == "echo --flag"


def test_sanitize_idempotent_on_clean_script():
    assert sanitize_script("echo --flag\n") == "echo --flag\n"


def test_sanitize_handles_multiple_em_dashes():
    assert sanitize_script("a —x b —y c") == "a --x b --y c"


def test_sanitize_preserves_unicode_in_strings():
    assert sanitize_script("echo 'привет мир'") == "echo 'привет мир'"


def test_sanitize_normalizes_crlf_to_lf():
    assert sanitize_script("echo a\r\nls\r\n") == "echo a\nls\n"


def test_sanitize_handles_mixed_crlf_and_lf():
    assert sanitize_script("a\r\nb\nc\r\n") == "a\nb\nc\n"


def test_sanitize_combines_emdash_and_crlf():
    assert sanitize_script("echo —flag\r\nexit 0\r\n") == "echo --flag\nexit 0\n"
