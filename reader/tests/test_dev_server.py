"""The dev server's scratch copy of your data: what it copies, and that it can never touch the original."""

import json
import sqlite3

import pytest

import dev_server


@pytest.fixture
def prod(tmp_path):
    """A small stand-in for the real data folders."""
    root = tmp_path / "prod"
    (root / "data").mkdir(parents=True)
    with sqlite3.connect(root / "data" / "reader.db") as conn:
        conn.execute("CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT)")
        conn.execute("INSERT INTO books VALUES (1, 'Real book')")
    (root / "data" / "settings.json").write_text('{"tts_engine": "kokoro"}')
    (root / "audio_cache" / "voice_refs").mkdir(parents=True)
    (root / "audio_cache" / "a.wav").write_bytes(b"audio-a")
    (root / "audio_cache" / "voice_refs" / "v.wav").write_bytes(b"voice-ref")
    (root / "uploads").mkdir()
    (root / "uploads" / "book.epub").write_bytes(b"epub-bytes")
    (root / "uploads" / "ref_1.wav").write_bytes(b"reference-clip")
    return root


@pytest.fixture(params=["auto", "copy"])         # clone where the filesystem allows it, and the plain-copy fallback
def force_copy(request):
    return request.param == "copy"


def books(db_path):
    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT id, title FROM books ORDER BY id").fetchall()


def test_the_scratch_folder_gets_the_database_settings_audio_and_uploads(prod, tmp_path, force_copy):
    dev = tmp_path / "dev"
    info = dev_server.build_scratch(prod, dev, force_copy=force_copy)

    assert books(dev / "data" / "reader.db") == [(1, "Real book")]
    assert json.loads((dev / "data" / "settings.json").read_text()) == {"tts_engine": "kokoro"}
    assert (dev / "audio_cache" / "a.wav").read_bytes() == b"audio-a"
    assert (dev / "audio_cache" / "voice_refs" / "v.wav").read_bytes() == b"voice-ref"
    assert (dev / "uploads" / "book.epub").read_bytes() == b"epub-bytes"
    assert list((dev / "exports").iterdir()) == []                          # exports are not copied
    assert set(info["modes"]) == {"audio_cache", "uploads"} and info["source"] == str(prod)
    assert (dev / dev_server.MARKER).exists()


def test_clones_are_used_when_the_filesystem_supports_them(prod, tmp_path):
    dev = tmp_path / "probe-dev"
    dev.mkdir()
    ok, why = dev_server.clone_check(prod, dev)
    if not ok:
        pytest.skip(f"no usable copy-on-write clones here: {why}")
    assert list(dev.iterdir()) == []                                        # the probe files are removed
    assert set(dev_server.build_scratch(prod, tmp_path / "dev")["modes"].values()) == {"clone"}


# ── The clone self-test: each way a "clone" could fail to be one ──────────────

@pytest.fixture
def fake_clone(monkeypatch):
    """Make the clone command something else, to see the self-test notice."""
    return lambda *cmd: monkeypatch.setattr(dev_server, "_clone_command", lambda: list(cmd))


def probe(prod, tmp_path):
    dev = tmp_path / "probe-dev"
    dev.mkdir()
    result = dev_server.clone_check(prod, dev)
    assert list(dev.iterdir()) == []                                        # never leaves probe files behind
    return result


def test_a_hardlink_pretending_to_be_a_clone_is_rejected(prod, tmp_path, fake_clone):
    fake_clone("ln")                                                        # `ln src dst`: a second name for the same file
    ok, why = probe(prod, tmp_path)
    assert not ok and "same file" in why


def test_a_symlink_pretending_to_be_a_clone_is_rejected(prod, tmp_path, fake_clone):
    fake_clone("ln", "-s")
    ok, why = probe(prod, tmp_path)
    assert not ok and "same file" in why


def copy_uses_visible_space(tmp_path):
    """Whether a plain full-size copy shows up in this filesystem's free-space figures at all
    (it does on APFS, btrfs, ext4, ...; not on exotic or deduplicating ones)."""
    import os, shutil
    src, dst = tmp_path / "space-src", tmp_path / "space-dst"
    with open(src, "wb") as f:
        f.write(os.urandom(1024 * 1024) * 32)
        f.flush()
        os.fsync(f.fileno())
    before = shutil.disk_usage(tmp_path).free
    shutil.copyfile(src, dst)
    return before - shutil.disk_usage(tmp_path).free > dev_server.PROBE_BYTES // 2


def test_a_full_copy_pretending_to_be_a_clone_is_rejected(prod, tmp_path, fake_clone):
    if not copy_uses_visible_space(tmp_path):
        pytest.skip("this filesystem does not show the space a plain copy uses")
    fake_clone("cp", "-p")                                                  # a real, separate, full-size copy
    ok, why = probe(prod, tmp_path)
    assert not ok and "disk space" in why


def test_a_clone_with_different_content_is_rejected(prod, tmp_path, fake_clone):
    fake_clone("sh", "-c", 'head -c 100 "$0" > "$1"')                       # a truncated copy
    ok, why = probe(prod, tmp_path)
    assert not ok and "content differs" in why


def test_a_failing_clone_command_means_no_clones(prod, tmp_path, fake_clone):
    fake_clone("false")
    ok, why = probe(prod, tmp_path)
    assert not ok and "cannot be cloned" in why


def test_nothing_to_clone_makes_no_claim(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    dev = tmp_path / "dev"
    dev.mkdir()
    assert dev_server.clone_check(empty, dev) == (False, "nothing to clone")


def test_a_failed_self_test_falls_back_to_a_safe_plain_copy_and_says_why(prod, tmp_path, fake_clone):
    fake_clone("ln")                                                        # a "clone" that would share the real files
    dev = tmp_path / "dev"
    info = dev_server.build_scratch(prod, dev)

    assert set(info["modes"].values()) == {"copy"} and "same file" in info["clone_note"]
    assert dev_server.shared_with_prod(prod, dev) == []                     # nothing is shared with the real data
    (dev / "uploads" / "ref_1.wav").open("wb").write(b"scribbled over")
    assert (prod / "uploads" / "ref_1.wav").read_bytes() == b"reference-clip"


def test_nothing_done_to_the_scratch_folder_reaches_the_real_data(prod, tmp_path, force_copy):
    dev = tmp_path / "dev"
    dev_server.build_scratch(prod, dev, force_copy=force_copy)

    # overwrite in place (the app does this when a reference clip is re-uploaded), delete, and edit the database
    (dev / "uploads" / "ref_1.wav").open("wb").write(b"scribbled over")
    (dev / "audio_cache" / "a.wav").open("wb").write(b"scribbled over")
    (dev / "audio_cache" / "voice_refs" / "v.wav").open("r+b").write(b"XX")
    (dev / "uploads" / "book.epub").unlink()
    with sqlite3.connect(dev / "data" / "reader.db") as conn:
        conn.execute("INSERT INTO books VALUES (2, 'Scratch only')")
        conn.execute("UPDATE books SET title='Changed' WHERE id=1")
    (dev / "data" / "settings.json").write_text("{}")

    assert (prod / "uploads" / "ref_1.wav").read_bytes() == b"reference-clip"
    assert (prod / "audio_cache" / "a.wav").read_bytes() == b"audio-a"
    assert (prod / "audio_cache" / "voice_refs" / "v.wav").read_bytes() == b"voice-ref"
    assert (prod / "uploads" / "book.epub").read_bytes() == b"epub-bytes"
    assert books(prod / "data" / "reader.db") == [(1, "Real book")]
    assert json.loads((prod / "data" / "settings.json").read_text()) == {"tts_engine": "kokoro"}


def test_an_existing_copy_is_reused_until_refreshed(prod, tmp_path):
    dev = tmp_path / "dev"
    _, built = dev_server.ensure_scratch(prod, dev)
    assert built
    with sqlite3.connect(dev / "data" / "reader.db") as conn:
        conn.execute("INSERT INTO books VALUES (2, 'Made while testing')")

    _, built = dev_server.ensure_scratch(prod, dev)                         # a restart: keeps what you did
    assert not built and books(dev / "data" / "reader.db") == [(1, "Real book"), (2, "Made while testing")]

    with sqlite3.connect(prod / "data" / "reader.db") as conn:              # the real data moves on...
        conn.execute("INSERT INTO books VALUES (3, 'New in the real data')")
    _, built = dev_server.ensure_scratch(prod, dev)
    assert not built and (3, "New in the real data") not in books(dev / "data" / "reader.db")   # ...the copy does not follow

    _, built = dev_server.ensure_scratch(prod, dev, refresh=True)           # until you ask it to
    assert built and books(dev / "data" / "reader.db") == [(1, "Real book"), (3, "New in the real data")]


def test_it_will_not_delete_a_folder_it_did_not_create(prod, tmp_path):
    other = tmp_path / "somebody-elses-folder"
    other.mkdir()
    (other / "important.txt").write_text("keep me")
    with pytest.raises(SystemExit):
        dev_server.build_scratch(prod, other)
    assert (other / "important.txt").read_text() == "keep me"


def test_it_will_not_use_the_real_data_folder_as_the_scratch_folder(prod):
    with pytest.raises(SystemExit):
        dev_server.build_scratch(prod, prod)
    assert books(prod / "data" / "reader.db") == [(1, "Real book")]


def test_the_database_snapshot_ignores_a_write_that_is_still_in_progress(prod, tmp_path):
    busy = sqlite3.connect(prod / "data" / "reader.db")                     # the real server, mid-write
    busy.execute("BEGIN")
    busy.execute("INSERT INTO books VALUES (2, 'not committed yet')")
    try:
        dev_server.build_scratch(prod, tmp_path / "dev")
    finally:
        busy.rollback()
        busy.close()
    assert books(tmp_path / "dev" / "data" / "reader.db") == [(1, "Real book")]


def test_a_missing_audio_cache_or_uploads_folder_is_fine(prod, tmp_path):
    import shutil
    shutil.rmtree(prod / "audio_cache")
    shutil.rmtree(prod / "uploads")
    dev_server.build_scratch(prod, tmp_path / "dev")
    assert (tmp_path / "dev" / "audio_cache").is_dir() and (tmp_path / "dev" / "uploads").is_dir()


# ── Validation before it starts ───────────────────────────────────────────────

def test_a_normal_copy_passes_the_independence_check(prod, tmp_path, force_copy):
    dev = tmp_path / "dev"
    dev_server.build_scratch(prod, dev, force_copy=force_copy)
    assert dev_server.shared_with_prod(prod, dev) == []


def test_a_hardlink_or_symlink_to_the_real_data_is_detected(prod, tmp_path):
    import os
    dev = tmp_path / "dev"
    dev_server.build_scratch(prod, dev)

    (dev / "uploads" / "ref_1.wav").unlink()
    os.link(prod / "uploads" / "ref_1.wav", dev / "uploads" / "ref_1.wav")            # same file, second name
    (dev / "audio_cache" / "a.wav").unlink()
    (dev / "audio_cache" / "a.wav").symlink_to(prod / "audio_cache" / "a.wav")         # a pointer to the real file

    shared = dev_server.shared_with_prod(prod, dev)
    assert sorted(p.rsplit("/", 1)[-1] for p in shared) == ["a.wav", "ref_1.wav"]
    with pytest.raises(SystemExit, match="same file as your real data"):
        dev_server.verify_independent(prod, dev)


def test_a_reused_copy_is_checked_too_not_just_a_fresh_one(prod, tmp_path):
    import os
    dev = tmp_path / "dev"
    dev_server.ensure_scratch(prod, dev)
    (dev / "uploads" / "book.epub").unlink()
    os.link(prod / "uploads" / "book.epub", dev / "uploads" / "book.epub")
    with pytest.raises(SystemExit, match="--refresh"):
        dev_server.ensure_scratch(prod, dev)                                          # a restart, not a rebuild


@pytest.mark.parametrize("inside", ["data", "audio_cache", "uploads", "exports"])
def test_a_scratch_folder_inside_the_real_data_is_refused(prod, inside):
    with pytest.raises(SystemExit, match="inside the real data folder"):
        dev_server.build_scratch(prod, prod / inside / "scratch")
    assert books(prod / "data" / "reader.db") == [(1, "Real book")]


def test_the_real_data_root_follows_the_variable_if_you_use_it(tmp_path, monkeypatch):
    monkeypatch.setenv("AURIS_DATA_DIR", str(tmp_path / "elsewhere"))
    assert dev_server.real_data_root() == (tmp_path / "elsewhere").resolve()
    monkeypatch.delenv("AURIS_DATA_DIR")
    assert dev_server.real_data_root() == dev_server.APP_DIR.resolve()


def test_it_refuses_to_start_if_the_app_is_not_using_the_scratch_folder(tmp_path):
    # In this test process the app was imported without AURIS_DATA_DIR, i.e. pointing at the real folders.
    with pytest.raises(SystemExit, match="not using the scratch folder"):
        dev_server.check_runtime_paths(tmp_path)


def test_it_starts_when_the_app_is_using_the_scratch_folder(tmp_path):
    import os, subprocess, sys
    code = ("import dev_server, pathlib; dev_server.check_runtime_paths(pathlib.Path(%r)); print('ok')" % str(tmp_path))
    env = {**os.environ, "AURIS_DATA_DIR": str(tmp_path)}                    # what main() sets before importing the app
    out = subprocess.run([sys.executable, "-c", code], cwd=dev_server.APP_DIR, env=env, capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), out.stderr
