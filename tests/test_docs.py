"""Generated reference docs cannot drift from the code, and the README names everything."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

from infoforge import cli, mcp_server
from infoforge import settings_schema as schema
from infoforge.errors import CATALOG

ROOT = Path(__file__).resolve().parent.parent


def load_gen_docs():
    spec = importlib.util.spec_from_file_location("gen_docs", ROOT / "scripts" / "gen_docs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen_docs = load_gen_docs()


@pytest.mark.parametrize("name", sorted(gen_docs.PAGES))
def test_reference_page_matches_the_code(name):
    path = ROOT / "docs" / "reference" / name
    assert path.is_file(), f"{path} missing: run python scripts/gen_docs.py"
    assert path.read_text(encoding="utf-8") == gen_docs.PAGES[name](), (
        f"docs/reference/{name} is stale: run python scripts/gen_docs.py")


def test_check_mode_passes_on_a_clean_tree_and_fails_on_drift(tmp_path, monkeypatch):
    assert gen_docs.main(["--check"]) == 0
    monkeypatch.setattr(gen_docs, "DOCS", tmp_path)       # an empty docs dir = all stale
    assert gen_docs.main(["--check"]) == 1
    assert gen_docs.main([]) == 0                          # writes them ...
    assert gen_docs.main(["--check"]) == 0                 # ... and then it is clean
    assert {p.name for p in tmp_path.glob("*.md")} == set(gen_docs.PAGES)


def test_script_runs_from_the_command_line():
    done = subprocess.run([sys.executable, str(ROOT / "scripts" / "gen_docs.py"), "--check"],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_config_reference_lists_every_key_and_env_var():
    text = (ROOT / "docs" / "reference" / "config.md").read_text(encoding="utf-8")
    for s in schema.SETTINGS:
        assert f"`{s.key}`" in text and f"`{s.env}`" in text


def test_errors_reference_has_an_anchor_per_code():
    text = (ROOT / "docs" / "reference" / "errors.md").read_text(encoding="utf-8")
    for code, spec in CATALOG.items():
        assert f"### {code}\n" in text
        assert spec.docs.endswith(f"#{code}")


def test_cli_reference_documents_every_command_and_flag():
    text = (ROOT / "docs" / "reference" / "cli.md").read_text(encoding="utf-8")
    parser = cli.build_parser()
    for name, sub in gen_docs._subparsers(parser).items():
        assert f"## `infoforge {name}`" in text
        for action in sub._actions:
            for opt in action.option_strings:
                if opt not in ("-h", "--help"):
                    assert f"`{opt}`" in text, (name, opt)


def test_mcp_reference_lists_the_tools():
    text = (ROOT / "docs" / "reference" / "mcp.md").read_text(encoding="utf-8")
    for tool in mcp_server.TOOL_NAMES:
        assert f"| `{tool}` |" in text


# ----------------------------------------------------------- the hand-written README

README = (ROOT / "README.md").read_text(encoding="utf-8")


def test_readme_documents_every_command():
    for name in gen_docs._subparsers(cli.build_parser()):
        assert re.search(rf"infoforge {name}\b", README), f"README never mentions `{name}`"


@pytest.mark.parametrize("needle", [
    "--no-ensure", "--text", "--config", "--version", "--max-articles", "--strict",
    "infoforge config show", "INFOFORGE_", "XDG_CONFIG_HOME", "infoforge-mcp",
    "CC BY-SA", "kiwix_url", "8090", "8080", "docs/reference/", "CHANGELOG", "SECURITY",
    "THIRD_PARTY_NOTICES",
])
def test_readme_mentions(needle):
    assert needle in README


def test_readme_no_longer_claims_nothing_leaves_the_machine():
    assert "Nothing leaves the machine" not in README
    assert "library.kiwix.org" in README          # the one catalog call is disclosed
    assert "ghcr.io" in README                    # and so is the image pull


def test_readme_says_what_platforms_work():
    assert "Linux" in README and "podman" in README and "Any OS" in README


def test_the_other_project_files_exist():
    for name in ("CHANGELOG.md", "SECURITY.md", "THIRD_PARTY_NOTICES.md", "LICENSE"):
        assert (ROOT / name).read_text(encoding="utf-8").strip(), name
    assert "2.1.0" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_version_is_consistent_everywhere():
    from infoforge import __version__
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{__version__}"' in pyproject
    assert __version__ == "2.2.0"


def test_compose_default_tag_matches_the_package_version():
    """Issue #7: the compose image tag default must not drift from the release."""
    import re

    from infoforge import __version__
    compose = (ROOT / "deploy" / "compose" / "compose.yml").read_text(encoding="utf-8")
    tags = set(re.findall(r"\$\{INFOFORGE_TAG:-([^}]+)\}", compose))
    assert tags, "deploy/compose/compose.yml has no ${INFOFORGE_TAG:-<version>} default"
    py = re.search(r'^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text(
        encoding="utf-8"), re.M)
    assert py and py.group(1) == __version__, (
        f"pyproject.toml version {py and py.group(1)!r} != infoforge.__version__ "
        f"{__version__!r}: fix pyproject.toml or src/infoforge/__init__.py")
    assert tags == {__version__}, (
        f"deploy/compose/compose.yml defaults INFOFORGE_TAG to {sorted(tags)} but the "
        f"package version is {__version__!r}: update every ${{INFOFORGE_TAG:-...}} "
        "default in deploy/compose/compose.yml")


def test_security_policy_uses_private_vulnerability_reporting():
    text = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert "example.invalid" not in text and "placeholder" not in text
    assert "Report a vulnerability" in text
    line = next(ln for ln in text.splitlines() if "github.com/LaserLloyd" in ln)  # scrub-ok: public repo URL
    assert "scrub-ok" in line
