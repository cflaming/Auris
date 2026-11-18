#!/usr/bin/env python3
"""Run Auris against a scratch copy of your data, so you can try things without touching it.

    python dev_server.py                 # port 7862, TTS disabled (nothing is generated)
    python dev_server.py --real-engine   # same, but with the real TTS engine
    python dev_server.py --refresh       # throw the scratch copy away and copy your data again

The scratch copy lives in reader/.dev. It is made from your real data the first time and then
REUSED on later starts, so whatever you change while testing (corrections, settings, ...) is still
there next time. It does not follow your real data afterwards; use --refresh to catch up.

What is copied: the database (a consistent snapshot, even while the real server is running),
settings, and the audio cache and uploads. Those two are cloned where the filesystem supports
copy-on-write (APFS, btrfs, xfs): instant, no extra disk space until something changes, and
changing or deleting a clone can never affect the original. Elsewhere they are plain copies.
Before cloning, a self-test on throwaway files proves the clone really is copy-on-write: a separate
file, no real disk space used, and writing to it leaves the original alone.
(Hardlinks and symlinks are deliberately not used: they share the original file, so writing to
one writes to your real data.)
"""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
DEFAULT_SCRATCH = APP_DIR / ".dev"
MARKER = ".auris-dev.json"          # present only in folders this script created
CLONED_DIRS = ("audio_cache", "uploads")


def _clone_command():
    """The cp invocation that makes copy-on-write clones on this platform, or None."""
    if sys.platform == "darwin":
        return ["cp", "-cpR"]
    if sys.platform.startswith("linux"):
        return ["cp", "--reflink=always", "-pR"]
    return None


PROBE_BYTES = 32 * 1024 * 1024       # big enough that a full copy is obvious in the free-space figures


def _run_clone(cmd, src: Path, dst: Path) -> bool:
    return subprocess.run([*cmd, str(src), str(dst)], capture_output=True).returncode == 0


def clone_check(prod: Path, dev: Path) -> tuple[bool, str]:
    """Whether copy-on-write clones can safely be used here, and if not, why not.

    1. A real file from your data can be cloned into the scratch folder (same volume, filesystem support).
    2. On throwaway files in the scratch folder, the clone behaves like one: it is a separate file (not
       a hardlink or symlink), it takes no real disk space (a full copy would), and writing to it
       changes the clone but leaves the original alone. Your own files are never written to.
    """
    cmd = _clone_command()
    if not cmd:
        return False, "this platform has no copy-on-write clone command"
    sample = next((f for name in CLONED_DIRS for f in (prod / name).rglob("*") if f.is_file()), None)
    if sample is None:
        return False, "nothing to clone"
    real, a, b = dev / ".clone-probe-real", dev / ".clone-probe-a", dev / ".clone-probe-b"
    try:
        if not _run_clone(cmd, sample, real):
            return False, "your data cannot be cloned into the scratch folder (different volume, or no filesystem support)"

        data = os.urandom(1024 * 1024) * (PROBE_BYTES // (1024 * 1024))
        with open(a, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        free_before = shutil.disk_usage(dev).free
        if not _run_clone(cmd, a, b):
            return False, "cloning a scratch file failed"
        free_after = shutil.disk_usage(dev).free

        if b.is_symlink() or os.path.samefile(a, b):                       # checked before anything is written
            return False, "the 'clone' is the same file as the original, not a separate one"
        if b.read_bytes() != data:
            return False, "the clone's content differs from the original"
        if free_before - free_after > PROBE_BYTES // 2:
            return False, "cloning used real disk space, so it is a full copy and not copy-on-write"

        marker = b"CLONE-WRITE-TEST"
        with open(b, "r+b") as f:                                          # write into the clone, in place
            f.seek(len(data) // 2)
            f.write(marker)
            f.flush()
            os.fsync(f.fileno())
        if a.read_bytes() != data:
            return False, "writing to the clone changed the original"
        if b.read_bytes()[len(data) // 2:][:len(marker)] != marker:
            return False, "a write to the clone was lost"
        return True, ""
    finally:
        for probe in (real, a, b):
            probe.unlink(missing_ok=True)


def copy_dir(src: Path, dst: Path, clone: bool) -> str:
    """Copy src's contents into dst (created); returns how it was done: 'clone' or 'copy'."""
    dst.mkdir(parents=True, exist_ok=True)
    if clone and subprocess.run([*_clone_command(), f"{src}/.", f"{dst}/"], capture_output=True).returncode == 0:
        return "clone"
    shutil.copytree(src, dst, dirs_exist_ok=True)
    return "copy"


def snapshot_db(src: Path, dst: Path):
    """A consistent copy of a SQLite database, safe while another process is using it."""
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    target = sqlite3.connect(dst)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def real_data_root() -> Path:
    """Where the real data lives: AURIS_DATA_DIR if you run your real install with it set, else next to the app."""
    return Path(os.environ.get("AURIS_DATA_DIR") or APP_DIR).expanduser().resolve()


def check_locations(prod: Path, dev: Path):
    """The scratch folder must not be, or sit inside, any of the real data folders."""
    dev, prod = dev.resolve(), prod.resolve()
    if dev == prod:
        raise SystemExit(f"Refusing to use the real data folder ({prod}) as the scratch folder.")
    for name in ("data", "exports", *CLONED_DIRS):
        if dev == prod / name or (prod / name) in dev.parents:
            raise SystemExit(f"The scratch folder {dev} is inside the real data folder {prod / name}.")


def shared_with_prod(prod: Path, dev: Path) -> list[str]:
    """Files in the scratch folder that are really the same file as one in the real data (a symlink
    or hardlink), so that writing to them would write to your real data. Clones and copies are fine."""
    shared = []
    for name in ("data", *CLONED_DIRS):
        root = dev / name
        if root.is_symlink():
            shared.append(str(root))
            continue
        for f in root.rglob("*"):
            if f.is_symlink():
                shared.append(str(f))
            elif f.is_file():
                twin = prod / f.relative_to(dev)
                if twin.exists() and os.path.samefile(f, twin):
                    shared.append(str(f))
    return shared


def verify_independent(prod: Path, dev: Path):
    shared = shared_with_prod(prod, dev)
    if shared:
        raise SystemExit(f"{len(shared)} file(s) in the scratch folder are the same file as your real data "
                         f"(e.g. {shared[0]}). Not starting; run with --refresh to rebuild the copy.")


def check_runtime_paths(scratch: Path):
    """The app reads its data locations when it is imported. Refuse to start unless every one of
    them really points inside the scratch folder (the last line of defence for your real data)."""
    import app
    import core.base_engine
    import core.database
    import core.exporter
    import core.settings
    in_use = {
        "database": core.database.get_db_path(), "settings": core.settings.SETTINGS_FILE,
        "audio cache": core.base_engine.AUDIO_CACHE_DIR, "uploads": app.UPLOAD_DIR,
        "exports": core.exporter.EXPORTS_DIR, "chapter renders": app._CHAPTER_RENDER_DIR,
    }
    outside = {name: str(path) for name, path in in_use.items() if scratch.resolve() not in Path(path).resolve().parents}
    if outside:
        raise SystemExit("Not starting: the app is not using the scratch folder for: "
                         + ", ".join(f"{k} ({v})" for k, v in outside.items()))


def build_scratch(prod: Path, dev: Path, force_copy: bool = False) -> dict:
    """(Re)create the scratch folder from the data under `prod`. Returns what was recorded about it."""
    check_locations(prod, dev)
    if dev.exists():
        if not (dev / MARKER).exists():
            raise SystemExit(f"{dev} exists and was not created by dev_server.py; not touching it.")
        shutil.rmtree(dev)
    (dev / "data").mkdir(parents=True)
    (dev / "exports").mkdir()

    if (prod / "data" / "reader.db").exists():
        snapshot_db(prod / "data" / "reader.db", dev / "data" / "reader.db")
        with sqlite3.connect(dev / "data" / "reader.db") as conn:
            if (result := conn.execute("PRAGMA quick_check").fetchone()[0]) != "ok":
                raise SystemExit(f"The copied database failed its integrity check: {result}")
    if (prod / "data" / "settings.json").exists():
        shutil.copy2(prod / "data" / "settings.json", dev / "data" / "settings.json")

    clone, why_not = (False, "") if force_copy else clone_check(prod, dev)
    modes = {}
    for name in CLONED_DIRS:
        if (prod / name).is_dir():
            modes[name] = copy_dir(prod / name, dev / name, clone)
        else:
            (dev / name).mkdir()
    info = {"created_at": time.time(), "source": str(prod), "modes": modes}
    if why_not and "nothing to clone" not in why_not:
        info["clone_note"] = why_not                  # shown at startup: why this is a plain copy
    (dev / MARKER).write_text(json.dumps(info))
    return info


def ensure_scratch(prod: Path, dev: Path, refresh: bool = False, force_copy: bool = False) -> tuple[dict, bool]:
    """The scratch folder's info, and whether it was (re)built just now. An existing one is reused.
    Either way it is checked to be independent of the real data before it is handed back."""
    check_locations(prod, dev)
    built = refresh or not (dev / MARKER).exists()
    info = build_scratch(prod, dev, force_copy) if built else json.loads((dev / MARKER).read_text())
    verify_independent(prod, dev)
    return info, built


def _age(seconds: float) -> str:
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            n = int(seconds // size)
            return f"{n} {unit}{'' if n == 1 else 's'}"
    return "moments"


def main():
    parser = argparse.ArgumentParser(description="Run Auris against a scratch copy of your data.")
    parser.add_argument("--port", type=int, default=7862)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--refresh", action="store_true", help="discard the scratch copy and copy your data again")
    parser.add_argument("--real-engine", action="store_true", help="use the real TTS engine (default: TTS disabled)")
    parser.add_argument("--dir", type=Path, default=DEFAULT_SCRATCH, help=f"scratch folder (default: {DEFAULT_SCRATCH})")
    args = parser.parse_args()

    prod, scratch = real_data_root(), args.dir.resolve()      # read before AURIS_DATA_DIR is pointed at the scratch folder
    info, built = ensure_scratch(prod, scratch, refresh=args.refresh)

    os.environ["AURIS_DATA_DIR"] = str(scratch)      # before anything from core/ or app is imported
    sys.path.insert(0, str(APP_DIR))
    import app
    check_runtime_paths(scratch)

    if not args.real_engine:
        real = app.tts

        class StubEngine:
            """Reports not-ready and never generates. Its cache tag matches the real engine's, so audio
            already in the (copied) cache still counts as current."""
            engine_name, cache_tag = real.engine_name, real.cache_tag
            def load_async(self): pass
            def status(self): return {"state": "loading", "message": "dev server: TTS disabled (--real-engine to enable)"}
            def apply_settings(self, updates): pass
            def set_engine(self, name): return False
            def close(self): pass

        app.tts = StubEngine()

    how = ", ".join(f"{k}: {v}" for k, v in info["modes"].items()) or "nothing to copy"
    print(f"\n  Auris dev server  ->  http://{args.host}:{args.port}")
    print(f"  Scratch data : {scratch}")
    print(f"  {'Copied just now' if built else 'Reusing the copy made ' + _age(time.time() - info['created_at']) + ' ago'}"
          f" from {info['source']} ({how})")
    if info.get("clone_note"):
        print(f"  Note         : plain copy, not clones ({info['clone_note']})")
    print(f"  TTS          : {'real engine' if args.real_engine else 'disabled (nothing is generated)'}")
    print("  Your real data is not touched. --refresh copies it again.\n")
    app.app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
