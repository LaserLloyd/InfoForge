"""Config loading: defaults, derivation, and pointed failures."""

from __future__ import annotations

import json

import pytest

from infoforge import config as config_mod
from infoforge.config import EXIT_CONFIG, ConfigError

MINIMAL = """
model_id: some-model
base_url: http://127.0.0.1:11234/v1
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
    assert cfg.hold_wait_s == 300


def test_hold_wait_s_zero_is_allowed_negative_is_not(tmp_path):
    cfg = config_mod.load(write(tmp_path, MINIMAL + "hold_wait_s: 0\n"))
    assert cfg.hold_wait_s == 0
    with pytest.raises(ConfigError, match="hold_wait_s"):
        config_mod.load(write(tmp_path, MINIMAL + "hold_wait_s: -1\n"))


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
        config_mod.load(write(tmp_path, "base_url: http://127.0.0.1:11234/v1\n"))


def test_missing_base_url_is_fatal(tmp_path):
    with pytest.raises(ConfigError, match="base_url"):
        config_mod.load(write(tmp_path, "model_id: m\n"))


def test_base_url_scheme_is_validated(tmp_path):
    with pytest.raises(ConfigError, match="http"):
        config_mod.load(write(tmp_path, "model_id: m\nbase_url: 127.0.0.1:11234\n"))


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
        "    base_url: http://127.0.0.1:11235/v1\n"
        "  - model_id: m3\n"
        "    base_url: http://127.0.0.1:11236/v1\n"
        '    api_key: ""\n'
    )
    cfg = config_mod.load(write(tmp_path, text))
    assert cfg.fallbacks[0].api_key == "primary-key"
    assert cfg.fallbacks[1].api_key == ""


def test_endpoints_puts_primary_first(tmp_path):
    text = MINIMAL + (
        "fallbacks:\n  - model_id: m2\n    base_url: http://127.0.0.1:11235/v1\n"
    )
    cfg = config_mod.load(write(tmp_path, text))
    assert [e.model_id for e in cfg.endpoints] == ["some-model", "m2"]


# ------------------------------------------------------------ 2.1 hardening

def test_enabled_false_as_a_string_disables_the_zim(tmp_path):
    # bool("false") is True: this used to silently ENABLE a ZIM switched off.
    text = MINIMAL + (
        "zims:\n"
        '  - file: "a.zim"\n'
        '    enabled: "false"\n'
        '  - file: "b.zim"\n'
        '    enabled: "yes"\n'
    )
    cfg = config_mod.load(write(tmp_path, text))
    assert [z.enabled for z in cfg.zims] == [False, True]
    assert [z.file for z in cfg.enabled_zims] == ["b.zim"]


def test_enabled_must_be_a_boolean(tmp_path):
    text = MINIMAL + 'zims:\n  - file: "a.zim"\n    enabled: maybe\n'
    with pytest.raises(ConfigError, match="enabled"):
        config_mod.load(write(tmp_path, text))


@pytest.mark.parametrize("name", [
    "a\\b.zim", "..\\x.zim", "C:\\x.zim", "dir/a.zim", ".hidden.zim", "a\x01b.zim"])
def test_zim_file_must_be_a_bare_filename(tmp_path, name):
    # Build the YAML with json.dumps: JSON string escapes are valid YAML.
    text = MINIMAL + "zims:\n  - file: " + json.dumps(name) + "\n"
    with pytest.raises(ConfigError, match="bare filename"):
        config_mod.load(write(tmp_path, text))


def test_xdg_config_home_is_honoured(tmp_path, monkeypatch):
    target = tmp_path / "xdg" / "infoforge"
    target.mkdir(parents=True)
    (target / "config.yaml").write_text(MINIMAL, encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config_mod.config_path() == target / "config.yaml"
    assert config_mod.load().model_id == "some-model"


def test_explicit_and_infoforge_config_beat_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("INFOFORGE_CONFIG", str(tmp_path / "env.yaml"))
    assert config_mod.config_path() == tmp_path / "env.yaml"
    assert config_mod.config_path(tmp_path / "flag.yaml") == tmp_path / "flag.yaml"


def test_relative_xdg_config_home_is_ignored(tmp_path, monkeypatch):
    # The XDG spec says a relative value must be disregarded.
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/dir")
    assert config_mod.config_path() == config_mod.DEFAULT_CONFIG_PATH.expanduser()


def test_license_label_defaults_by_zim_family(tmp_path):
    text = MINIMAL + (
        "zims:\n"
        '  - file: "wikipedia_en_all_nopic.zim"\n'
        '  - file: "wiktionary_en_all.zim"\n'
        '  - file: "stackoverflow.com_en_all.zim"\n'
        '  - file: "notes.zim"\n'
        '    license: "CC0"\n'
    )
    cfg = config_mod.load(write(tmp_path, text))
    assert [z.license_label for z in cfg.zims] == [
        "CC BY-SA 4.0", "CC BY-SA 4.0", "unknown", "CC0"]


def test_license_for_url_matches_the_book(tmp_path):
    cfg = config_mod.load(write(tmp_path, MINIMAL + 'zims:\n  - file: "wikipedia_en_x.zim"\n'))
    assert cfg.license_for_url("http://h:1/content/wikipedia_en_x/A/Paris") == "CC BY-SA 4.0"
    assert cfg.license_for_url("http://h:1/content/other/A/Paris") == "unknown"
    assert cfg.license_for_url("no content marker") == "unknown"


def test_unreadable_and_unparseable_configs_have_distinct_codes(tmp_path):
    with pytest.raises(ConfigError) as exc:
        config_mod.load(write(tmp_path, "model_id: [unclosed\n"))
    assert exc.value.code == "config_unreadable"
    with pytest.raises(ConfigError) as exc:
        config_mod.load(tmp_path)  # a directory: read_text raises OSError
    assert exc.value.code == "config_unreadable"


def test_non_mapping_yaml_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="mapping"):
        config_mod.load(write(tmp_path, "- just\n- a list\n"))


def test_zims_and_fallbacks_shape_errors(tmp_path):
    with pytest.raises(ConfigError, match="list"):
        config_mod.load(write(tmp_path, MINIMAL + "zims: nope\n"))
    with pytest.raises(ConfigError, match="mapping"):
        config_mod.load(write(tmp_path, MINIMAL + "zims:\n  - just-a-string\n"))
    with pytest.raises(ConfigError, match="list"):
        config_mod.load(write(tmp_path, MINIMAL + "fallbacks: nope\n"))
    with pytest.raises(ConfigError, match="mapping"):
        config_mod.load(write(tmp_path, MINIMAL + "fallbacks:\n  - nope\n"))


def test_private_helpers_still_work_for_old_callers(tmp_path):
    assert config_mod._as_int("7", "k", tmp_path) == 7
    assert config_mod._as_bool("no", "k", tmp_path) is False
