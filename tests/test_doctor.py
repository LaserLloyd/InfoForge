"""doctor: every row, with fakes for kiwix and the LLM endpoints."""

from __future__ import annotations

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


def test_loose_permissions_on_a_config_with_a_key_fail(tmp_path, monkeypatch):
    path = tmp_path / "c.yaml"
    path.write_text("x", encoding="utf-8")
    monkeypatch.setattr(doctor, "_IS_POSIX", True)
    monkeypatch.setattr(doctor, "_file_mode", lambda p: 0o644)
    report = doctor.Report()
    doctor._check_config_permissions(report, _cfg_with_key(path))
    assert not report.ok and "chmod 600" in report.checks[0].detail


def test_tight_permissions_pass(tmp_path, monkeypatch):
    path = tmp_path / "c.yaml"
    path.write_text("x", encoding="utf-8")
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
    doctor._check_config_permissions(report, _cfg_with_key(tmp_path / "c.yaml"))
    assert report.checks == []


def test_file_mode_reads_the_real_mode(tmp_path):
    path = tmp_path / "f"
    path.write_text("x", encoding="utf-8")
    assert 0 <= doctor._file_mode(path) <= 0o777
