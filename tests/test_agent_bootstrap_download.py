"""Run the exact Windows bootstrap downloader against loopback fault fixtures."""
from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import ssl
import threading
import time
from types import SimpleNamespace
import urllib.error

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = b"synthetic-agent-package-" * 8000
SHA = hashlib.sha256(PAYLOAD).hexdigest()


@pytest.fixture
def downloader():
    text = (ROOT / "deploy/install-from-hub.ps1").read_text(encoding="utf-8")
    code = text.split("$CodePierDownload = @'\n", 1)[1].split("\n'@", 1)[0]
    namespace = {"__name__": "bootstrap_download"}
    exec(compile(code, "bootstrap-download", "exec"), namespace)
    delays = []
    namespace["time"] = SimpleNamespace(monotonic=time.monotonic, sleep=delays.append)
    return SimpleNamespace(download=namespace["download"], namespace=namespace, delays=delays, code=code)


@pytest.fixture
def server():
    state = SimpleNamespace(modes=[], requests=[])

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            index = len(state.requests)
            state.requests.append({"path": self.path, "headers": dict(self.headers)})
            mode = state.modes[min(index, len(state.modes) - 1)] if state.modes else "ok"
            if mode == "slow":
                time.sleep(0.15)
            status = int(mode) if mode.isdigit() else (302 if mode == "redirect" else 200)
            self.send_response(status)
            if mode == "redirect":
                self.send_header("Location", "/must-not-follow")
            etag = "0" * 64 if mode == "etag" else SHA
            if mode != "no-etag":
                self.send_header("ETag", ('W/' if mode == "weak-etag" else '') + '"' + etag + '"')
            body = b"<html>gateway error</html>" if mode == "html" else PAYLOAD
            lengths = {"oversize": "8388609", "empty": "0", "invalid-length": "invalid"}
            if mode not in {"no-length", "stream-oversize"}:
                self.send_header("Content-Length", lengths.get(mode, str(len(body))))
            self.end_headers()
            if mode == "truncate":
                body = body[:70000]
            if mode == "corrupt":
                body = b"x" + body[1:]
            try:
                if mode == "slow-body":
                    self.wfile.write(body[:1000])
                    self.wfile.flush()
                    time.sleep(0.15)
                    self.wfile.write(body[1000:])
                elif mode == "stream-oversize":
                    for _ in range(129):
                        self.wfile.write(b"x" * 65536)
                elif mode != "oversize":
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{httpd.server_port}"
    try:
        yield state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("first", ["truncate", "408", "429", "500", "502", "503", "504", "slow", "slow-body"])
def test_transient_download_restarts_without_appending(downloader, server, tmp_path, first, capsys):
    server.modes = [first, "ok"]
    output = tmp_path / "Agent package [test].zip"
    downloader.download(server.url, SHA, output, timeout=0.05 if first.startswith("slow") else 2)
    assert output.read_bytes() == PAYLOAD
    assert not output.with_name(output.name + ".part").exists()
    assert len(server.requests) == 2 and downloader.delays == [1]
    assert {r["path"] for r in server.requests} == {"/agent/agent.zip?sha256=" + SHA}
    assert all("Range" not in r["headers"] and "Authorization" not in r["headers"] for r in server.requests)
    assert all(r["headers"].get("Accept-Encoding") == "identity" for r in server.requests)
    log = capsys.readouterr().out
    assert "attempt 1/4" in log and "attempt 2/4" in log
    assert "retrying from the beginning" in log and "SHA-256 matched" in log


@pytest.mark.parametrize("mode,message", [
    ("409", "HTTP 409"), ("401", "HTTP 401"), ("403", "HTTP 403"), ("redirect", "HTTP 302"),
    ("etag", "identity changed"), ("weak-etag", "identity changed"),
    ("oversize", "response size"), ("empty", "response size"), ("invalid-length", "response size"),
    ("stream-oversize", "size limit"), ("206", "response status"),
    ("html", "checksum mismatch"), ("corrupt", "checksum mismatch"),
])
def test_untrusted_or_permanent_failure_never_retries_or_publishes(downloader, server, tmp_path, mode, message):
    server.modes = [mode, "ok"]
    output = tmp_path / "agent.zip"
    with pytest.raises(ValueError, match=message):
        downloader.download(server.url, SHA, output, timeout=2)
    assert len(server.requests) == 1 and downloader.delays == []
    assert not output.exists() and not output.with_name("agent.zip.part").exists()


@pytest.mark.parametrize("mode", ["ok", "no-length", "no-etag"])
def test_verified_download_allows_optional_headers(downloader, server, tmp_path, mode):
    server.modes = [mode]
    output = tmp_path / "agent.zip"
    downloader.download(server.url, SHA, output, timeout=2)
    assert output.read_bytes() == PAYLOAD
    assert len(server.requests) == 1 and downloader.delays == []
    assert not output.with_name("agent.zip.part").exists()


def test_retries_are_bounded_and_partial_is_removed(downloader, server, tmp_path):
    server.modes = ["truncate"]
    output = tmp_path / "agent.zip"
    with pytest.raises(ValueError, match="after 4 attempts"):
        downloader.download(server.url, SHA, output, timeout=2)
    assert len(server.requests) == 4 and downloader.delays == [1, 2, 4]
    assert not output.exists() and not output.with_name("agent.zip.part").exists()


@pytest.mark.parametrize("name", ["agent.zip", "agent.zip.part"])
@pytest.mark.parametrize("symlink", [False, True])
def test_existing_bytes_are_never_reused_or_overwritten(downloader, server, tmp_path, name, symlink):
    output = tmp_path / "agent.zip"
    existing = tmp_path / name
    if symlink:
        try:
            existing.symlink_to(tmp_path / "absent")
        except OSError:
            pytest.skip("Symlink creation unavailable")
    else:
        existing.write_bytes(b"preserve")
    with pytest.raises(ValueError, match="already exists"):
        downloader.download(server.url, SHA, output)
    assert existing.is_symlink() if symlink else existing.read_bytes() == b"preserve"
    assert not server.requests


@pytest.mark.parametrize("wrapped", [False, True])
def test_tls_verification_failure_is_not_retried(downloader, monkeypatch, tmp_path, wrapped):
    calls = []
    class Opener:
        def open(self, *args, **kwargs):
            calls.append(args)
            error = ssl.SSLCertVerificationError("fixture")
            raise urllib.error.URLError(error) if wrapped else error
    monkeypatch.setattr(downloader.namespace["urllib"].request, "build_opener", lambda *_: Opener())
    with pytest.raises(ValueError, match="certificate verification"):
        downloader.download("https://panel.invalid", SHA, tmp_path / "agent.zip")
    assert len(calls) == 1 and downloader.delays == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("hub,sha,timeout,attempts", [
    ("file:///tmp/package", SHA, 30, 4), ("http://", SHA, 30, 4),
    ("https://user:secret@panel.invalid", SHA, 30, 4),
    ("https://panel.invalid?query=1", SHA, 30, 4), ("https://panel.invalid#fragment", SHA, 30, 4),
    ("https://panel.invalid", "A"*64, 30, 4), ("https://panel.invalid", SHA, 0, 4),
    ("https://panel.invalid", SHA, 121, 4), ("https://panel.invalid", SHA, 30, 0),
    ("https://panel.invalid", SHA, 30, 6),
])
def test_invalid_inputs_do_not_open_network(downloader, monkeypatch, tmp_path, hub, sha, timeout, attempts):
    monkeypatch.setattr(downloader.namespace["urllib"].request, "build_opener",
                        lambda *_: pytest.fail("invalid input opened network"))
    with pytest.raises(ValueError):
        downloader.download(hub, sha, tmp_path / "agent.zip", timeout, attempts)
    assert list(tmp_path.iterdir()) == [] and downloader.delays == []


def test_attempt_time_budget_cleans_partial_and_bounds_retries(downloader, server, tmp_path):
    ticks = iter(range(0, 10000, 121))
    downloader.namespace["time"].monotonic = lambda: next(ticks)
    output = tmp_path / "agent.zip"
    with pytest.raises(ValueError, match="after 2 attempts"):
        downloader.download(server.url, SHA, output, timeout=2, attempts=2)
    assert len(server.requests) == 2 and downloader.delays == [1]
    assert list(tmp_path.iterdir()) == []


def test_publish_failure_cleans_partial_without_retry(downloader, server, monkeypatch, tmp_path):
    def fail(*args):
        raise OSError("fixture publish failed")
    monkeypatch.setattr(downloader.namespace["os"], "replace", fail)
    with pytest.raises(OSError, match="publish failed"):
        downloader.download(server.url, SHA, tmp_path / "agent.zip", timeout=2)
    assert len(server.requests) == 1 and downloader.delays == []
    assert list(tmp_path.iterdir()) == []


def test_bootstrap_writes_payload_before_call_for_powershell_51_quoting():
    text = (ROOT / "deploy/install-from-hub.ps1").read_text(encoding="utf-8")
    assert "Set-Content -LiteralPath $DownloadHelper -Value $CodePierDownload -Encoding UTF8" in text
    assert "& $CodePierPython -I $DownloadHelper $Hub $Sha256 $Archive $DownloadTimeoutSec $DownloadAttempts" in text
    assert text.index("& $CodePierPython -I $DownloadHelper") < text.index("$env:CODEPIER_INSTALL_TOKEN = $Token")
    assert "[ValidateRange(1,5)][int]$DownloadAttempts = 4" in text
    assert "[ValidateRange(5,120)][int]$DownloadTimeoutSec = 30" in text
