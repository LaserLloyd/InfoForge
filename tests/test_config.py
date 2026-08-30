"""Config loading: defaults, derivation, and pointed failures."""

from __future__ import annotations

import pytest

from infoforge import config as config_mod
from infoforge.config import EXIT_CONFIG, ConfigError

MINIMAL = """
model_id: some-model
base_url: http://127.0.0.1:1234/v1
"""


def write(tmp_path, text, name="config.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_minimal_config_gets_defaults(tmp_path):
    cfg = config_mod.load(write(tmp_path, MINIMAL))
    assert cfg.model_id == "some-model"
    assert cfg.max_articles == 5
    assert cfg.max_chars == 8000
    assert cfg.search_pool == 15
    assert cfg.studioforge is False
    assert cfg.fallbacks == ()


def test_kiwix_url_is_derived_from_bind_and_port(tmp_path):
    cfg = config_mod.load(write(tmp_path, MINIMAL + "kiwix_port: 9999\n"))
    assert cfg.kiwix_url == "http://127.0.0.1:9999"


def test_wildcard_bind_is_dialled_as_loopback(tmp_path):
    # 0.0.0.0 is a bind address, not a destination — the client must not
    # try to connect to it.
    cfg = config_mod.load(write(tmp_path, MINIMAL + 'kiwix_bind: "0.0.0.0"\n'))
    assert cfg.kiwix_url == "http://127.0.0.1:8080"


def test_explicit_kiwix_url_wins(tmp_path):
    cfg = config_mod.load(
        write(tmp_path, MINIMAL + 'kiwix_url: "http://127.0.0.1:7000/"\n')
    )
    assert cfg.kiwix_url == "http://127.0.0.1:7000"


def test_env_override_is_used(tmp_path, monkeypatch):
    path = write(tmp_path, MINIMAL)
    monkeypatch.setenv("INFOFORGE_CONFIG", str(path))
    assert config_mod.load().path == path


def test_missing_file_names_the_path_and_the_fix(tmp_path):
    with pytest.raises(ConfigError) as exc:
        config_mod.load(tmp_path / "nope.yaml")
    message = str(exc.value)
    assert "nope.yaml" in message
    assert "config.example.yaml" in message
    assert exc.value.exit_code == EXIT_CONFIG


def test_missing_model_id_refuses_to_guess(tmp_path):
    with pytest.raises(ConfigError, match="model_id"):
        config_mod.load(write(tmp_path, "base_url: http://127.0.0.1:1234/v1\n"))


def test_missing_base_url_is_fatal(tmp_path):
    with pytest.raises(ConfigError, match="base_url"):
        config_mod.load(write(tmp_path, "model_id: m\n"))


def test_base_url_scheme_is_validated(tmp_path):
    with pytest.raises(ConfigError, match="http"):
        config_mod.load(write(tmp_path, "model_id: m\nbase_url: 127.0.0.1:1234\n"))


def test_non_integer_max_articles(tmp_path):
    with pytest.raises(ConfigError, match="max_articles"):
        config_mod.load(write(tmp_path, MINIMAL + "max_articles: many\n"))


def test_zero_max_chars_rejected(tmp_path):
    with pytest.raises(ConfigError, match="max_chars"):
        config_mod.load(write(tmp_path, MINIMAL + "max_chars: 0\n"))


def test_bad_yaml_is_reported_as_such(tmp_path):
    with pytest.raises(ConfigError, match="YAML"):
        config_mod.load(write(tmp_path, "model_id: [unclosed\n"))


def test_empty_file_is_reported(tmp_path):
    with pytest.raises(ConfigError, match="empty"):
        config_mod.load(write(tmp_path, "\n"))


def test_zim_entry_needs_a_file(tmp_path):
    text = MINIMAL + "zims:\n  - name: nameless\n"
    with pytest.raises(ConfigError, match="file"):
        config_mod.load(write(tmp_path, text))


def test_zim_file_may_not_traverse(tmp_path):
    text = MINIMAL + 'zims:\n  - file: "../etc/passwd"\n'
    with pytest.raises(ConfigError, match="bare filename"):
        config_mod.load(write(tmp_path, text))


def test_books_name_strips_the_extension(tmp_path):
    text = MINIMAL + 'zims:\n  - file: "wikipedia_en_all_nopic.zim"\n'
    cfg = config_mod.load(write(tmp_path, text))
    assert cfg.zims[0].books_name == "wikipedia_en_all_nopic"


def test_disabled_zims_are_excluded_from_enabled(tmp_path):
    text = MINIMAL + (
        "zims:\n"
        '  - file: "a.zim"\n'
        "    enabled: true\n"
        '  - file: "b.zim"\n'
        "    enabled: false\n"
    )
    cfg = config_mod.load(write(tmp_path, text))
    assert [z.file for z in cfg.enabled_zims] == ["a.zim"]


def test_fallback_needs_model_and_url(tmp_path):
    text = MINIMAL + "fallbacks:\n  - model_id: only-a-model\n"
    with pytest.raises(ConfigError, match="fallbacks"):
        config_mod.load(write(tmp_path, text))


def test_fallback_inherits_api_key_unless_explicit(tmp_path):
    text = MINIMAL + (
        'api_key: "primary-key"\n'
        "fallbacks:\n"
        "  - model_id: m2\n"
        "    base_url: http://127.0.0.1:1235/v1\n"
        "  - model_id: m3\n"
        "    base_url: http://127.0.0.1:1236/v1\n"
        '    api_key: ""\n'
    )
    cfg = config_mod.load(write(tmp_path, text))
    assert cfg.fallbacks[0].api_key == "primary-key"
    assert cfg.fallbacks[1].api_key == ""


def test_endpoints_puts_primary_first(tmp_path):
    text = MINIMAL + (
        "fallbacks:\n  - model_id: m2\n    base_url: http://127.0.0.1:1235/v1\n"
    )
    cfg = config_mod.load(write(tmp_path, text))
    assert [e.model_id for e in cfg.endpoints] == ["some-model", "m2"]
