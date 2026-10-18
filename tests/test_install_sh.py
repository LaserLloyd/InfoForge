"""deploy/install.sh, exercised in a sandbox with fake systemctl / podman / uv.

HOME and XDG_* point into tmp_path and every external command is a recording
stub placed first on PATH, so nothing real (no systemd, no podman, no uv) is
ever touched.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "install.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash")

STUB = """#!/bin/sh
echo "{name} $*" >> "$STUB_LOG"
{extra}
exit 0
"""


@pytest.fixture
def sandbox(tmp_path):
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    log.write_text("", encoding="utf-8")

    def stub(name, extra=""):
        path = bindir / name
        path.write_text(STUB.format(name=name, extra=extra), encoding="utf-8", newline="\n")
        path.chmod(0o755)

    stub("systemctl")
    stub("python3")
    stub("podman", extra='case "$1 $2" in "container exists") exit 0;; '
                         '"image exists") exit 1;; esac')
    # `uv tool list` shows an installed infoforge; `uv tool install` drops the CLI stub.
    cli = home / ".local" / "bin" / "infoforge"
    stub("uv", extra='case "$1 $2" in "tool list") echo "infoforge v2.1.0";; '
                     f'"tool install") printf "#!/bin/sh\\necho infoforge \\$* >> $STUB_LOG\\n" '
                     f'> "{cli.as_posix()}"; chmod +x "{cli.as_posix()}";; esac')
    env = {
        **os.environ,
        "HOME": home.as_posix(),
        "XDG_CONFIG_HOME": (tmp_path / "xdg").as_posix(),
        "XDG_STATE_HOME": (tmp_path / "state").as_posix(),
        "PATH": f"{bindir.as_posix()}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "STUB_LOG": log.as_posix(),
    }
    return {"home": home, "env": env, "log": log, "tmp": tmp_path, "cli": cli}


def run(sandbox, *args):
    return subprocess.run([BASH, str(SCRIPT), *args], env=sandbox["env"], text=True,
                          capture_output=True, timeout=60)


def calls(sandbox):
    return sandbox["log"].read_text(encoding="utf-8").splitlines()


def test_script_is_valid_bash():
    done = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_help_documents_the_modes(sandbox):
    done = run(sandbox, "--help")
    assert done.returncode == 0
    for flag in ("--uninstall", "--pull", "--migrate-localwiki"):
        assert flag in done.stdout


def test_unknown_option_is_refused(sandbox):
    done = run(sandbox, "--frobnicate")
    assert done.returncode == 1 and "unknown option" in done.stderr


def test_uninstall_removes_service_container_and_cli_but_keeps_data(sandbox):
    xdg = sandbox["tmp"] / "xdg"
    unit_dir = xdg / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    (unit_dir / "infoforge.service").write_text("[Unit]\n", encoding="utf-8")
    config = xdg / "infoforge" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("model_id: keep-me\n", encoding="utf-8")
    zim_dir = sandbox["home"] / "zims"
    zim_dir.mkdir()
    (zim_dir / "wiki.zim").write_bytes(b"x")
    log_dir = sandbox["tmp"] / "state" / "infoforge"
    log_dir.mkdir(parents=True)
    (log_dir / "log.jsonl").write_text("{}\n", encoding="utf-8")

    done = run(sandbox, "--uninstall")
    assert done.returncode == 0, done.stderr + done.stdout

    assert not (unit_dir / "infoforge.service").exists()
    log = calls(sandbox)
    assert "systemctl --user disable --now infoforge.service" in log
    assert "systemctl --user daemon-reload" in log
    assert "podman rm -f infoforge-kiwix" in log
    assert "uv tool uninstall infoforge" in log
    # the predecessor's container is none of an uninstall's business
    assert not any("localwiki" in line for line in log)
    # data is kept
    assert config.read_text(encoding="utf-8") == "model_id: keep-me\n"
    assert (zim_dir / "wiki.zim").exists() and (log_dir / "log.jsonl").exists()
    assert "your data was not touched" in done.stdout


def test_uninstall_when_nothing_is_installed_is_success(sandbox):
    sandbox["env"]["PATH"] = sandbox["env"]["PATH"]  # same stubs; podman says no container
    # make `podman container exists` fail and `uv tool list` empty
    bindir = Path(sandbox["env"]["PATH"].split(os.pathsep)[0])
    (bindir / "podman").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8", newline="\n")
    (bindir / "uv").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
    done = run(sandbox, "--uninstall")
    assert done.returncode == 0
    assert "nothing to remove" in done.stdout


def test_install_seeds_a_private_config_with_the_new_port_and_never_overwrites(sandbox):
    done = run(sandbox)
    assert done.returncode == 0, done.stderr + done.stdout
    config = sandbox["tmp"] / "xdg" / "infoforge" / "config.yaml"   # XDG honoured
    text = config.read_text(encoding="utf-8")
    assert "kiwix_port: 8090" in text and "kiwix_image_tag" in text
    if os.name == "posix":
        assert config.stat().st_mode & 0o777 == 0o600
    log = calls(sandbox)
    assert "uv tool install --reinstall " + str(ROOT).replace("\\", "/") in " ".join(log) \
        or any(line.startswith("uv tool install --reinstall") for line in log)
    assert "infoforge serve start" in log and "infoforge doctor" in log
    assert (sandbox["tmp"] / "xdg" / "systemd" / "user" / "infoforge.service").is_file()
    assert not any(line.startswith("podman pull") for line in log), "pulling is opt-in"

    config.write_text("model_id: mine\n", encoding="utf-8")
    assert run(sandbox).returncode == 0
    assert config.read_text(encoding="utf-8") == "model_id: mine\n"


def test_install_pulls_the_pinned_tag_only_when_asked(sandbox):
    done = run(sandbox, "--pull")
    assert done.returncode == 0, done.stderr + done.stdout
    assert "podman pull ghcr.io/kiwix/kiwix-serve:3.8.2" in calls(sandbox)


def test_install_without_pull_tells_you_the_command(sandbox):
    done = run(sandbox)
    assert "podman pull ghcr.io/kiwix/kiwix-serve:3.8.2" in done.stdout


def test_legacy_migration_is_off_by_default(sandbox):
    legacy = sandbox["home"] / "localwiki"
    legacy.mkdir()
    (legacy / "config.yaml").write_text("model_id: old\nbase_url: http://old/v1\n",
                                        encoding="utf-8")
    done = run(sandbox)
    assert done.returncode == 0
    assert "model_id: old" not in (sandbox["tmp"] / "xdg" / "infoforge" /
                                   "config.yaml").read_text(encoding="utf-8")
    assert not any("localwiki" in line for line in calls(sandbox))


def test_legacy_container_is_only_retired_with_the_flag(sandbox):
    run(sandbox, "--migrate-localwiki")
    # the stub podman prints nothing for `ps`, so nothing is stopped, but the
    # legacy ps query must have been made - and only because of the flag
    assert any(line.startswith("podman ps -a --filter name=^localwiki-kiwix")
               for line in calls(sandbox))
