"""The settings schema: one declaration, validated, env-overridable, exported."""

from __future__ import annotations

import dataclasses
import json

import pytest

from infoforge import config as config_mod
from infoforge import settings_schema as schema
from infoforge.config import Config, ConfigError

MINIMAL = "model_id: some-model\nbase_url: http://127.0.0.1:11234/v1\n"

#: A valid env string and the Config attribute value it must produce, per key.
ENV_SAMPLES = {
    "model_id": ("env-model", "env-model"),
    "base_url": ("http://127.0.0.1:19999/v1", "http://127.0.0.1:19999/v1"),
    "api_key": ("sk-env", "sk-env"),
    "studioforge": ("true", True),
    "fallbacks": ('[{"model_id": "m2", "base_url": "http://127.0.0.1:11235/v1"}]', None),
    "kiwix_bind": ("10.0.0.5", "10.0.0.5"),
    "kiwix_port": ("19090", 19090),
    "kiwix_url": ("http://127.0.0.1:17000/", "http://127.0.0.1:17000"),
    "kiwix_image_tag": ("3.7.0", "3.7.0"),
    "zim_dir": ("/data/zims", None),
    "zims": ('[{"file": "a.zim", "enabled": "false"}]', None),
    "max_articles": ("7", 7),
    "max_chars": ("1234", 1234),
    "search_pool": ("9", 9),
    "timeout_s": ("42", 42),
    "hold_wait_s": ("0", 0),
    "log_questions": ("yes", True),
}


def load(tmp_path, text=MINIMAL, **kwargs):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return config_mod.load(path, **kwargs)


def test_schema_covers_all_17_keys_and_every_config_field():
    keys = [s.key for s in schema.SETTINGS]
    assert len(keys) == len(set(keys)) == 17
    fields = {f.name for f in dataclasses.fields(Config)} - {"path", "sources"}
    assert fields == set(keys)


def test_every_setting_has_the_documented_attributes():
    for s in schema.SETTINGS:
        assert s.description and s.level in ("basic", "advanced")
        assert s.env == f"INFOFORGE_{s.key.upper()}"
        assert s.type in ("string", "integer", "boolean", "path", "url", "list")


def test_samples_cover_every_key():
    assert set(ENV_SAMPLES) == set(schema.BY_KEY)


@pytest.mark.parametrize("key", sorted(ENV_SAMPLES))
def test_env_override_works_for_every_key(tmp_path, key):
    raw, expected = ENV_SAMPLES[key]
    cfg = load(tmp_path, environ={schema.BY_KEY[key].env: raw})
    assert cfg.sources[key] == "env"
    value = getattr(cfg, key)
    if key == "fallbacks":
        assert [e.model_id for e in value] == ["m2"]
    elif key == "zims":
        assert value[0].file == "a.zim" and value[0].enabled is False
    elif key == "zim_dir":
        assert str(value).replace("\\", "/").endswith("/data/zims")
    else:
        assert value == expected


def test_precedence_cli_over_env_over_file_over_default(tmp_path):
    text = MINIMAL + "max_articles: 2\n"
    assert load(tmp_path, text).max_articles == 2
    assert load(tmp_path, text).sources["max_articles"] == "file"
    env = {"INFOFORGE_MAX_ARTICLES": "3"}
    assert load(tmp_path, text, environ=env).max_articles == 3
    cfg = load(tmp_path, text, environ=env, overrides={"max_articles": 4})
    assert cfg.max_articles == 4 and cfg.sources["max_articles"] == "cli"
    assert load(tmp_path).sources["max_articles"] == "default"


def test_empty_env_value_counts_as_unset(tmp_path):
    cfg = load(tmp_path, MINIMAL + "max_chars: 111\n", environ={"INFOFORGE_MAX_CHARS": ""})
    assert cfg.max_chars == 111


def test_bad_env_value_names_the_variable(tmp_path):
    with pytest.raises(ConfigError, match="INFOFORGE_KIWIX_PORT"):
        load(tmp_path, environ={"INFOFORGE_KIWIX_PORT": "99999"})
    with pytest.raises(ConfigError, match="INFOFORGE_LOG_QUESTIONS"):
        load(tmp_path, environ={"INFOFORGE_LOG_QUESTIONS": "maybe"})
    with pytest.raises(ConfigError, match="INFOFORGE_ZIMS"):
        load(tmp_path, environ={"INFOFORGE_ZIMS": "[not json"})


def test_range_messages(tmp_path):
    with pytest.raises(ConfigError, match="1-65535"):
        load(tmp_path, MINIMAL + "kiwix_port: 0\n")
    with pytest.raises(ConfigError, match=">= 1"):
        load(tmp_path, MINIMAL + "timeout_s: 0\n")
    with pytest.raises(ConfigError, match="integer"):
        load(tmp_path, MINIMAL + "search_pool: true\n")


def test_kiwix_url_must_be_http(tmp_path):
    with pytest.raises(ConfigError, match="http"):
        load(tmp_path, MINIMAL + "kiwix_url: localhost:8090\n")


def test_fresh_config_defaults_to_8090_existing_files_keep_8080(tmp_path):
    # An existing file that omits the key keeps the 2.0 value ...
    assert load(tmp_path).kiwix_port == 8080
    # ... a file that sets it keeps what it says ...
    assert load(tmp_path, MINIMAL + "kiwix_port: 8090\n").kiwix_port == 8090
    # ... and a fresh environment-only setup gets the new default.
    cfg = config_mod.load(tmp_path / "absent.yaml", environ={
        "INFOFORGE_MODEL_ID": "m", "INFOFORGE_BASE_URL": "http://127.0.0.1:11234/v1"})
    assert cfg.kiwix_port == 8090 and cfg.kiwix_url == "http://127.0.0.1:8090"
    assert cfg.path is None


def test_env_only_needs_both_required_keys(tmp_path):
    with pytest.raises(ConfigError) as exc:
        config_mod.load(tmp_path / "absent.yaml", environ={"INFOFORGE_MODEL_ID": "m"})
    assert exc.value.code == "config_missing"
    assert "INFOFORGE_MODEL_ID" in str(exc.value)


def test_unknown_keys_warn_but_do_not_fail(tmp_path, capsys):
    cfg = load(tmp_path, MINIMAL + "max_articels: 3\n")
    assert cfg.max_articles == 5
    assert "unknown key 'max_articels'" in capsys.readouterr().err


def test_config_show_redacts_secrets_and_reports_sources(tmp_path):
    cfg = load(tmp_path, MINIMAL + 'api_key: "sk-secret"\n'
               "fallbacks:\n  - model_id: f\n    base_url: http://127.0.0.1:11235/v1\n"
               "zims:\n  - file: wikipedia_en_all_nopic.zim\n")
    shown = config_mod.show(cfg)
    dumped = json.dumps(shown)
    assert "sk-secret" not in dumped
    rows = {r["key"]: r for r in shown["settings"]}
    assert rows["api_key"]["value"] == "***" and rows["api_key"]["source"] == "file"
    assert rows["fallbacks"]["value"][0]["api_key"] == "***"
    assert rows["zims"]["value"][0]["license"] == "CC BY-SA 4.0"
    assert rows["kiwix_url"]["source"] == "derived"
    assert rows["max_articles"]["env"] == "INFOFORGE_MAX_ARTICLES"
    assert len(rows) == 17


def test_json_schema_describes_every_key(tmp_path):
    doc = schema.json_schema()
    assert set(doc["properties"]) == set(schema.BY_KEY)
    assert doc["required"] == ["model_id", "base_url"]
    assert doc["properties"]["kiwix_port"]["default"] == 8090
    assert doc["properties"]["kiwix_port"]["x-legacy-default"] == 8080
    assert doc["properties"]["api_key"]["writeOnly"] is True
    assert doc["properties"]["fallbacks"]["type"] == "array"
    assert json.loads(json.dumps(doc)) == doc


def test_schema_rows_flag_derived_and_ranges():
    rows = {r["key"]: r for r in schema.schema_rows()}
    assert rows["kiwix_url"]["derived"] is True
    assert rows["kiwix_port"]["minimum"] == 1 and rows["kiwix_port"]["maximum"] == 65535
    assert rows["hold_wait_s"]["minimum"] == 0


def test_example_config_loads_and_sets_only_schema_keys():
    from pathlib import Path

    import yaml
    text = (Path(__file__).resolve().parent.parent / "config.example.yaml").read_text("utf-8")
    parsed = yaml.safe_load(text)
    assert set(parsed) <= set(schema.BY_KEY)
    assert parsed["kiwix_port"] == 8090
    assert parsed["kiwix_image_tag"] == schema.DEFAULT_KIWIX_IMAGE_TAG


def test_the_legacy_port_is_labelled_in_config_show(tmp_path):
    # review fix (opus): `config show` printed `kiwix_port = 8080 [default]`
    # while the schema (and `config schema`) say the default is 8090.
    cfg = load(tmp_path)
    assert cfg.kiwix_port == 8080 and cfg.sources["kiwix_port"] == "legacy_default"
    row = next(r for r in config_mod.show(cfg)["settings"] if r["key"] == "kiwix_port")
    assert (row["value"], row["source"]) == (8080, "legacy_default")
    fresh = config_mod.load(tmp_path / "absent.yaml", environ={
        "INFOFORGE_MODEL_ID": "m", "INFOFORGE_BASE_URL": "http://127.0.0.1:11234/v1"})
    assert fresh.sources["kiwix_port"] == "default"
