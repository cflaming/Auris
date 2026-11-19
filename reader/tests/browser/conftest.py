"""Browser tests: the real reader page in Chromium against a real Flask server (tone-engine TTS).

The server is started once per run and reset between tests. Each test gets:
    site     server handle: ids, stats(), reset
    reader   open_reader(chapter=None, segment=None) -> the loaded page
    perf     records what the page cost (requests, bytes, TTS encodes, script time); printed at the end
             of the run and saved to tests/browser/perf-report.json
Run just these with:  uv run --no-sync pytest tests/browser     (once: uv run --no-sync playwright install chromium)
"""

import json
import re
import socket
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

try:
    import playwright  # noqa: F401
except ImportError:                      # not installed: the rest of the suite still runs
    collect_ignore_glob = ["test_*.py"]

HERE = Path(__file__).parent
REPORT = HERE / "perf-report.json"
_RECORDS: list[dict] = []


def pytest_configure(config):
    config.addinivalue_line("markers", "browser: drives the real page in Chromium (slower)")


def pytest_collection_modifyitems(items):
    for item in items:
        if "tests/browser" in str(item.fspath).replace("\\", "/"):
            item.add_marker(pytest.mark.browser)


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args):
    # Playback starts from a click, but the page also resumes it from timers: don't let autoplay rules interfere.
    return {**browser_type_launch_args, "args": ["--autoplay-policy=no-user-gesture-required"]}


def _get(url, data=None):
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


@pytest.fixture(scope="session")
def _server(tmp_path_factory):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    data_dir = tmp_path_factory.mktemp("auris-data")
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "serve.py"), "--data-dir", str(data_dir), "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            _get(f"{base}/__ids")
            break
        except Exception:
            if proc.poll() is not None:
                pytest.fail("test server exited:\n" + proc.stdout.read())
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("test server did not start")
    yield base
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture
def site(_server):
    ids = _get(f"{_server}/__reset", data=b"")
    return SimpleNamespace(
        url=_server, book_id=ids["book_id"], chapter_id=ids["chapter_ids"][0],
        stats=lambda: _get(f"{_server}/__stats"),
        api=lambda path, data=None: _get(f"{_server}{path}", None if data is None else json.dumps(data).encode()),
    )


def _route(url: str) -> str:
    path = re.sub(r"^https?://[^/]+", "", url).split("?")[0]
    path = re.sub(r"/[0-9a-f]{32}/", "/:key/", path)
    return re.sub(r"/\d+", "/:n", path)


@pytest.fixture
def perf(page, site, request):
    """Records what the page cost during the test: requests by route, bytes, TTS encodes, main-thread time.
    (`reader` uses it, so a test that opens the reader needs nothing extra.)"""
    finished = []
    page.on("requestfinished", lambda req: finished.append(req))
    cdp = page.context.new_cdp_session(page)
    cdp.send("Performance.enable")
    yield
    metrics = {m["name"]: m["value"] for m in cdp.send("Performance.getMetrics")["metrics"]}
    by_route = Counter(f"{r.method} {_route(r.url)}" for r in finished)
    total = sum(r.sizes()["responseBodySize"] + r.sizes()["responseHeadersSize"] for r in finished)
    server = site.stats()
    _RECORDS.append({
        "test": request.node.name,
        "requests": len(finished),
        "by_route": dict(by_route),
        "bytes_downloaded": total,
        "tts_encodes": server["encodes"],
        "ffmpeg_conversions": server["fmp4_conversions"],
        "script_ms": round(metrics.get("ScriptDuration", 0) * 1000),
        "main_thread_ms": round(metrics.get("TaskDuration", 0) * 1000),
        "layouts": int(metrics.get("LayoutCount", 0)),
        "style_recalcs": int(metrics.get("RecalcStyleCount", 0)),
        "js_heap_kb": round(metrics.get("JSHeapUsedSize", 0) / 1024),
    })


@pytest.fixture
def reader(page, site, perf):
    """open_reader(chapter=None, segment=None): the reader page, loaded and ready."""
    def _open(chapter=None, segment=None):
        q = ""
        if segment is not None:
            q = f"?chapter={chapter or site.chapter_id}&segment={segment}"
        page.goto(f"{site.url}/reader/{site.book_id}{q}")
        page.wait_for_function("typeof chapter !== 'undefined' && chapter.segments.length > 0")
        return page
    return _open


def pytest_terminal_summary(terminalreporter):
    if not _RECORDS:
        return
    REPORT.write_text(json.dumps(_RECORDS, indent=2))
    tr = terminalreporter
    tr.section("browser cost per test")
    cols = ("requests", "bytes_downloaded", "tts_encodes", "ffmpeg_conversions", "script_ms", "main_thread_ms", "layouts")
    tr.write_line(f"{'test':58}" + "".join(f"{c[:11]:>12}" for c in cols))
    for r in _RECORDS:
        tr.write_line(f"{r['test'][:57]:58}" + "".join(f"{r[c]:>12}" for c in cols))
    tr.write_line(f"(details by route: {REPORT})")
