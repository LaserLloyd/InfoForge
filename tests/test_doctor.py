"""doctor: every row, with fakes for kiwix and the LLM endpoints."""

from __future__ import annotations

import dataclasses

import pytest
import requests
from conftest import FakeResponse

from infoforge import doctor, kiwix
from infoforge.config import Config, Endpoint

CONFIG = """
model_id: test-model
base_url: http://127.0.0.1:11234/v1
zim_dir: {zim_dir}
zims:
  - file: wikipedia_en_x.zim
"""


class Net:
    """requests.Session stand-in: routes by URL suffix."""

    def __init__(self, models=None, llm_error=None, status=200):
        self.models = models if models is not None else ["test-model"]
        self.llm_error = llm_error
        self.status = status
        self.calls = []

    def get(self, url, headers=None, timeout=None, params=None):
        self.calls.append((url, headers))
        if url.endswith("/models"):
            if self.llm_error:
                raise self.llm_error
            if self.status >= 400:
                return FakeResponse(self.status)
            return FakeResponse(200, json_data={"data": [{"id": m} for m in self.models]})
        return FakeResponse(200)


@pytest.fixture
def config_path(tmp_path):
    zim_dir = tmp_path / "zims"
    zim_dir.mkdir()
    (zim_dir / "wikipedia_en_x.zim").write_bytes(b"x")
    path = tmp_path / "c.yaml"
    path.write_text(CONFIG.format(zim_dir=zim_dir.as_posix()), encoding="utf-8")
    return path


@pytest.fixture
def healthy(monkeypatch):
    monkeypatch.setattr(kiwix, "podman_available", lambda: True)
    monkeypatch.setattr(kiwix, "container_state", lambda name=kiwix.CONTAINER_NAME: "running")
    monkeypatch.setattr(kiwix, "_serving_files",
                        lambda name=kiwix.CONTAINER_NAME: ["wikipedia_en_x.zim"])
    monkeypatch.setattr(kiwix, "is_up", lambda cfg, session=None, timeout=5: True)
    monkeypatch.setattr(kiwix, "search", lambda *a, **k: [object(), object()])


def rows(report):
    return {c.name: c for c in report.checks}


def test_everything_healthy(config_path, healthy):
    report = doctor.run(str(config_path), session=Net())
    assert report.ok
    r = rows(report)
    assert r["container"].detail == "infoforge-kiwix: running, serving wikipedia_en_x.zim"
    assert "2 hit(s)" in r["search"].detail
    assert "model listed" in r["llm primary"].detail
    assert "all checks passed" in report.render()
    assert report.to_dict()["ok"] is True and "error" not in report.to_dict()


def test_failing_report_carries_the_doctor_failed_error(config_path, healthy):
    report = doctor.run(str(config_path), session=Net(llm_error=requests.ConnectionError("x")))
    assert not report.ok
    out = report.to_dict()
    assert out["error"]["code"] == "doctor_failed"
    assert "[FAIL] llm primary" in report.render()
    assert "problems found" in doctor.render(out)


def test_llm_http_error_and_unlisted_model(config_path, healthy):
    failed = rows(doctor.run(str(config_path), session=Net(status=500)))["llm primary"]
    assert "HTTP 500" in failed.detail
    unlisted = rows(doctor.run(str(config_path), session=Net(models=["other"])))["llm primary"]
    assert "WARNING: model_id not in /models list" in unlisted.detail


def test_llm_probe_sends_the_bearer_token_only_when_configured():
    session = Net()
    doctor._check_llm(doctor.Report(), "x", Endpoint("m", "http://h/v1", "sk-1"), 5, session)
    assert session.calls[-1][1] == {"Authorization": "Bearer sk-1"}
    doctor._check_llm(doctor.Report(), "x", Endpoint("m", "http://h/v1"), 5, session)
    assert session.calls[-1][1] == {}


def test_llm_non_json_models_body_is_fine():
    class Odd:
        def get(self, *a, **k):
            return FakeResponse(200)  # .json() raises ValueError
    report = doctor.Report()
    doctor._check_llm(report, "x", Endpoint("m", "http://h/v1"), 5, Odd())
    assert report.ok and "model listed" not in report.checks[0].detail


def test_fallbacks_are_probed_too(config_path, healthy, tmp_path):
    path = tmp_path / "fb.yaml"
    path.write_text(config_path.read_text(encoding="utf-8") +
                    "fallbacks:\n  - model_id: f1\n    base_url: http://127.0.0.1:11235/v1\n",
                    encoding="utf-8")
    report = doctor.run(str(path), session=Net(models=["test-model", "f1"]))
    assert "llm fallback 1" in rows(report)


def test_missing_zim_dir_and_missing_zims(tmp_path, healthy):
    path = tmp_path / "c.yaml"
    path.write_text(CONFIG.format(zim_dir=(tmp_path / "nope").as_posix()), encoding="utf-8")
    r = rows(doctor.run(str(path), session=Net()))
    assert not r["zim_dir"].ok and "does not exist" in r["zim_dir"].detail
    assert not r["zims"].ok and "missing on disk" in r["zims"].detail


def test_no_enabled_zims_skips_the_search_probe(tmp_path, healthy):
    path = tmp_path / "c.yaml"
    path.write_text("model_id: m\nbase_url: http://127.0.0.1:11234/v1\n"
                    f"zim_dir: {tmp_path.as_posix()}\n", encoding="utf-8")
    r = rows(doctor.run(str(path), session=Net()))
    assert not r["zims"].ok and "no enabled entries" in r["zims"].detail
    assert "search" not in r


def test_kiwix_down_and_no_hits(config_path, healthy, monkeypatch):
    monkeypatch.setattr(kiwix, "is_up", lambda cfg, session=None, timeout=5: False)
    monkeypatch.setattr(kiwix, "search", lambda *a, **k: [])
    r = rows(doctor.run(str(config_path), session=Net()))
    assert not r["kiwix http"].ok and not r["search"].ok


def test_container_not_running_and_podman_errors(config_path, healthy, monkeypatch):
    monkeypatch.setattr(kiwix, "container_state", lambda name=kiwix.CONTAINER_NAME: "exited")
    r = rows(doctor.run(str(config_path), session=Net()))
    assert not r["container"].ok and "exited" in r["container"].detail

    def boom(name=kiwix.CONTAINER_NAME):
        raise kiwix.KiwixError("podman exploded\nsecond line")
    monkeypatch.setattr(kiwix, "container_state", boom)
    r = rows(doctor.run(str(config_path), session=Net()))
    assert not r["container"].ok and r["container"].detail == "podman exploded"


def test_bring_your_own_kiwix_passes_without_podman(config_path, healthy, monkeypatch):
    monkeypatch.setattr(kiwix, "podman_available", lambda: False)
    monkeypatch.setattr(kiwix, "container_state",
                        lambda name=kiwix.CONTAINER_NAME: pytest.fail("no podman call"))
    report = doctor.run(str(config_path), session=Net())
    assert report.ok
    assert "podman not installed: skipped" in rows(report)["container"].detail


def test_non_loopback_bind_gets_an_exposure_warning(tmp_path, healthy):
    path = tmp_path / "c.yaml"
    path.write_text(CONFIG.format(zim_dir=tmp_path.as_posix()) + 'kiwix_bind: "0.0.0.0"\n',
                    encoding="utf-8")
    r = rows(doctor.run(str(path), session=Net()))
    assert r["kiwix exposure"].ok  # a warning, not a failure
    assert "no authentication" in r["kiwix exposure"].detail


def test_loopback_bind_has_no_exposure_row(config_path, healthy):
    assert "kiwix exposure" not in rows(doctor.run(str(config_path), session=Net()))


def test_env_only_config_is_reported(monkeypatch, healthy, tmp_path):
    monkeypatch.setenv("INFOFORGE_MODEL_ID", "m")
    monkeypatch.setenv("INFOFORGE_BASE_URL", "http://127.0.0.1:11234/v1")
    monkeypatch.setenv("INFOFORGE_ZIM_DIR", str(tmp_path))
    report = doctor.run(str(tmp_path / "absent.yaml"), session=Net())
    assert "environment" in rows(report)["config"].detail


def test_missing_config_is_a_report_not_a_traceback(tmp_path):
    report = doctor.run(str(tmp_path / "absent.yaml"))
    assert [c.name for c in report.checks] == ["config"]
    assert "config.example.yaml" in report.checks[0].detail


# ---- config file permissions (an api_key in a readable file is a finding)

def _cfg_with_key(path, key="sk-1"):
    return Config(model_id="m", base_url="http://h/v1", api_key=key, path=path)


def _keyed_file(tmp_path, text="model_id: m\nbase_url: http://h/v1\napi_key: sk-1\n"):
    path = tmp_path / "c.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_loose_permissions_on_a_config_with_a_key_fail(tmp_path, monkeypatch):
    path = _keyed_file(tmp_path)
    monkeypatch.setattr(doctor, "_IS_POSIX", True)
    monkeypatch.setattr(doctor, "_file_mode", lambda p: 0o644)
    monkeypatch.setattr(doctor, "_mount_ignores_modes", lambda p: False)
    report = doctor.Report()
    doctor._check_config_permissions(report, _cfg_with_key(path))
    assert not report.ok and "chmod 600" in report.checks[0].detail


def test_tight_permissions_pass(tmp_path, monkeypatch):
    path = _keyed_file(tmp_path)
    monkeypatch.setattr(doctor, "_IS_POSIX", True)
    monkeypatch.setattr(doctor, "_file_mode", lambda p: 0o600)
    report = doctor.Report()
    doctor._check_config_permissions(report, _cfg_with_key(path))
    assert report.ok and "mode 600" in report.checks[0].detail


def test_permission_check_is_skipped_without_a_key_or_off_posix(tmp_path, monkeypatch):
    path = tmp_path / "c.yaml"
    monkeypatch.setattr(doctor, "_IS_POSIX", True)
    monkeypatch.setattr(doctor, "_file_mode", lambda p: 0o644)
    report = doctor.Report()
    doctor._check_config_permissions(report, _cfg_with_key(path, key=""))
    doctor._check_config_permissions(report, _cfg_with_key(None))
    monkeypatch.setattr(doctor, "_IS_POSIX", False)
    doctor._check_config_permissions(report, _cfg_with_key(path))
    assert report.checks == []


def test_permission_check_tolerates_a_vanished_file(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "_IS_POSIX", True)

    def gone(p):
        raise FileNotFoundError
    monkeypatch.setattr(doctor, "_file_mode", gone)
    report = doctor.Report()
    doctor._check_config_permissions(report, _cfg_with_key(_keyed_file(tmp_path)))
    doctor._check_config_permissions(report, _cfg_with_key(tmp_path / "missing.yaml"))
    assert report.checks == []


def test_file_mode_reads_the_real_mode(tmp_path):
    path = tmp_path / "f"
    path.write_text("x", encoding="utf-8")
    assert 0 <= doctor._file_mode(path) <= 0o777


# ---- the key is in the environment, or the mount cannot carry modes (Windows bind mount)

def _posix_777(monkeypatch):
    monkeypatch.setattr(doctor, "_IS_POSIX", True)
    monkeypatch.setattr(doctor, "_file_mode", lambda p: 0o777)


def test_env_supplied_key_with_a_keyless_file_passes_whatever_the_mode(tmp_path, monkeypatch):
    # The Windows-bind-mount case: mode 777, but the file holds no secret.
    path = _keyed_file(tmp_path, 'model_id: m\nbase_url: http://h/v1\napi_key: ""\n')
    _posix_777(monkeypatch)
    monkeypatch.setattr(doctor, "_mount_ignores_modes",
                        lambda p: pytest.fail("a keyless file needs no mount probe"))
    report = doctor.Report()
    doctor._check_config_permissions(report, _cfg_with_key(path, key="from-env"))
    row = report.checks[0]
    assert report.ok and row.name == "config perms"
    assert "no secret" in row.detail and "environment" in row.detail
    assert "[PASS] config perms" in report.render()


def test_file_without_the_api_key_field_at_all_also_passes(tmp_path, monkeypatch):
    path = _keyed_file(tmp_path, "model_id: m\nbase_url: http://h/v1\n")
    _posix_777(monkeypatch)
    report = doctor.Report()
    doctor._check_config_permissions(report, _cfg_with_key(path, key="from-env"))
    assert report.ok and "no secret" in report.checks[0].detail


def test_secret_on_a_mount_without_posix_modes_is_a_warning_not_a_failure(tmp_path, monkeypatch):
    path = _keyed_file(tmp_path)
    _posix_777(monkeypatch)
    monkeypatch.setattr(doctor, "_mount_fstype", lambda p: "fuse.grpcfuse")
    report = doctor.Report()
    doctor._check_config_permissions(report, _cfg_with_key(path))
    row = report.checks[0]
    assert report.ok
    assert row.detail.startswith("WARNING") and "move the key to the environment" in row.detail
    assert "[WARN] config perms" in report.render()


def test_secret_on_a_normal_filesystem_still_fails(tmp_path, monkeypatch):
    path = _keyed_file(tmp_path)
    _posix_777(monkeypatch)
    monkeypatch.setattr(doctor, "_mount_fstype", lambda p: "ext4")
    monkeypatch.setattr(doctor, "_chmod_is_ignored", lambda p: False)
    report = doctor.Report()
    doctor._check_config_permissions(report, _cfg_with_key(path))
    assert not report.ok and "chmod 600" in report.checks[0].detail
    assert "[FAIL] config perms" in report.render()


def test_a_fallback_key_in_the_file_counts_as_a_secret(tmp_path, monkeypatch):
    path = _keyed_file(tmp_path, "model_id: m\nbase_url: http://h/v1\nfallbacks:\n"
                                 "  - {model_id: f, base_url: http://f/v1, api_key: sk-2}\n")
    _posix_777(monkeypatch)
    monkeypatch.setattr(doctor, "_mount_fstype", lambda p: "ext4")
    monkeypatch.setattr(doctor, "_chmod_is_ignored", lambda p: False)
    report = doctor.Report()
    cfg = dataclasses.replace(_cfg_with_key(path, key=""),
                              fallbacks=(Endpoint("f", "http://f/v1", "sk-2"),))
    doctor._check_config_permissions(report, cfg)
    assert not report.ok


def test_unparsable_file_gives_no_perms_row(tmp_path, monkeypatch):
    _posix_777(monkeypatch)
    report = doctor.Report()
    doctor._check_config_permissions(report, _cfg_with_key(_keyed_file(tmp_path, "a: [")))
    assert report.checks == []


def test_file_holds_secret_shapes(tmp_path):
    assert doctor._file_holds_secret(_keyed_file(tmp_path, "- just\n- a list\n")) is False
    assert doctor._file_holds_secret(_keyed_file(tmp_path, "fallbacks: nope\n")) is False
    assert doctor._file_holds_secret(_keyed_file(tmp_path, "api_key: '  '\n")) is False
    assert doctor._file_holds_secret(_keyed_file(tmp_path, "api_key: sk\n")) is True


def test_mount_fstype_picks_the_longest_containing_mount(tmp_path):
    mounts = tmp_path / "mounts"
    lines = ["overlay / overlay rw 0 0",
             "grpcfuse /config fuse.grpcfuse rw 0 0",
             "grpcfuse /config/config.yaml fuse.grpcfuse ro 0 0",
             "tmpfs /config2 tmpfs rw 0 0",
             "9p /my" + chr(92) + "040data 9p rw 0 0",   # /proc/mounts escapes a space as \040
             "garbage"]
    mounts.write_text("\n".join(lines) + "\n", encoding="utf-8")
    f = str(mounts)
    assert doctor._mount_fstype("/config/config.yaml", f) == "fuse.grpcfuse"
    assert doctor._mount_fstype("/config/other.yaml", f) == "fuse.grpcfuse"
    assert doctor._mount_fstype("/config2/x", f) == "tmpfs"   # /config is not a prefix of it
    assert doctor._mount_fstype("/etc/x", f) == "overlay"
    assert doctor._mount_fstype("/my data/x", f) == "9p"
    assert doctor._mount_fstype("/x", str(tmp_path / "missing")) is None


def test_chmod_probe_is_inconclusive_when_the_directory_is_not_writable(tmp_path):
    assert doctor._chmod_is_ignored(tmp_path / "no-such-dir" / "c.yaml") is False


def test_chmod_probe_detects_a_chmod_that_does_not_stick(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.os, "chmod", lambda *a, **k: None)
    monkeypatch.setattr(doctor.os, "stat",
                        lambda p, *a, **k: type("S", (), {"st_mode": 0o100777})())
    assert doctor._chmod_is_ignored(tmp_path / "c.yaml") is True


def test_mount_ignores_modes_uses_either_signal(monkeypatch):
    monkeypatch.setattr(doctor, "_mount_fstype", lambda p: "virtiofs")
    assert doctor._mount_ignores_modes("/x") is True
    monkeypatch.setattr(doctor, "_mount_fstype", lambda p: "ext4")
    monkeypatch.setattr(doctor, "_chmod_is_ignored", lambda p: True)
    assert doctor._mount_ignores_modes("/x") is True
    monkeypatch.setattr(doctor, "_chmod_is_ignored", lambda p: False)
    assert doctor._mount_ignores_modes("/x") is False
