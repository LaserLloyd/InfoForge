"""Bring-your-own kiwix-serve: the whole stack works with no podman at all.

A tiny in-process HTTP server plays kiwix-serve and another plays the LLM, both
on ephemeral loopback ports (never a live-rig port), and the configuration comes
entirely from INFOFORGE_* environment variables.  Podman is made to fail loudly
if anything so much as looks for it.  This is the "any OS, set kiwix_url, do not
run `serve`" path from the README.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from infoforge import api, cli, kiwix

ARTICLE = ("<html><body><h1>Emu War</h1><p>The Emu War was a 1932 wildlife management "
           "operation in Western Australia.</p></body></html>")
RSS = ("<?xml version='1.0'?><rss version='2.0'><channel><item><title>Emu War</title>"
       "<link>/content/book/Emu_War</link><description>1932 operation</description>"
       "</item></channel></rss>")


class FakeKiwix(BaseHTTPRequestHandler):
    seen: list[str] = []

    def log_message(self, *args):
        pass

    def _send(self, body: str, ctype="text/html", status=200):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        FakeKiwix.seen.append(url.path)
        if url.path == "/":
            self._send("kiwix-serve")
        elif url.path == "/suggest":
            self._send("[]", "application/json")
        elif url.path == "/search":
            assert parse_qs(url.query)["books.name"] == ["book"]
            self._send(RSS, "application/xml")
        elif url.path == "/raw/book/content/Emu_War":
            self._send(ARTICLE)
        else:
            self._send("nope", status=404)


class FakeLLM(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._json({"data": [{"id": "byo-model"}]})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self._json({"choices": [{"message": {
            "content": "ANSWERED: According to Emu War, it happened in 1932."}}]})


@pytest.fixture
def stack(tmp_path, monkeypatch):
    servers = []
    for handler in (FakeKiwix, FakeLLM):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
    kiwix_port, llm_port = (s.server_address[1] for s in servers)
    FakeKiwix.seen = []

    zim_dir = tmp_path / "zims"
    zim_dir.mkdir()
    (zim_dir / "book.zim").write_bytes(b"x")
    for name, value in {
        "INFOFORGE_CONFIG": str(tmp_path / "no-such-file.yaml"),
        "INFOFORGE_MODEL_ID": "byo-model",
        "INFOFORGE_BASE_URL": f"http://127.0.0.1:{llm_port}/v1",
        "INFOFORGE_KIWIX_URL": f"http://127.0.0.1:{kiwix_port}",
        "INFOFORGE_ZIM_DIR": str(zim_dir),
        "INFOFORGE_ZIMS": json.dumps([{"file": "book.zim", "license": "CC BY-SA 4.0"}]),
        "NO_PROXY": "127.0.0.1,localhost",
    }.items():
        monkeypatch.setenv(name, value)

    # Podman must never be consulted on this path.
    monkeypatch.setattr(kiwix, "podman_available", lambda: False)
    monkeypatch.setattr(kiwix, "_podman",
                        lambda *a, **k: pytest.fail("podman was invoked in BYO mode"))
    yield
    for server in servers:
        server.shutdown()
        server.server_close()


def test_search_works_with_no_podman_and_no_config_file(stack, capsys):
    assert cli.main(["search", "--no-ensure", "--json", "--text", "emu war"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["query"] == "emu war"
    assert out["sources"][0]["title"] == "Emu War"
    assert "1932 wildlife management" in out["sources"][0]["text"]
    assert out["sources"][0]["url"].endswith("/content/book/Emu_War")
    assert "/search" in FakeKiwix.seen and "/raw/book/content/Emu_War" in FakeKiwix.seen


def test_plain_search_also_works_because_kiwix_is_already_up(stack, capsys):
    # Without --no-ensure the CLI checks HTTP first; podman is never reached.
    assert cli.main(["search", "emu war"]) == 0
    assert "[1] Emu War" in capsys.readouterr().out


def test_ask_works_end_to_end_with_attribution(stack, capsys):
    assert cli.main(["ask", "--json", "when was the emu war"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["verdict"] == "answered"
    assert out["answer"] == "According to Emu War, it happened in 1932."
    assert out["cited"] == ["Emu War"]
    assert out["sources"][0]["license"] == "CC BY-SA 4.0"
    assert "CC BY-SA 4.0" in out["attribution"]


def test_ask_text_output(stack, capsys):
    assert cli.main(["ask", "when was the emu war"]) == 0
    out = capsys.readouterr().out
    assert "According to Emu War" in out and "Citations:" in out and "Attribution:" in out


def test_status_and_doctor_report_a_healthy_podmanless_setup(stack, capsys):
    status = api.status()
    assert status["state"] == "unavailable" and status["podman"] is False
    assert status["kiwix_responding"] is True
    report = api.doctor()
    assert report["ok"] is True, report
    names = {c["name"]: c for c in report["checks"]}
    assert "skipped" in names["container"]["detail"]
    assert names["kiwix http"]["ok"] and names["search"]["ok"] and names["llm primary"]["ok"]
    assert cli.main(["doctor"]) == 0


def test_a_dead_kiwix_is_a_clear_error_without_podman(stack, monkeypatch, capsys):
    # A port that was listening a moment ago and now refuses connections.
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    probe.listen()
    dead_port = probe.getsockname()[1]
    probe.close()
    monkeypatch.setenv("INFOFORGE_KIWIX_URL", f"http://127.0.0.1:{dead_port}")
    assert cli.main(["search", "--json", "emu"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["error"]["code"] == "kiwix_unreachable"
    assert "podman is not installed" in out["error"]["message"]
    assert cli.main(["search", "--json", "--no-ensure", "emu"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["error"]["code"] == "kiwix_unreachable"
    assert "serve start" in out["error"]["message"]
