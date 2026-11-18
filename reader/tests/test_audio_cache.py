"""Which stored audio counts as current, and chapter-render naming/pruning."""

import os


def seg(tmp_path, tag, *, duration=1.0, exists=True):
    path = tmp_path / "clip.wav"
    if exists:
        path.write_bytes(b"x")
    return {"audio_path": str(path), "duration_sec": duration, "engine_tag": tag}


def test_audio_is_current_only_for_the_active_engine_tag(app_module, tmp_path):
    current = app_module.tts.cache_tag
    assert app_module._segment_audio_current(seg(tmp_path, current))
    assert not app_module._segment_audio_current(seg(tmp_path, "kokoro:1"))      # other engine/version
    assert not app_module._segment_audio_current(seg(tmp_path, None))            # legacy, untagged


def test_audio_is_not_current_when_file_or_duration_is_missing(app_module, tmp_path):
    current = app_module.tts.cache_tag
    assert not app_module._segment_audio_current(seg(tmp_path, current, exists=False))
    assert not app_module._segment_audio_current(seg(tmp_path, current, duration=None))
    assert not app_module._segment_audio_current({"audio_path": None, "engine_tag": current})


def test_render_filenames_carry_the_engine_tag(app_module):
    path = app_module._chapter_render_m4a(3, 7)
    assert os.path.basename(path) == "b3_c7_fake-1.m4a"


def test_prune_removes_renders_from_other_engines_only(app_module):
    d = app_module._CHAPTER_RENDER_DIR
    keep = app_module._chapter_render_m4a(1, 1)
    stale = [os.path.join(d, n) for n in ("b1_c1_kokoro-1.m4a", "b1_c1.m4a", "b2_c9_omnivoice-1.fmp4")]
    for path in [keep, *stale]:
        open(path, "w").close()

    app_module._prune_stale_renders()
    assert os.listdir(d) == [os.path.basename(keep)]


def test_clear_chapter_renders_scopes_by_chapter_and_book(app_module):
    mk = lambda b, c, ext="m4a": open(app_module._chapter_render_path(b, c, ext), "w").close()
    for b, c in [(1, 1), (1, 2), (2, 1)]:
        mk(b, c)

    app_module._clear_chapter_renders(1, 1)
    assert sorted(os.listdir(app_module._CHAPTER_RENDER_DIR)) == ["b1_c2_fake-1.m4a", "b2_c1_fake-1.m4a"]
    app_module._clear_chapter_renders(1)
    assert os.listdir(app_module._CHAPTER_RENDER_DIR) == ["b2_c1_fake-1.m4a"]
    app_module._clear_chapter_renders()
    assert os.listdir(app_module._CHAPTER_RENDER_DIR) == []


def test_asset_urls_are_versioned_by_mtime(app_module):
    url = app_module.asset("css/style.css")
    assert url.startswith("/static/css/style.css?v=") and not url.endswith("?v=0")
    assert app_module.asset("css/missing.css").endswith("?v=0")
