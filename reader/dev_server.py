#!/usr/bin/env python3
"""Run Auris against a scratch copy of your data, so you can try things without touching it.

    python dev_server.py                 # port 7862, TTS disabled (nothing is generated)
    python dev_server.py --real-engine   # same, but with the real TTS engine
    python dev_server.py --refresh       # throw the scratch copy away and copy your data again

The scratch copy lives in reader/.dev. It is made from your real data the first time and then
REUSED on later starts, so whatever you change while testing (corrections, settings, ...) is still
there next time. It does not follow your real data afterwards; use --refresh to catch up.

Protected: your database, settings, audio cache, uploads and exports. NOT protected, because they
live outside the data folder: models (the default model path and any model download you trigger from
Settings), the Hugging Face cache used by --real-engine, and the spaCy install button, which installs
into your real Python environment.

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
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path

from core.layout import APP_DIR, FILES, layout, resolve_root     # pure: safe to import before the data folder is switched

DEFAULT_SCRATCH = APP_DIR / ".dev"
MARKER = ".auris-dev.json"          # present only in folders this script created
CLONED = ("audio_cache", "uploads")  # the layout() entries that are cloned (or copied) as folders
PROBE_BYTES = 8 * 1024 * 1024        # large enough that a full copy is obvious in the free-space figures


def _clone_command():
    """The cp invocation that makes copy-on-write clones on this platform, or None."""
    if sys.platform == "darwin":
        return ["cp", "-cpR"]
    if sys.platform.startswith("linux"):
        return ["cp", "--reflink=always", "-pR"]
    return None


def _run_clone(cmd, src: Path, dst: Path) -> bool:
    return subprocess.run([*cmd, str(src), str(dst)], capture_output=True).returncode == 0


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def clone_check(prod_layout: dict, dev: Path) -> tuple[bool, str]:
    """Whether copy-on-write clones can safely be used here, and if not, why not.

    1. A real file from your data can be cloned into the scratch folder (same volume, filesystem support).
    2. On throwaway files in the scratch folder, the clone behaves like one: it is a separate file (not
       a hardlink or symlink), it takes no real disk space (a full copy would), and writing to it
       changes the clone but leaves the original alone. Your own files are never written to.
    """
    cmd = _clone_command()
    if not cmd:
        return False, "this platform has no copy-on-write clone command"
    sample = next((f for name in CLONED for f in prod_layout[name].rglob("*") if f.is_file()), None)
    if sample is None:
        return False, "nothing to clone"
    real, a, b = dev / ".clone-probe-real", dev / ".clone-probe-a", dev / ".clone-probe-b"
    try:
        if not _run_clone(cmd, sample, real):
            return False, "your data cannot be cloned into the scratch folder (different volume, or no filesystem support)"

        block = os.urandom(1024 * 1024)                                    # random, so nothing can compress or dedupe it
        with open(a, "wb") as f:
            for _ in range(PROBE_BYTES // len(block)):
                f.write(block)
            f.flush()
            os.fsync(f.fileno())
        original = _digest(a)
        free_before = shutil.disk_usage(dev).free
        if not _run_clone(cmd, a, b):
            return False, "cloning a scratch file failed"
        free_after = shutil.disk_usage(dev).free

        if b.is_symlink() or os.path.samefile(a, b):                       # checked before anything is written
            return False, "the 'clone' is the same file as the original, not a separate one"
        if _digest(b) != original:
            return False, "the clone's content differs from the original"
        if free_before - free_after > PROBE_BYTES // 2:
            return False, "cloning used real disk space, so it is a full copy and not copy-on-write"

        marker, at = b"CLONE-WRITE-TEST", PROBE_BYTES // 2
        with open(b, "r+b") as f:                                          # write into the clone, in place
            f.seek(at)
            f.write(marker)
            f.flush()
            os.fsync(f.fileno())
        if _digest(a) != original:
            return False, "writing to the clone changed the original"
        with open(b, "rb") as f:
            f.seek(at)
            if f.read(len(marker)) != marker:
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


def check_locations(prod: Path, dev: Path):
    """The scratch folder must not be, or sit inside, any of the real data folders."""
    dev, prod = dev.resolve(), prod.resolve()
    if dev == prod:
        raise SystemExit(f"Refusing to use the real data folder ({prod}) as the scratch folder.")
    for name, path in layout(prod).items():
        folder = path.parent if name in FILES else path                  # the folder holding a file, or the folder itself
        if dev == folder or folder in dev.parents:
            raise SystemExit(f"The scratch folder {dev} is inside the real data folder {folder}.")


def shared_files(dev: Path) -> list[str]:
    """Files in the scratch copy that could be someone else's: a symlink, or a file with more than one
    name (a hardlink, which would be the real file's second name). Clones and copies have neither, and
    this needs only one lstat per file, with no lookups against the real data."""
    found = []
    for name in ("data", *CLONED):
        root = dev / name
        if root.is_symlink():
            found.append(str(root))
            continue
        for folder, subfolders, files in os.walk(root):
            found += [os.path.join(folder, d) for d in subfolders if os.path.islink(os.path.join(folder, d))]
            for f in files:
                st = os.lstat(os.path.join(folder, f))
                if stat.S_ISLNK(st.st_mode) or st.st_nlink > 1:
                    found.append(os.path.join(folder, f))
    return found


def verify_independent(dev: Path):
    shared = shared_files(dev)
    if shared:
        raise SystemExit(f"{len(shared)} file(s) in the scratch folder are linked to other files "
                         f"(e.g. {shared[0]}), so they might be your real data. Not starting; "
                         "run with --refresh to rebuild the copy.")


def check_runtime_paths(scratch: Path):
    """The app reads its data locations when it is imported. Refuse to start unless every location
    in core.paths, and the paths the modules actually use, are inside the scratch folder."""
    import app
    import core.base_engine
    import core.database
    import core.exporter
    import core.paths
    import core.settings
    in_use = {
        **{f"paths.{name}": path for name, path in core.paths.LOCATIONS.items()},    # a new location can't be missed
        "database in use": core.database.get_db_path(), "settings in use": core.settings.SETTINGS_FILE,
        "audio cache in use": core.base_engine.AUDIO_CACHE_DIR, "uploads in use": app.UPLOAD_DIR,
        "exports in use": core.exporter.EXPORTS_DIR, "chapter renders in use": app._CHAPTER_RENDER_DIR,
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
    real, scratch = layout(prod), layout(dev)
    for name, path in scratch.items():
        (path.parent if name in FILES else path).mkdir(parents=True, exist_ok=True)

    if real["database"].exists():
        snapshot_db(real["database"], scratch["database"])
        with sqlite3.connect(scratch["database"]) as conn:
            if (result := conn.execute("PRAGMA quick_check").fetchone()[0]) != "ok":
                raise SystemExit(f"The copied database failed its integrity check: {result}")
    if real["settings"].exists():
        shutil.copy2(real["settings"], scratch["settings"])

    clone, why_not = (False, "") if force_copy else clone_check(real, dev)
    modes = {name: copy_dir(real[name], scratch[name], clone) for name in CLONED if real[name].is_dir()}
    info = {"created_at": time.time(), "modes": modes}
    if why_not and why_not != "nothing to clone":
        info["clone_note"] = why_not                  # explains why THIS copy is a plain copy
    (dev / MARKER).write_text(json.dumps(info))
    return info


def ensure_scratch(prod: Path, dev: Path, refresh: bool = False, force_copy: bool = False) -> dict:
    """The scratch folder's info. An existing copy is reused; either way it is checked to be
    independent of the real data before it is handed back."""
    info = build_scratch(prod, dev, force_copy) if refresh or not (dev / MARKER).exists() else json.loads((dev / MARKER).read_text())
    verify_independent(dev)
    return info


def _age(seconds: float) -> str:
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            n = int(seconds // size)
            return f"{n} {unit}{'' if n == 1 else 's'}"
    return "under a minute"


def main():
    parser = argparse.ArgumentParser(description="Run Auris against a scratch copy of your data.")
    parser.add_argument("--port", type=int, default=7862)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--refresh", action="store_true", help="discard the scratch copy and copy your data again")
    parser.add_argument("--real-engine", action="store_true", help="use the real TTS engine (default: TTS disabled)")
    parser.add_argument("--dir", type=Path, default=DEFAULT_SCRATCH, help=f"scratch folder (default: {DEFAULT_SCRATCH})")
    args = parser.parse_args()

    prod, scratch = resolve_root(), args.dir.resolve()        # read before AURIS_DATA_DIR is pointed at the scratch folder
    info = ensure_scratch(prod, scratch, refresh=args.refresh)

    os.environ["AURIS_DATA_DIR"] = str(scratch)      # before anything from core/ or app is imported
    sys.path.insert(0, str(APP_DIR))
    import app
    check_runtime_paths(scratch)

    if not args.real_engine:
        from core.null_engine import NullEngine
        app.tts = NullEngine(app.tts.engine_name, app.tts.cache_tag, state="loading",   # same cache tag: copied audio stays current
                             message="dev server: TTS disabled (--real-engine to enable)")

    how = ", ".join(f"{k}: {v}" for k, v in info["modes"].items()) or "nothing to copy"
    print(f"\n  Auris dev server  ->  http://{args.host}:{args.port}")
    print(f"  Scratch data : {scratch}")
    print(f"  Copy of your data made {_age(time.time() - info['created_at'])} ago ({how})")
    if info.get("clone_note"):
        print(f"  Note         : plain copy, not clones ({info['clone_note']})")
    print(f"  TTS          : {'real engine' if args.real_engine else 'disabled (nothing is generated)'}")
    print("  Your database, settings, audio, uploads and exports are not touched. --refresh copies them again.\n")
    app.app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
