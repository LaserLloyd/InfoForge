"""Turn a folder of Markdown/HTML docs into a .zim the kiwix container can serve.

Port of the ~/localwiki `zimify` bash script.  Behaviour kept: convert Markdown
with pandoc when it is available, copy everything else through, write a minimal
index page, then build with zimwriterfs — and when zimwriterfs is missing,
prepare the directory and print the exact command to finish the job later
rather than failing with nothing to show for the work.

All subprocess calls use fixed argv lists; no shell interpolation of paths.
"""

from __future__ import annotations

import html
import shutil
import subprocess
from pathlib import Path

from .config import InfoForgeError

MARKDOWN_SUFFIXES = (".md", ".markdown")


class ZimifyError(InfoForgeError):
    """Input directory missing, or the zim build failed."""

    code = "zimify_failed"


def _have(tool: str) -> bool:
    return shutil.which(tool) is not None


def _output_name(src: Path, base: str) -> str:
    """The filename this input gets in the prepared directory."""
    if src.suffix.lower() in MARKDOWN_SUFFIXES and _have("pandoc"):
        return src.stem + ".html"
    return base


def _convert(src: Path, dest_dir: Path) -> Path:
    """Copy or convert one input file into the prepared directory."""
    out = dest_dir / _output_name(src, src.name)
    if src.suffix.lower() in MARKDOWN_SUFFIXES and _have("pandoc"):
        subprocess.run(
            ["pandoc", "-f", "markdown", "-t", "html5", "-s",
             str(src), "-o", str(out)],
            check=True, capture_output=True, text=True,
        )
    else:
        shutil.copy2(src, out)
    return out


def _write_index(prep_dir: Path, files: list[Path], title: str, description: str) -> None:
    items = "\n".join(
        f'    <li><a href="{html.escape(_output_name(f, f.name))}">'
        f"{html.escape(f.name)}</a></li>"
        for f in files
    )
    prep_dir.joinpath("index.html").write_text(
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n  <meta charset="utf-8">\n'
        f"  <title>{html.escape(title)}</title>\n</head>\n<body>\n"
        f"  <h1>{html.escape(title)}</h1>\n"
        f"  <p>{html.escape(description)}</p>\n  <ul>\n{items}\n  </ul>\n"
        "</body>\n</html>\n",
        encoding="utf-8",
    )


def zimify(input_dir: Path, output_zim: Path, title: str = "",
           description: str = "Local knowledge base",
           language: str = "eng", creator: str = "InfoForge",
           publisher: str = "InfoForge") -> dict:
    """Prepare (and if possible build) a .zim from ``input_dir``.

    Returns a report dict: ``{"built": bool, "zim": path, "prep_dir": path,
    "files": n, "command": [...]}``.  ``built`` false means the prepared
    directory is on disk and ``command`` is what to run once zimwriterfs is
    installed.
    """
    input_dir = Path(input_dir).expanduser()
    if not input_dir.is_dir():
        raise ZimifyError(f"input directory not found: {input_dir}")

    output_zim = Path(output_zim).expanduser().resolve()
    output_zim.parent.mkdir(parents=True, exist_ok=True)
    title = title or input_dir.name

    prep_dir = Path(str(output_zim) + ".prep")
    if prep_dir.exists():
        shutil.rmtree(prep_dir)
    prep_dir.mkdir(parents=True)

    files = sorted(p for p in input_dir.iterdir() if p.is_file())
    if not files:
        raise ZimifyError(f"no files to convert in {input_dir}")
    for src in files:
        try:
            _convert(src, prep_dir)
        except subprocess.CalledProcessError as exc:
            raise ZimifyError(
                f"pandoc failed on {src}: {(exc.stderr or '').strip()[:300]}"
            ) from None

    _write_index(prep_dir, files, title, description)

    # --creator and --publisher are mandatory in current zim-tools; without
    # them zimwriterfs refuses the build with a usage error.
    command = [
        "zimwriterfs", "--welcome=index.html", f"--language={language}",
        f"--title={title}", f"--description={description}",
        f"--creator={creator}", f"--publisher={publisher}",
        ".", str(output_zim),
    ]

    if not _have("zimwriterfs"):
        return {"built": False, "zim": output_zim, "prep_dir": prep_dir,
                "files": len(files), "command": command}

    result = subprocess.run(command, cwd=prep_dir, capture_output=True, text=True)
    if result.returncode != 0:
        raise ZimifyError(
            f"zimwriterfs failed: {(result.stderr or result.stdout).strip()[:500]}"
        )
    return {"built": True, "zim": output_zim, "prep_dir": prep_dir,
            "files": len(files), "command": command}
