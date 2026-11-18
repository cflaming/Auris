"""AURIS_DATA_DIR relocates every place the app keeps data; unset, nothing moves."""

import json
from pathlib import Path

from core.layout import APP_DIR
from tests.helpers import run_in_app

SCRIPT = """
import json
import app
import core.base_engine as b, core.database as d, core.exporter as e, core.settings as s
print(json.dumps({
    "database": d.get_db_path(), "settings": str(s.SETTINGS_FILE), "audio_cache": b.AUDIO_CACHE_DIR,
    "uploads": app.UPLOAD_DIR, "exports": e.EXPORTS_DIR, "chapter_renders": app._CHAPTER_RENDER_DIR,
}))
"""


def resolved_paths(data_dir=None):
    """Where a fresh Python process (which reads the variable at import) puts everything."""
    out = run_in_app(SCRIPT, data_dir)
    assert out.returncode == 0, out.stderr
    return {k: Path(v).resolve() for k, v in json.loads(out.stdout.splitlines()[-1]).items()}


def test_with_the_variable_set_every_data_path_is_under_it(tmp_path):
    found = resolved_paths(tmp_path)
    assert set(found) == {"database", "settings", "audio_cache", "uploads", "exports", "chapter_renders"}
    outside = {name: str(path) for name, path in found.items() if tmp_path.resolve() not in path.parents}
    assert outside == {}                                        # nothing left pointing at the real folders


def test_without_it_everything_is_where_it_always_was():
    found = resolved_paths()
    assert found["database"] == APP_DIR / "data" / "reader.db"
    assert found["settings"] == APP_DIR / "data" / "settings.json"
    assert found["audio_cache"] == APP_DIR / "audio_cache"
    assert found["uploads"] == APP_DIR / "uploads"
    assert found["exports"] == APP_DIR / "exports"
    assert found["chapter_renders"] == APP_DIR / "audio_cache" / "chapter_renders"
