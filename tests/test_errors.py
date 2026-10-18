"""The error catalog: one table, and nothing raised outside it."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from infoforge import answer, errors, kiwix, zimify
from infoforge.config import ConfigError
from infoforge.errors import (
    CATALOG,
    REASON_BY_EXIT,
    InfoForgeError,
    LLMUnreachableError,
    NoGroundingError,
    error_info,
    info_for_code,
    spec_for,
)

SRC = Path(__file__).resolve().parent.parent / "src" / "infoforge"


def test_exit_codes_and_reasons_are_the_2_0_contract():
    # DisPatch and other wrappers script against these; they must not move.
    assert (errors.EXIT_OK, errors.EXIT_CONFIG, errors.EXIT_NO_GROUNDING,
            errors.EXIT_CITATION, errors.EXIT_LLM, errors.EXIT_NOT_IN_SOURCES) == (0, 2, 3, 4, 5, 6)
    assert REASON_BY_EXIT == {
        0: "answered", 2: "config", 3: "no_sources", 4: "uncited",
        5: "llm_unreachable", 6: "not_in_sources",
    }


def test_every_row_is_complete_and_unique():
    assert len(CATALOG) == len(errors._ROWS)
    for code, spec in CATALOG.items():
        assert re.fullmatch(r"[a-z][a-z_]+", code)
        assert spec.code == code
        assert spec.message and spec.fix and spec.summary
        assert spec.docs == f"docs/reference/errors.md#{code}"
        assert spec.reason == REASON_BY_EXIT.get(spec.exit, "error")


def test_exception_classes_agree_with_the_catalog():
    for cls in (InfoForgeError, ConfigError, NoGroundingError, LLMUnreachableError,
                kiwix.KiwixError, answer.LLMRequestError, zimify.ZimifyError):
        assert cls.code in CATALOG, cls
        assert CATALOG[cls.code].exit == cls.exit_code, cls


def test_every_code_named_in_the_source_is_catalogued():
    used = set()
    for path in SRC.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        used |= set(re.findall(r'code="([a-z_]+)"', text))
        used |= set(re.findall(r'^\s+code = "([a-z_]+)"', text, flags=re.M))
        used |= set(re.findall(r'info_for_code\("([a-z_]+)"', text))
    assert used, "the scan found nothing - the pattern is stale"
    assert used <= set(CATALOG), used - set(CATALOG)


def test_error_info_has_code_message_fix_docs():
    exc = kiwix.KiwixError("podman gone", code="podman_missing")
    info = error_info(exc)
    assert info["code"] == "podman_missing"
    assert info["message"] == "podman gone"
    assert info["fix"] == CATALOG["podman_missing"].fix
    assert info["docs"].endswith("#podman_missing")


def test_an_explicit_fix_overrides_the_catalog_text():
    exc = InfoForgeError("x", fix="do this instead")
    assert error_info(exc)["fix"] == "do this instead"


def test_spec_for_falls_back_to_the_exit_codes_default_row():
    assert spec_for("nonsense", 3).code == "no_sources"
    assert spec_for(None, 99).code == "internal_error"


def test_info_for_code_uses_the_catalog_message():
    info = info_for_code("not_in_sources")
    assert info["code"] == "not_in_sources" and "nothing was invented" in info["message"]
    assert info_for_code("config_invalid", "bad value")["message"] == "bad value"


@pytest.mark.parametrize("code", sorted(CATALOG))
def test_messages_format_with_a_detail(code):
    CATALOG[code].message.format(detail="x")
