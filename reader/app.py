"""
Offline Ebook Reader — Flask application.
"""

import base64
import email.utils
import json
import logging
import os
import re
import threading
import time
import uuid
import wave
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from itertools import groupby

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    make_response,
    render_template,
    request,
    send_file,
)
from werkzeug.utils import secure_filename

from core import characters as char_module
from core import enrichment, exporter, paths, structure
from core import settings as app_settings
from core.base_engine import SAMPLE_RATE
from core.database import get_conn, init_db
from core.engine_manager import EngineManager
from core.enrichment import ENRICHMENT_VERSION, OTHER_SPEAKER
from core.parser import epub_parser, pdf_parser, txt_parser

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(name)s: %(message)s')
log = logging.getLogger(__name__)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500 MB


@app.template_global()
def asset(path: str) -> str:
    """URL for a static file, versioned by mtime so browsers never serve a stale copy."""
    try:
        version = int(os.path.getmtime(os.path.join(app.static_folder, path)))
    except OSError:
        version = 0
    return f'/static/{path}?v={version}'

UPLOAD_DIR = str(paths.UPLOAD_DIR)
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Pre-rendered chapter audio (full-chapter fMP4 for smooth mobile playback)
from core.base_engine import AUDIO_CACHE_DIR as _ACD

_CHAPTER_RENDER_DIR = os.path.join(_ACD, 'chapter_renders')
os.makedirs(_CHAPTER_RENDER_DIR, exist_ok=True)


def _render_tag() -> str:
    return tts.cache_tag.replace(':', '-')


def _chapter_render_path(book_id: int, chapter_id: int, ext: str) -> str:
    # The engine tag is part of the name, so renders made by another engine (or an
    # older output version) are simply never found and get re-rendered.
    return os.path.join(_CHAPTER_RENDER_DIR, f'b{book_id}_c{chapter_id}_{_render_tag()}.{ext}')


def _chapter_render_fmp4(book_id: int, chapter_id: int) -> str:
    return _chapter_render_path(book_id, chapter_id, 'fmp4')


def _chapter_render_meta(book_id: int, chapter_id: int) -> str:
    return _chapter_render_path(book_id, chapter_id, 'json')


def _chapter_render_m4a(book_id: int, chapter_id: int) -> str:
    return _chapter_render_path(book_id, chapter_id, 'm4a')


def _clear_chapter_renders(book_id: int | None = None, chapter_id: int | None = None):
    """Delete cached chapter renders for a chapter, a book, or (book_id=None) everything."""
    import glob
    book = '*' if book_id is None else book_id
    chapter = '*' if chapter_id is None else chapter_id
    for ext in ('fmp4', 'm4a', 'json'):
        for p in glob.glob(os.path.join(_CHAPTER_RENDER_DIR, f'b{book}_c{chapter}_*.{ext}')):
            try: os.remove(p)
            except OSError: pass


tts = EngineManager()

DEFAULT_NARRATOR_INSTRUCT = app_settings.DEFAULT_NARRATOR_INSTRUCT

_export_jobs: dict = {}
_chapter_prerender_jobs: dict = {}  # (book_id, chapter_id) -> job_id for in-progress renders

import queue as _queue

_prerender_queue: _queue.Queue = _queue.Queue()

def _prerender_worker():
    while True:
        job_id, book_id, chapter_id = _prerender_queue.get()
        try:
            _run_chapter_prerender(job_id, book_id, chapter_id)
        finally:
            _prerender_queue.task_done()

threading.Thread(target=_prerender_worker, daemon=True, name='prerender-worker').start()

# ── Seg-mode TTS activity tracker ────────────────────────────────────────────
from collections import deque as _deque

_tts_active: dict = {}
_tts_recent = _deque(maxlen=60)
_tts_lock = threading.Lock()
_tts_req_id = 0
_chapter_order_cache: dict[int, int] = {}  # chapter_id → order_num within book


def _chapter_order_num(chapter_id: int) -> int:
    if chapter_id not in _chapter_order_cache:
        with get_conn() as conn:
            row = conn.execute('SELECT order_num FROM chapters WHERE id=?', (chapter_id,)).fetchone()
        _chapter_order_cache[chapter_id] = row['order_num'] if row else chapter_id
    return _chapter_order_cache[chapter_id]


def _tts_start(book_id, chapter_id, segment_index, character):
    global _tts_req_id
    with _tts_lock:
        _tts_req_id += 1
        rid = _tts_req_id
        _tts_active[rid] = {
            'book_id': book_id, 'chapter_order': _chapter_order_num(chapter_id),
            'segment_index': segment_index, 'character': character or 'Narrator',
            'started_at': time.time(),
        }
    return rid


def _tts_finish(rid, *, cached=False, duration_sec=None, error=None):
    with _tts_lock:
        entry = _tts_active.pop(rid, None)
        if entry is None:
            return
        elapsed = time.time() - entry['started_at']
        _tts_recent.append({**entry, 'elapsed': round(elapsed, 2),
                             'cached': cached, 'duration_sec': duration_sec,
                             'error': error})


# Per-chapter locks prevent concurrent segment building from racing on the
# DELETE + INSERT in _store_segments when multiple requests hit the same
# chapter before segments are built (e.g. parallel prewarm requests).
_chapter_build_locks: dict = {}
_chapter_build_locks_meta = threading.Lock()
_startup_lock = threading.Lock()
_startup_complete = False


def _get_chapter_build_lock(book_id: int, chapter_id: int) -> threading.Lock:
    key = (book_id, chapter_id)
    with _chapter_build_locks_meta:
        if key not in _chapter_build_locks:
            _chapter_build_locks[key] = threading.Lock()
        return _chapter_build_locks[key]


VOICE_PREVIEW_TEXT = (
    'Hello. This is a voice preview sample. The afternoon is calm, the room is quiet, '
    'and every word should sound clear, steady, and natural.'
)


# ════════════════════════════════════════════════════════════════════════════
# Startup
# ════════════════════════════════════════════════════════════════════════════

@app.before_request
def _startup():
    global _startup_complete

    if _startup_complete:
        return

    with _startup_lock:
        if _startup_complete:
            return
        try:
            init_db()
            _backfill_character_frequencies()
            _backfill_correction_context()
            _prune_stale_renders()
        except Exception:
            tts.load_async()
            raise
        tts.load_async()
        _startup_complete = True


def _prune_stale_renders():
    """Delete chapter renders made by another engine or an older output version."""
    keep = f'_{_render_tag()}.'
    for name in os.listdir(_CHAPTER_RENDER_DIR):
        if keep not in name:
            try: os.remove(os.path.join(_CHAPTER_RENDER_DIR, name))
            except OSError: pass


def _audio_url(cache_key: str) -> str:
    return f'/api/audio/{cache_key}'


def _record_segment_audio(conn, seg_id, audio_path, duration_sec, cache_key, generation_sec):
    """Store the audio made for a segment (by the engine now running) on its row."""
    conn.execute(
        'UPDATE tts_segments SET audio_path=?, duration_sec=?, cache_key=?, generation_sec=?, engine_tag=? WHERE id=?',
        (audio_path, duration_sec, cache_key, generation_sec, tts.cache_tag, seg_id)
    )


def _segment_audio_current(seg: dict) -> bool:
    """True if the segment's stored audio exists and was made by the active engine/version."""
    return bool(seg.get('audio_path') and seg.get('duration_sec')
                and seg.get('engine_tag') == tts.cache_tag
                and os.path.exists(seg['audio_path']))


def _default_narrator_instruct() -> str:
    return app_settings.get('narrator_instruct', DEFAULT_NARRATOR_INSTRUCT)


def _book_narrator_instruct(book: dict | None) -> str:
    if not book:
        return _default_narrator_instruct()
    return book.get('narrator_instruct') or _default_narrator_instruct()


def _book_expression_tags(book: dict | None) -> bool:
    if not book:
        return True
    v = book.get('expression_tags')
    return v is None or bool(v)


def _book_single_narrator_mode(book: dict | None) -> bool:
    if not book:
        return False
    return bool(book.get('single_narrator_mode'))


def _book_narrator_ref_audio(book_id: int) -> str | None:
    try:
        with get_conn() as conn:
            row = conn.execute(
                'SELECT narrator_ref_audio_path FROM books WHERE id=?', (book_id,)
            ).fetchone()
    except Exception as exc:
        log.warning('Unable to load narrator reference audio for book %s: %s', book_id, exc)
        return None

    if not row:
        return None

    path = dict(row).get('narrator_ref_audio_path')
    if not isinstance(path, str) or not path.strip():
        return None

    resolved = os.path.abspath(path)
    return resolved if os.path.exists(resolved) else None


def _delete_file_if_exists(path: str | None):
    if not isinstance(path, str) or not path.strip():
        return
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError as exc:
        log.warning('Unable to delete file %s: %s', path, exc)


def _load_book(book_id: int):
    with get_conn() as conn:
        return conn.execute('SELECT * FROM books WHERE id=?', (book_id,)).fetchone()


def _clear_book_tts_segments(book_id: int):
    with get_conn() as conn:
        conn.execute('DELETE FROM tts_segments WHERE book_id=?', (book_id,))
    _clear_chapter_renders(book_id)


def _character_map(rows) -> dict:
    """name -> character dict; merged characters carry 'canonical', the name
    of the character whose voice they use."""
    by_id = {r['id']: dict(r) for r in rows}
    char_map = {}
    for c in by_id.values():
        target = by_id.get(c.get('alias_of'))
        if target and target['id'] != c['id']:
            c['canonical'] = target['name']
        char_map[c['name']] = c
    return char_map


def _compute_segments_for_chapter(book_id: int, chapter_id: int, extra_characters=(),
                                  apply_corrections: bool = True) -> list[dict]:
    """Segments for a chapter. extra_characters are hypothetical characters (dicts) added to
    the book's own, used to preview what adding one would change. apply_corrections=False
    gives what detection alone says, ignoring the user's speaker corrections."""
    with get_conn() as conn:
        ch = conn.execute(
            'SELECT * FROM chapters WHERE id=? AND book_id=?',
            (chapter_id, book_id)
        ).fetchone()
        chars = conn.execute(
            'SELECT * FROM characters WHERE book_id=?',
            (book_id,)
        ).fetchall()
        book = conn.execute(
            'SELECT narrator_instruct, single_narrator_mode, expression_tags FROM books WHERE id=?',
            (book_id,)
        ).fetchone()
        overrides = {
            r['anchor']: r['character_name'] for r in conn.execute(
                'SELECT anchor, character_name FROM segment_speakers WHERE book_id=? AND chapter_id=?',
                (book_id, chapter_id)
            )
        } if apply_corrections else {}

    if not ch:
        return []

    char_map = _character_map([*chars, *extra_characters])
    book_d = dict(book) if book else None
    segs = enrichment.enrich_chapter(
        ch['content'],
        char_map,
        _book_narrator_instruct(book_d),
        single_narrator_mode=_book_single_narrator_mode(book_d),
        chapter_title=ch['title'],
        expression_tags=_book_expression_tags(book_d),
        speaker_overrides=overrides,
    )
    return segs


def _build_segments_for_chapter(book_id: int, chapter_id: int) -> list[dict]:
    segs = _compute_segments_for_chapter(book_id, chapter_id)
    if not segs:
        return []
    _store_segments(book_id, chapter_id, segs)
    return segs


def _ensure_chapter_segments(book_id: int, chapter_id: int):
    """Return stored segment rows, rebuilding if the enrichment version is stale."""
    with get_conn() as conn:
        ch = conn.execute(
            'SELECT enrichment_version FROM chapters WHERE id=? AND book_id=?',
            (chapter_id, book_id)
        ).fetchone()
        if ch and ch['enrichment_version'] == ENRICHMENT_VERSION:
            rows = conn.execute(
                'SELECT * FROM tts_segments WHERE book_id=? AND chapter_id=? ORDER BY segment_index',
                (book_id, chapter_id)
            ).fetchall()
            if rows:
                return rows

    if not _build_segments_for_chapter(book_id, chapter_id):
        return []
    with get_conn() as conn:
        return conn.execute(
            'SELECT * FROM tts_segments WHERE book_id=? AND chapter_id=? ORDER BY segment_index',
            (book_id, chapter_id)
        ).fetchall()


# ════════════════════════════════════════════════════════════════════════════
# Page routes
# ════════════════════════════════════════════════════════════════════════════

@app.route('/')
def library_page():
    return render_template('library.html')


@app.route('/debug/jobs')
def debug_jobs_page():
    return render_template('debug_jobs.html')


@app.route('/debug/cache')
def debug_cache_page():
    return render_template('debug_cache.html')


@app.route('/debug/chapter-cache')
def debug_chapter_cache():
    book_id   = request.args.get('book_id',   type=int)
    chapter_id = request.args.get('chapter_id', type=int)
    with get_conn() as conn:
        books = [dict(r) for r in conn.execute(
            'SELECT id, title FROM books ORDER BY title').fetchall()]
        chapters = []
        segs     = []
        if book_id:
            chapters = [dict(r) for r in conn.execute(
                'SELECT id, title FROM chapters WHERE book_id=? ORDER BY order_num',
                (book_id,)).fetchall()]
        if book_id and chapter_id:
            segs = [dict(r) for r in conn.execute(
                '''SELECT segment_index, character_name, text,
                          audio_path, duration_sec, cache_key, generation_sec
                   FROM tts_segments
                   WHERE book_id=? AND chapter_id=?
                   ORDER BY segment_index''',
                (book_id, chapter_id)).fetchall()]

    # annotate with actual file existence
    for s in segs:
        s['file_ok'] = bool(s.get('audio_path') and os.path.exists(s['audio_path']))

    # also check if a prerender exists
    prerender_cached = False
    prerender_info   = {}
    if book_id and chapter_id:
        fmp4   = _chapter_render_fmp4(book_id, chapter_id)
        meta_p = _chapter_render_meta(book_id, chapter_id)
        if os.path.exists(fmp4) and os.path.exists(meta_p):
            prerender_cached = True
            try:
                with open(meta_p) as f:
                    m = json.load(f)
                prerender_info = {
                    'timeline_len': len(m.get('timeline', [])),
                    'total_segments': m.get('total_segments'),
                    'fmp4_size_mb': round(os.path.getsize(fmp4) / 1_048_576, 2),
                }
            except Exception:
                pass

    return jsonify({
        'books': books,
        'chapters': chapters,
        'segments': segs,
        'prerender_cached': prerender_cached,
        'prerender_info': prerender_info,
    })


@app.route('/debug/orphans')
def debug_orphans_page():
    return render_template('debug_orphans.html')


def _scan_orphans() -> dict:
    import re

    from core.base_engine import AUDIO_CACHE_DIR

    # --- known cache keys from DB ---
    with get_conn() as conn:
        known_keys = {
            row[0]
            for row in conn.execute('SELECT cache_key FROM tts_segments WHERE cache_key IS NOT NULL').fetchall()
        }
        known_book_ids = {
            row[0]
            for row in conn.execute('SELECT id FROM books').fetchall()
        }
        known_chapter_ids = {
            row[0]
            for row in conn.execute('SELECT id FROM chapters').fetchall()
        }
        dead_segs = [
            dict(r) for r in conn.execute(
                '''SELECT book_id, chapter_id, segment_index, audio_path
                   FROM tts_segments
                   WHERE audio_path IS NOT NULL'''
            ).fetchall()
            if not os.path.exists(r['audio_path'])
        ]

    # --- orphan WAVs: on disk but no matching cache_key in DB ---
    orphan_wavs = []
    for fname in os.listdir(AUDIO_CACHE_DIR):
        if not fname.endswith('.wav'):
            continue
        key = fname[:-4]
        if key not in known_keys:
            fpath = os.path.join(AUDIO_CACHE_DIR, fname)
            try:
                size = os.path.getsize(fpath)
            except OSError:
                size = 0
            orphan_wavs.append({'filename': fname, 'path': fpath, 'size_bytes': size})

    # --- orphan chapter renders: b{book}_c{chapter}.* with unknown ids ---
    render_pattern = re.compile(r'^b(\d+)_c(\d+)[._]')
    orphan_renders = []
    for fname in os.listdir(_CHAPTER_RENDER_DIR):
        m = render_pattern.match(fname)
        if not m:
            continue
        bid, cid = int(m.group(1)), int(m.group(2))
        reason = None
        if bid not in known_book_ids:
            reason = f'book {bid} not in DB'
        elif cid not in known_chapter_ids:
            reason = f'chapter {cid} not in DB'
        if reason:
            fpath = os.path.join(_CHAPTER_RENDER_DIR, fname)
            try:
                size = os.path.getsize(fpath)
            except OSError:
                size = 0
            key = f'b{bid}_c{cid}'
            orphan_renders.append({'filename': fname, 'path': fpath, 'size_bytes': size, 'reason': reason, '_key': key})

    # deduplicate by (book,chapter) for display — keep all files but show each row
    total_orphan_bytes = (
        sum(r['size_bytes'] for r in orphan_wavs) +
        sum(r['size_bytes'] for r in orphan_renders)
    )

    return {
        'orphan_wavs': orphan_wavs,
        'orphan_renders': orphan_renders,
        'dead_segments': dead_segs,
        'total_orphan_bytes': total_orphan_bytes,
    }


@app.route('/debug/orphans/data')
def debug_orphans_data():
    return jsonify(_scan_orphans())


@app.route('/debug/orphans/cleanup', methods=['POST'])
def debug_orphans_cleanup():
    data = _scan_orphans()
    deleted = 0
    freed = 0
    for r in data['orphan_wavs'] + data['orphan_renders']:
        try:
            size = r['size_bytes']
            os.remove(r['path'])
            deleted += 1
            freed += size
        except OSError:
            pass
    return jsonify({'deleted_files': deleted, 'freed_bytes': freed})


@app.route('/debug/jobs/data')
def debug_jobs_data():
    prerender = []
    for (bid, cid), job_id in list(_chapter_prerender_jobs.items()):
        job = _export_jobs.get(job_id, {})
        label = job.get('chapter_label') or f'book {bid} / chapter {cid}'
        prerender.append({**job, 'id': job_id, 'chapter_label': label})
    seen_ids = {j['id'] for j in prerender}
    exports = []
    for job_id, job in list(_export_jobs.items()):
        if job_id in seen_ids:
            continue
        exports.append({**job, 'id': job_id})
    prerender.sort(key=lambda j: j['id'], reverse=True)
    exports.sort(key=lambda j: j['id'], reverse=True)
    with _tts_lock:
        active = list(_tts_active.values())
        recent = list(reversed(_tts_recent))
    now = time.time()
    for a in active:
        a['elapsed'] = round(now - a['started_at'], 1)
    return jsonify({
        'prerender_jobs': prerender[:30],
        'export_jobs': exports[:30],
        'tts_active': active,
        'tts_recent': recent,
    })


@app.route('/reader/<int:book_id>')
def reader_page(book_id):
    book = _load_book(book_id)
    if not book:
        return 'Book not found', 404
    book_data = dict(book)
    book_data['narrator_instruct'] = _book_narrator_instruct(book_data)
    book_data['single_narrator_mode'] = _book_single_narrator_mode(book_data)
    return render_template('reader.html', book=book_data)


@app.route('/voice-studio/<int:book_id>')
def voice_studio_page(book_id):
    book = _load_book(book_id)
    if not book:
        return 'Book not found', 404
    book_data = dict(book)
    book_data['narrator_instruct'] = _book_narrator_instruct(book_data)
    book_data['single_narrator_mode'] = _book_single_narrator_mode(book_data)
    book_data['expression_tags'] = _book_expression_tags(book_data)
    with get_conn() as conn:
        corrections = conn.execute('SELECT COUNT(*) FROM segment_speakers WHERE book_id=?', (book_id,)).fetchone()[0]
    return render_template('voice_studio.html', book=book_data, corrections=corrections)


# ════════════════════════════════════════════════════════════════════════════
# Book import
# ════════════════════════════════════════════════════════════════════════════

@app.route('/api/books/import', methods=['POST'])
def import_book():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    f = request.files['file']
    if not f.filename:
        return jsonify({'error': 'Empty filename'}), 400

    ext = f.filename.rsplit('.', 1)[-1].lower()
    if ext not in ('epub', 'pdf', 'txt'):
        return jsonify({'error': f'Unsupported format: {ext}'}), 400

    safe_name = secure_filename(f.filename) or f'book.{ext}'
    dest = os.path.join(UPLOAD_DIR, f'{uuid.uuid4().hex}_{safe_name}')
    f.save(dest)

    try:
        if ext == 'epub':
            data = epub_parser.parse(dest)
        elif ext == 'pdf':
            data = pdf_parser.parse(dest)
        else:
            data = txt_parser.parse(dest)
    except Exception as e:
        return jsonify({'error': f'Parse error: {e}'}), 500

    chapters = structure.enrich_chapters(data['chapters'])

    with get_conn() as conn:
        cur = conn.execute(
            'INSERT INTO books (title, author, file_path, file_type, cover_b64, language, '
            'single_narrator_mode, total_chapters) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (data['title'], data['author'], dest, ext,
             data.get('cover_b64'), data.get('language', 'en'),
             int(bool(app_settings.get('single_narrator_mode', False))), len(chapters))
        )
        book_id = cur.lastrowid

        for ch in chapters:
            conn.execute(
                'INSERT INTO chapters (book_id, title, order_num, section_type, content, word_count) '
                'VALUES (?,?,?,?,?,?)',
                (book_id, ch['title'], ch['order_num'], ch.get('section_type', 'chapter'),
                 ch['content'], ch['word_count'])
            )

    # Detect characters in background
    threading.Thread(target=_detect_characters, args=(book_id, data), daemon=True).start()

    return jsonify({'book_id': book_id, 'title': data['title'], 'chapters': len(chapters)})


def _detect_characters(book_id: int, data: dict):
    full_text = ' '.join(ch['content'] for ch in data['chapters'])
    chars = char_module.extract_characters(full_text, top_n=20)
    with get_conn() as conn:
        for ch in chars:
            try:
                conn.execute(
                    'INSERT OR IGNORE INTO characters '
                    '(book_id, name, gender, frequency, instruct, color_hex) '
                    'VALUES (?,?,?,?,?,?)',
                    (book_id, ch['name'], ch['gender'], ch['frequency'],
                     ch['instruct'], ch['color_hex'])
                )
            except Exception:
                pass


# ════════════════════════════════════════════════════════════════════════════
# Library API
# ════════════════════════════════════════════════════════════════════════════

@app.route('/api/books')
def list_books():
    with get_conn() as conn:
        rows = conn.execute(
            'SELECT b.id, b.title, b.author, b.file_type, b.cover_b64, b.added_at, '
            'b.last_read, b.total_chapters, rp.chapter_id AS progress_chapter_id, '
            'rp.position AS progress_position, c.title AS progress_chapter_title '
            'FROM books b '
            'LEFT JOIN reading_progress rp ON rp.book_id = b.id '
            'LEFT JOIN chapters c ON c.id = rp.chapter_id '
            'ORDER BY COALESCE(b.last_read, b.added_at) DESC, b.added_at DESC'
        ).fetchall()
    books = []
    for r in rows:
        d = dict(r)
        if d['cover_b64']:
            d['cover_url'] = f'/api/books/{d["id"]}/cover'
            d.pop('cover_b64')
        else:
            d['cover_url'] = None
        books.append(d)
    return jsonify(books)


@app.route('/api/books/<int:book_id>/cover')
def book_cover(book_id):
    with get_conn() as conn:
        row = conn.execute('SELECT cover_b64, file_type FROM books WHERE id=?', (book_id,)).fetchone()
    if not row or not row['cover_b64']:
        return '', 204
    img_bytes = base64.b64decode(row['cover_b64'])
    ext = 'png' if row['file_type'] == 'pdf' else 'jpeg'
    return app.response_class(img_bytes, mimetype=f'image/{ext}')


@app.route('/api/books/<int:book_id>', methods=['DELETE'])
def delete_book(book_id):
    with get_conn() as conn:
        book = conn.execute(
            'SELECT file_path, narrator_ref_audio_path FROM books WHERE id=?', (book_id,)
        ).fetchone()
        char_refs = conn.execute(
            'SELECT ref_audio_path FROM characters WHERE book_id=? AND ref_audio_path IS NOT NULL',
            (book_id,)
        ).fetchall()
        conn.execute('DELETE FROM books WHERE id=?', (book_id,))

    if book:
        _delete_file_if_exists(book['file_path'])
        _delete_file_if_exists(book['narrator_ref_audio_path'])
    for row in char_refs:
        _delete_file_if_exists(row['ref_audio_path'])

    return jsonify({'ok': True})


# ════════════════════════════════════════════════════════════════════════════
# Chapter API
# ════════════════════════════════════════════════════════════════════════════

@app.route('/api/books/<int:book_id>/chapters')
def list_chapters(book_id):
    with get_conn() as conn:
        rows = conn.execute(
            'SELECT id, title, order_num, section_type, word_count FROM chapters '
            'WHERE book_id=? ORDER BY order_num',
            (book_id,)
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/books/<int:book_id>/chapters/<int:chapter_id>')
def get_chapter(book_id, chapter_id):
    with get_conn() as conn:
        row = conn.execute(
            'SELECT * FROM chapters WHERE id=? AND book_id=?', (chapter_id, book_id)
        ).fetchone()
    if not row:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(dict(row))


@app.route('/api/books/<int:book_id>/progress', methods=['POST'])
def save_progress(book_id):
    body = request.get_json(force=True) or {}
    with get_conn() as conn:
        conn.execute(
            'INSERT INTO reading_progress (book_id, chapter_id, position, updated_at) '
            'VALUES (?,?,?,datetime("now")) '
            'ON CONFLICT(book_id) DO UPDATE SET chapter_id=excluded.chapter_id, '
            'position=excluded.position, updated_at=excluded.updated_at',
            (book_id, body.get('chapter_id'), body.get('position', 0))
        )
        conn.execute('UPDATE books SET last_read=datetime("now") WHERE id=?', (book_id,))
    return jsonify({'ok': True})


@app.route('/api/books/<int:book_id>/progress')
def get_progress(book_id):
    with get_conn() as conn:
        row = conn.execute(
            'SELECT * FROM reading_progress WHERE book_id=?', (book_id,)
        ).fetchone()
    return jsonify(dict(row) if row else {})


# ════════════════════════════════════════════════════════════════════════════
# Characters API
# ════════════════════════════════════════════════════════════════════════════

def _ordered_characters(rows) -> list[dict]:
    """Characters in display order. A character merged into another stays right behind the one it is
    merged into, and the groups (a character plus whoever is merged into them) are ordered by their
    combined frequency, so a group moves up as a whole once its total passes another group's.
    Each entry also gets `group_total`: the combined frequency of its group."""
    chars = [dict(r) for r in rows]
    by_id = {c['id']: c for c in chars}

    def head_id(c):
        target = by_id.get(c['alias_of'])
        # Merges are one level deep; a missing or itself-merged target leaves the character on its own.
        return target['id'] if target and target['alias_of'] is None and target['id'] != c['id'] else c['id']

    groups = defaultdict(list)
    for c in chars:
        groups[head_id(c)].append(c)
    ordered = []
    for head, members in groups.items():
        members.sort(key=lambda c: (c['id'] != head, -c['frequency'], c['name'].lower()))   # the head first
        total = sum(c['frequency'] for c in members)
        for c in members:
            c['group_total'] = total
        ordered.append(members)
    ordered.sort(key=lambda m: (-m[0]['group_total'], -m[0]['frequency'], m[0]['name'].lower()))
    return [c for members in ordered for c in members]


@app.route('/api/books/<int:book_id>/characters')
def list_characters(book_id):
    with get_conn() as conn:
        rows = conn.execute('SELECT * FROM characters WHERE book_id=?', (book_id,)).fetchall()
        by_hand = _hand_assignments(conn, book_id)
    merge_targets = {r['alias_of'] for r in rows if r['alias_of'] is not None}      # characters others are merged into
    chars = _ordered_characters(rows)
    for c in chars:
        last, assigned = by_hand.get(c['name'], (None, 0))
        c['last_override_at'] = last                    # when a sentence was last assigned to them by hand
        c['deletable'] = _delete_blocker(c, assigned, c['id'] in merge_targets) is None
    return jsonify(chars)


def _hand_assignments(conn, book_id):
    """{character name: (when a sentence was last assigned to them by hand, how many)}."""
    return {r[0]: (r[1], r[2]) for r in conn.execute(
        'SELECT character_name, MAX(updated_at), COUNT(*) FROM segment_speakers '
        'WHERE book_id=? AND character_name IS NOT NULL GROUP BY character_name', (book_id,))}


def _delete_blocker(char, assigned_by_hand: int, has_merged: bool):
    """Why a character can't be deleted, or None if it can. Only hand-added characters can go, and only
    while nothing was assigned to them by hand and no one is merged into them: those would be lost."""
    if not char['manual']:
        return 'Only characters you added by hand can be deleted.'
    if assigned_by_hand:
        return f'{assigned_by_hand} sentence(s) were assigned to them by hand: reset those first.'
    if has_merged:
        return 'Other characters are merged into them: unmerge those first.'
    return None


@app.route('/api/books/<int:book_id>/characters/<int:char_id>', methods=['DELETE'])
def delete_character(book_id, char_id):
    with get_conn() as conn:
        char = conn.execute('SELECT * FROM characters WHERE id=? AND book_id=?', (char_id, book_id)).fetchone()
        if not char:
            return jsonify({'error': 'Character not found'}), 404
        assigned = conn.execute('SELECT COUNT(*) FROM segment_speakers WHERE book_id=? AND character_name=?',
                                (book_id, char['name'])).fetchone()[0]
        has_merged = conn.execute(
            'SELECT 1 FROM characters WHERE book_id=? AND alias_of=?', (book_id, char_id)).fetchone()
        blocker = _delete_blocker(char, assigned, bool(has_merged))
        if blocker:
            return jsonify({'error': blocker}), 409
        conn.execute('DELETE FROM characters WHERE id=?', (char_id,))
    # Their name no longer takes part in automatic attribution, so rebuild segments.
    _clear_book_tts_segments(book_id)
    return jsonify({'deleted': char['name']})


_VALID_GENDERS = {'male', 'female', 'unknown'}


def _count_name_mentions(conn, book_id: int, name: str) -> int:
    """How often a name appears in the book text (the same 'frequency' detected characters carry)."""
    pattern = re.compile(rf'\b{re.escape(name)}\b')
    return sum(len(pattern.findall(r['content'] or ''))
               for r in conn.execute('SELECT content FROM chapters WHERE book_id=?', (book_id,)))


def _backfill_character_frequencies():
    """Characters added by hand used to be stored with frequency 0; count their mentions."""
    with get_conn() as conn:
        for r in conn.execute('SELECT id, book_id, name FROM characters WHERE frequency=0').fetchall():
            conn.execute('UPDATE characters SET frequency=? WHERE id=?',
                         (_count_name_mentions(conn, r['book_id'], r['name']), r['id']))


def _reject(message: str, status: int):
    abort(make_response(jsonify({'error': message}), status))


def _parse_new_character(book_id: int, body: dict) -> tuple[str, str]:
    """Validate a new character request -> (name, gender), or abort with a 4xx."""
    name = re.sub(r'\s+', ' ', str(body.get('name') or '')).strip()
    gender = body.get('gender') or 'unknown'
    if not name or len(name) > 60:
        _reject('Name is required (max 60 characters)', 400)
    if gender not in _VALID_GENDERS:
        _reject('gender must be male, female or unknown', 400)
    if name.lower() in ('narrator', OTHER_SPEAKER.lower()):
        _reject(f'"{name}" is a reserved name', 400)
    with get_conn() as conn:
        if not conn.execute('SELECT 1 FROM books WHERE id=?', (book_id,)).fetchone():
            _reject('Book not found', 404)
        if conn.execute('SELECT 1 FROM characters WHERE book_id=? AND name=?', (book_id, name)).fetchone():
            _reject(f'{name} already exists in this book', 409)
    return name, gender


def _new_character(name: str, gender: str) -> dict:
    """The character row a new name would get (voice and colour assigned like a detected one)."""
    profile = char_module.generate_voice_profile(name, gender)
    return {'id': -1, 'name': name, 'gender': gender, 'frequency': 0, 'alias_of': None,
            'instruct': profile['instruct'], 'color_hex': profile['color_hex']}


_MAX_IMPACT_CHANGES = 400   # sentences listed individually in an impact preview


def _character_impact(book_id: int, name: str, gender: str) -> dict:
    """Who-speaks-what differences if a character called `name` were added to the book."""
    hypothetical = _new_character(name, gender)
    with get_conn() as conn:
        chapters = conn.execute(
            'SELECT id, title, content FROM chapters WHERE book_id=? ORDER BY order_num', (book_id,)
        ).fetchall()
        existing = conn.execute('SELECT * FROM characters WHERE book_id=?', (book_id,)).fetchall()
        # A correction naming a character that no longer exists is ignored, but goes live
        # again if that name is added back, even in a chapter that never mentions it.
        has_stale_correction = {r[0] for r in conn.execute(
            'SELECT DISTINCT chapter_id FROM segment_speakers WHERE book_id=? AND character_name=?',
            (book_id, name))}

    # Detection only depends on the character set through name matches, so a chapter that
    # contains none of the names affected by the new character cannot change: skip it.
    touched = enrichment.changed_name_pattern(
        _character_map(existing), _character_map([*existing, hypothetical]))

    changes, per_chapter = [], []
    for ch in chapters:
        if ch['id'] not in has_stale_correction and not (touched and touched.search(ch['content'] or '')):
            continue
        start = len(changes)
        before = _compute_segments_for_chapter(book_id, ch['id'])
        after = _compute_segments_for_chapter(book_id, ch['id'], [hypothetical])
        for i, (b, a) in enumerate(zip(before, after)):
            if b['character_name'] != a['character_name']:
                changes.append({
                    'chapter_id': ch['id'], 'chapter_title': ch['title'], 'segment_index': i,
                    'text': a['text'], 'prev': before[i - 1]['text'] if i else '',
                    'before': b['character_name'], 'after': a['character_name'],
                })
        if len(changes) > start:
            per_chapter.append({'chapter_id': ch['id'], 'title': ch['title'], 'changed': len(changes) - start})

    transitions = Counter((c['before'], c['after']) for c in changes)
    return {
        'name': name,
        'total': len(changes),
        'gained': sum(1 for c in changes if c['after'] == name),
        'transitions': [{'from': f, 'to': t, 'count': n} for (f, t), n in transitions.most_common()],
        'chapters': per_chapter,
        'changes': changes[:_MAX_IMPACT_CHANGES],
        'truncated': len(changes) > _MAX_IMPACT_CHANGES,
    }


@app.route('/api/books/<int:book_id>/characters/preview', methods=['POST'])
def preview_add_character(book_id):
    """Dry run of add_character: which sentences would change speaker. Changes nothing."""
    name, gender = _parse_new_character(book_id, request.get_json(force=True) or {})
    return jsonify(_character_impact(book_id, name, gender))


@app.route('/api/books/<int:book_id>/characters', methods=['POST'])
def add_character(book_id):
    """Add a character the detector missed. Its voice is assigned like a detected one."""
    name, gender = _parse_new_character(book_id, request.get_json(force=True) or {})
    new = _new_character(name, gender)
    with get_conn() as conn:
        conn.execute(
            'INSERT INTO characters (book_id, name, gender, frequency, instruct, color_hex, manual, added_at) '
            "VALUES (?,?,?,?,?,?,1,datetime('now'))",
            (book_id, name, gender, _count_name_mentions(conn, book_id, name),
             new['instruct'], new['color_hex'])
        )
        row = conn.execute('SELECT * FROM characters WHERE book_id=? AND name=?', (book_id, name)).fetchone()
    # The new name also takes part in automatic attribution, so rebuild segments
    # (manual speaker corrections are stored separately and survive this).
    _clear_book_tts_segments(book_id)
    return jsonify(dict(row)), 201


@app.route('/api/books/<int:book_id>/characters/<int:char_id>', methods=['PUT'])
def update_character(book_id, char_id):
    body = request.get_json(force=True) or {}
    allowed = {'instruct', 'gender', 'color_hex', 'ref_audio_path', 'alias_of'}
    updates = {k: v for k, v in body.items() if k in allowed}
    if 'color_hex' in updates and not re.match(r'^#[0-9a-fA-F]{6}$', str(updates['color_hex'] or '')):
        return jsonify({'error': 'color_hex must be a #RRGGBB string'}), 400
    if not updates:
        return jsonify({'error': 'Nothing to update'}), 400
    voice_fields = {'instruct', 'ref_audio_path', 'alias_of'} & updates.keys()
    set_clause = ', '.join(f'{k}=?' for k in updates)
    with get_conn() as conn:
        if 'alias_of' in updates:
            target_id = updates['alias_of']
            if target_id is not None:
                if not isinstance(target_id, int) or isinstance(target_id, bool) or target_id == char_id:
                    return jsonify({'error': 'alias_of must be another character id or null'}), 400
                target = conn.execute(
                    'SELECT id, alias_of FROM characters WHERE id=? AND book_id=?',
                    (target_id, book_id)
                ).fetchone()
                if not target:
                    return jsonify({'error': 'alias_of character not found in this book'}), 400
                if target['alias_of'] is not None:
                    return jsonify({'error': 'Cannot merge into a character that is itself merged'}), 400
                # Keep merges one level deep: this character's own aliases
                # follow it to the new target.
                conn.execute(
                    'UPDATE characters SET alias_of=? WHERE alias_of=? AND book_id=?',
                    (target_id, char_id, book_id)
                )
        conn.execute(
            f'UPDATE characters SET {set_clause} WHERE id=? AND book_id=?',
            (*updates.values(), char_id, book_id)
        )
    if voice_fields:
        _clear_book_tts_segments(book_id)
    return jsonify({'ok': True, 'segments_cleared': bool(voice_fields)})


@app.route('/api/books/<int:book_id>/characters/<int:char_id>/preview', methods=['POST'])
def preview_character(book_id, char_id):
    body = request.get_json(silent=True) or {}
    with get_conn() as conn:
        row = conn.execute('SELECT * FROM characters WHERE id=? AND book_id=?',
                           (char_id, book_id)).fetchone()
    if not row:
        return jsonify({'error': 'Not found'}), 404

    status = tts.status()
    if status['state'] != 'ready':
        return jsonify({'error': 'Model not ready', 'status': status}), 503

    instruct = (body.get('instruct') or row['instruct'] or '').strip()
    ref_audio = row['ref_audio_path'] if row['ref_audio_path'] else None
    sample_text = (
        f'Hello. I am {row["name"]}. '
        'This preview should sound clear, steady, and easy to understand.'
    )

    try:
        result = tts.generate_preview(
            instruct=instruct,
            sample_text=sample_text,
            ref_audio=ref_audio,
        )
        return jsonify({'audio_url': _audio_url(result['cache_key'])})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/books/<int:book_id>/narrator', methods=['GET'])
def get_narrator(book_id):
    book = _load_book(book_id)
    if not book:
        return jsonify({'error': 'Not found'}), 404
    book_data = dict(book)
    return jsonify({
        'instruct': _book_narrator_instruct(book_data),
        'single_narrator_mode': _book_single_narrator_mode(book_data),
        'expression_tags': _book_expression_tags(book_data),
    })


@app.route('/api/books/<int:book_id>/narrator', methods=['PUT'])
def update_narrator(book_id):
    body = request.get_json(force=True) or {}
    book = _load_book(book_id)
    if not book:
        return jsonify({'error': 'Not found'}), 404
    book_data = dict(book)

    raw_instruct = body.get('instruct')
    instruct = (
        raw_instruct.strip()
        if isinstance(raw_instruct, str)
        else _book_narrator_instruct(book_data)
    )
    if not instruct:
        return jsonify({'error': 'Narrator instruct is required'}), 400

    raw_mode = body.get('single_narrator_mode', _book_single_narrator_mode(book_data))
    if isinstance(raw_mode, str):
        single_narrator_mode = raw_mode.strip().lower() in {'1', 'true', 'yes', 'on'}
    else:
        single_narrator_mode = bool(raw_mode)

    prev_expression_tags = _book_expression_tags(book_data)
    raw_expr = body.get('expression_tags', prev_expression_tags)
    if isinstance(raw_expr, str):
        expression_tags = raw_expr.strip().lower() in {'1', 'true', 'yes', 'on'}
    else:
        expression_tags = bool(raw_expr)

    narrator_changed = instruct != _book_narrator_instruct(book_data)
    mode_changed = single_narrator_mode != _book_single_narrator_mode(book_data)
    expr_changed = expression_tags != prev_expression_tags

    with get_conn() as conn:
        conn.execute(
            'UPDATE books SET narrator_instruct=?, single_narrator_mode=?, expression_tags=? WHERE id=?',
            (instruct, int(single_narrator_mode), int(expression_tags), book_id)
        )

    if narrator_changed or mode_changed or expr_changed:
        _clear_book_tts_segments(book_id)

    return jsonify({
        'ok': True,
        'instruct': instruct,
        'single_narrator_mode': single_narrator_mode,
        'expression_tags': expression_tags,
        'segments_cleared': narrator_changed or mode_changed or expr_changed,
    })


@app.route('/api/books/<int:book_id>/characters/narrator/preview', methods=['POST'])
def preview_narrator(book_id):
    body = request.get_json(silent=True) or {}
    book = _load_book(book_id)
    if not book:
        return jsonify({'error': 'Not found'}), 404

    status = tts.status()
    if status['state'] != 'ready':
        return jsonify({'error': 'Model not ready', 'status': status}), 503

    instruct = (body.get('instruct') or _book_narrator_instruct(dict(book))).strip()
    narrator_ref = _book_narrator_ref_audio(book_id)
    try:
        result = tts.generate_preview(
            instruct=instruct,
            sample_text=VOICE_PREVIEW_TEXT,
            ref_audio=narrator_ref,
        )
        return jsonify({'audio_url': _audio_url(result['cache_key'])})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/characters/<int:char_id>/ref-audio', methods=['POST'])
def upload_ref_audio(char_id):
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    f = request.files['file']
    path = os.path.join(UPLOAD_DIR, f'ref_{char_id}.wav')
    f.save(path)
    with get_conn() as conn:
        row = conn.execute('SELECT book_id FROM characters WHERE id=?', (char_id,)).fetchone()
        conn.execute('UPDATE characters SET ref_audio_path=? WHERE id=?', (path, char_id))
    if row:
        _clear_book_tts_segments(row['book_id'])
    return jsonify({'ok': True, 'path': path})


@app.route('/api/books/<int:book_id>/narrator-ref-audio', methods=['POST'])
def upload_narrator_ref_audio(book_id):
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    f = request.files['file']
    path = os.path.join(UPLOAD_DIR, f'narrator_ref_{book_id}.wav')
    f.save(path)
    with get_conn() as conn:
        conn.execute('UPDATE books SET narrator_ref_audio_path=? WHERE id=?', (path, book_id))
    _clear_book_tts_segments(book_id)
    return jsonify({'ok': True, 'path': path})


@app.route('/api/books/<int:book_id>/narrator-ref-audio', methods=['DELETE'])
def delete_narrator_ref_audio(book_id):
    with get_conn() as conn:
        row = conn.execute(
            'SELECT narrator_ref_audio_path FROM books WHERE id=?', (book_id,)
        ).fetchone()
        if not row:
            return jsonify({'error': 'Not found'}), 404
        path = row['narrator_ref_audio_path']
        conn.execute(
            'UPDATE books SET narrator_ref_audio_path=NULL WHERE id=?', (book_id,)
        )

    _delete_file_if_exists(path)
    _clear_book_tts_segments(book_id)
    return jsonify({'ok': True, 'segments_cleared': True})


# ════════════════════════════════════════════════════════════════════════════
# TTS API
# ════════════════════════════════════════════════════════════════════════════

@app.route('/api/tts/status')
def tts_status():
    return jsonify(tts.status())


@app.route('/api/tts/load', methods=['POST'])
def tts_load():
    tts.load_async()
    return jsonify({'ok': True})


_SILENCE_CACHE_KEY   = 'a' * 32  # fixed key — all section breaks share one silence file
_SILENCE_DURATION    = 1.5       # seconds
_SILENCE_SAMPLE_RATE = SAMPLE_RATE
_silence_file_ready  = False     # avoids repeated stat() after first generation


def _ensure_section_break_audio(seg) -> str:
    """Ensure the shared silence WAV exists and update the segment DB row to point at it."""
    global _silence_file_ready
    wav_path = os.path.join(_ACD, f'{_SILENCE_CACHE_KEY}.wav')
    if not _silence_file_ready and not os.path.exists(wav_path):
        n = int(_SILENCE_DURATION * _SILENCE_SAMPLE_RATE)
        with wave.open(wav_path, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(_SILENCE_SAMPLE_RATE)
            wf.writeframes(b'\x00\x00' * n)
    _silence_file_ready = True
    if seg.get('cache_key') != _SILENCE_CACHE_KEY:
        with get_conn() as conn:
            _record_segment_audio(conn, seg['id'], wav_path, _SILENCE_DURATION, _SILENCE_CACHE_KEY, 0.0)
    return wav_path


def _section_break_silence(seg, segment_index):
    _ensure_section_break_audio(seg)
    return jsonify({
        'audio_url': _audio_url(_SILENCE_CACHE_KEY),
        'duration_sec': _SILENCE_DURATION,
        'text': '',
        'character_name': None,
        'is_dialogue': False,
        'segment_index': segment_index,
        'cached': seg.get('cache_key') == _SILENCE_CACHE_KEY,
    })


@app.route('/api/tts/generate', methods=['POST'])
def tts_generate():
    body = request.get_json(force=True) or {}
    book_id = body.get('book_id')
    chapter_id = body.get('chapter_id')
    segment_index = body.get('segment_index', 0)

    status = tts.status()
    if status['state'] != 'ready':
        return jsonify({'error': 'Model not ready', 'status': status}), 503

    with get_conn() as conn:
        seg = conn.execute(
            'SELECT * FROM tts_segments WHERE book_id=? AND chapter_id=? AND segment_index=?',
            (book_id, chapter_id, segment_index)
        ).fetchone()

    if not seg:
        ch_lock = _get_chapter_build_lock(book_id, chapter_id)
        with ch_lock:
            # Re-check after acquiring the lock: another thread may have built it.
            with get_conn() as conn:
                seg = conn.execute(
                    'SELECT * FROM tts_segments WHERE book_id=? AND chapter_id=? AND segment_index=?',
                    (book_id, chapter_id, segment_index)
                ).fetchone()
            if not seg:
                if not _build_segments_for_chapter(book_id, chapter_id):
                    return jsonify({'error': 'Chapter not found'}), 404
                with get_conn() as conn:
                    seg = conn.execute(
                        'SELECT * FROM tts_segments WHERE book_id=? AND chapter_id=? AND segment_index=?',
                        (book_id, chapter_id, segment_index)
                    ).fetchone()

    if not seg:
        return jsonify({'error': 'Segment index out of range'}), 404

    seg = dict(seg)

    # Section break: serve a short silence rather than running TTS on the ellipsis text.
    if not seg.get('text'):
        return _section_break_silence(seg, segment_index)

    if _segment_audio_current(seg):
        rid = _tts_start(book_id, chapter_id, segment_index, seg['character_name'])
        _tts_finish(rid, cached=True, duration_sec=seg['duration_sec'])
        return jsonify({
            'audio_url': _audio_url(seg['cache_key']),
            'duration_sec': seg['duration_sec'],
            'text': seg['text'],
            'character_name': seg['character_name'],
            'is_dialogue': bool(seg['is_dialogue']),
            'segment_index': segment_index,
            'cached': True,
        })

    char = None
    if seg['character_name']:
        with get_conn() as conn:
            char = conn.execute(
                'SELECT ref_audio_path FROM characters WHERE book_id=? AND name=?',
                (book_id, seg['character_name'])
            ).fetchone()
    # No character of their own (narration, or the "other speaker" label): the narrator's voice.
    ref_audio = (char['ref_audio_path'] or None) if char else _book_narrator_ref_audio(book_id)

    rid = _tts_start(book_id, chapter_id, segment_index, seg['character_name'])
    try:
        result = tts.generate(
            text=seg['enriched_text'],
            instruct=seg['instruct'],
            ref_audio=ref_audio,
            speed=seg['speed'],
        )
    except RuntimeError as e:
        _tts_finish(rid, error=str(e))
        return jsonify({'error': str(e)}), 503

    with get_conn() as conn:
        _record_segment_audio(conn, seg['id'], result['audio_path'], result['duration_sec'],
                              result['cache_key'], result['generation_sec'])

    _tts_finish(rid, cached=result['cache_hit'], duration_sec=result['duration_sec'])
    return jsonify({
        'audio_url': _audio_url(result['cache_key']),
        'duration_sec': result['duration_sec'],
        'text': seg['text'],
        'character_name': seg['character_name'],
        'is_dialogue': bool(seg['is_dialogue']),
        'segment_index': segment_index,
        'cached': result['cache_hit'],
    })


def _segment_json(r) -> dict:
    """A stored segment row as the reader sees it."""
    has_audio = _segment_audio_current(dict(r))
    return {
        'anchor': r['anchor'],
        'overridden': bool(r['overridden']),
        'segment_index': r['segment_index'],
        'paragraph': r['paragraph'],
        'span': r['span'],
        'text': r['text'],
        'character_name': r['character_name'],
        'is_dialogue': bool(r['is_dialogue']),
        'has_audio': has_audio,
        'audio_url': _audio_url(r['cache_key']) if has_audio else None,
        'duration_sec': r['duration_sec'],
    }


@app.route('/api/tts/segments/<int:book_id>/<int:chapter_id>')
def get_segments(book_id, chapter_id):
    """Return segment metadata, rebuilding if the enrichment version is stale."""
    ch_lock = _get_chapter_build_lock(book_id, chapter_id)
    with ch_lock:
        rows = _ensure_chapter_segments(book_id, chapter_id)
    if not rows:
        return jsonify([])
    return jsonify([_segment_json(r) for r in rows])


def _correction_context(book_id: int, chapter_id: int, rows, anchors: set) -> dict:
    """anchor -> (detected_speaker, text, prev_text, next_text, paragraph_text) for the given
    sentences: what detection alone says about each, and the text around it."""
    detected = {s['anchor']: s['character_name']
                for s in _compute_segments_for_chapter(book_id, chapter_id, apply_corrections=False)}
    paragraphs = defaultdict(list)
    for r in rows:
        paragraphs[r['paragraph']].append(r['text'])
    context = {}
    for i, r in enumerate(rows):
        if r['anchor'] in anchors:
            context[r['anchor']] = (
                detected.get(r['anchor']), r['text'],
                rows[i - 1]['text'] if i else '', rows[i + 1]['text'] if i + 1 < len(rows) else '',
                ' '.join(paragraphs[r['paragraph']]),
            )
    return context


# A correction is also a labelled example: its detection and context are captured once, when it is
# first made. Correcting the same sentence again only changes the speaker (see set_chapter_speakers).
_INSERT_CORRECTION = '''
INSERT INTO segment_speakers (book_id, chapter_id, anchor, character_name, detected_speaker, text,
                              prev_text, next_text, paragraph_text, created_at, updated_at)
VALUES (?,?,?,?,?,?,?,?,?, datetime('now'), datetime('now'))
'''


def _invalidate_chapter(conn, book_id: int, chapter_id: int):
    """Drop a chapter's stored segments and renders so they rebuild on the next read (with the
    current corrections applied); unchanged sentences come back from the WAV cache."""
    conn.execute('DELETE FROM tts_segments WHERE book_id=? AND chapter_id=?', (book_id, chapter_id))
    _clear_chapter_renders(book_id, chapter_id)


@app.route('/api/books/<int:book_id>/chapters/<int:chapter_id>/speaker', methods=['PUT'])
def set_chapter_speakers(book_id, chapter_id):
    """Correct who speaks the given sentences (identified by anchor).

    body: {anchors: [...], character: <name> | null (narrator), reset?: bool}
    reset=true removes the corrections, restoring the detected speaker. Which
    sentences a correction covers (sentence / quote / paragraph) is the client's call.
    """
    body = request.get_json(force=True) or {}
    reset = bool(body.get('reset'))
    name = body.get('character')
    requested = body.get('anchors')
    if not isinstance(requested, list):
        return jsonify({'error': 'anchors must be a list'}), 400

    with _get_chapter_build_lock(book_id, chapter_id):
        rows = _ensure_chapter_segments(book_id, chapter_id)
    valid = {r['anchor'] for r in rows if r['anchor']}
    anchors = [a for a in dict.fromkeys(requested) if a in valid]
    if not anchors:
        return jsonify({'error': 'No matching sentences in this chapter'}), 404

    with get_conn() as conn:
        if not reset and name not in (None, OTHER_SPEAKER) and not conn.execute(
                'SELECT 1 FROM characters WHERE book_id=? AND name=?', (book_id, name)).fetchone():
            return jsonify({'error': f'Unknown character: {name}'}), 400
        if reset:
            conn.executemany(
                'DELETE FROM segment_speakers WHERE book_id=? AND chapter_id=? AND anchor=?',
                [(book_id, chapter_id, a) for a in anchors]
            )
        else:
            have_record = {r[0] for r in conn.execute(
                'SELECT anchor FROM segment_speakers WHERE book_id=? AND chapter_id=?', (book_id, chapter_id))}
            first_time = [a for a in anchors if a not in have_record]
            if first_time:                  # detection + context are captured once, the first time
                context = _correction_context(book_id, chapter_id, rows, set(first_time))
                conn.executemany(_INSERT_CORRECTION,
                                 [(book_id, chapter_id, a, name, *context[a]) for a in first_time])
            conn.executemany(
                "UPDATE segment_speakers SET character_name=?, updated_at=datetime('now') "
                'WHERE book_id=? AND chapter_id=? AND anchor=?',
                [(name, book_id, chapter_id, a) for a in anchors if a in have_record])
        _invalidate_chapter(conn, book_id, chapter_id)
    return jsonify({'ok': True, 'updated': len(anchors)})


def _backfill_correction_context():
    """Corrections made before context was captured: fill it in from the current chapter text
    (and what detection alone says). Sentences that no longer exist are left as they are."""
    with get_conn() as conn:
        pending = conn.execute(
            'SELECT book_id, chapter_id, anchor FROM segment_speakers WHERE text IS NULL').fetchall()
    by_chapter = defaultdict(set)
    for r in pending:
        by_chapter[(r['book_id'], r['chapter_id'])].add(r['anchor'])
    for (book_id, chapter_id), anchors in by_chapter.items():
        try:
            with _get_chapter_build_lock(book_id, chapter_id):
                rows = _ensure_chapter_segments(book_id, chapter_id)
            context = _correction_context(book_id, chapter_id, rows, anchors)
            with get_conn() as conn:
                conn.executemany(
                    'UPDATE segment_speakers SET detected_speaker=?, text=?, prev_text=?, next_text=?, '
                    'paragraph_text=? WHERE book_id=? AND chapter_id=? AND anchor=? AND text IS NULL',
                    [(*ctx, book_id, chapter_id, anchor) for anchor, ctx in context.items()])
        except Exception:
            log.exception('Could not backfill correction context for chapter %s', chapter_id)


def _list_corrections(book_id: int) -> list[dict]:
    """Every speaker correction in the book, in reading order, with its status:
    active, ignored (its character no longer exists) or missing (the sentence no longer exists)."""
    with get_conn() as conn:
        rows = conn.execute(
            'SELECT sp.*, c.title AS chapter_title FROM segment_speakers sp '
            'JOIN chapters c ON c.id = sp.chapter_id WHERE sp.book_id=? ORDER BY c.order_num, sp.chapter_id',
            (book_id,)).fetchall()
        known = {r['name'] for r in conn.execute('SELECT name FROM characters WHERE book_id=?', (book_id,))}
    known.add(OTHER_SPEAKER)
    out = []
    for chapter_id, items in groupby(rows, key=lambda r: r['chapter_id']):
        current = {s['anchor']: (i, s) for i, s in enumerate(_compute_segments_for_chapter(book_id, chapter_id))}
        chapter_items = []
        for r in items:
            index, seg = current.get(r['anchor'], (None, None))
            if seg is None:
                status = 'missing'
            elif r['character_name'] is not None and r['character_name'] not in known:
                status = 'ignored'
            else:
                status = 'active'
            chapter_items.append({
                'chapter_id': chapter_id, 'chapter_title': r['chapter_title'], 'anchor': r['anchor'],
                'segment_index': index, 'status': status,
                'text': r['text'] if r['text'] is not None else (seg['text'] if seg else None),
                'prev_text': r['prev_text'], 'next_text': r['next_text'], 'paragraph_text': r['paragraph_text'],
                'detected_speaker': r['detected_speaker'], 'corrected_speaker': r['character_name'],
                'context_captured': r['text'] is not None,
                'created_at': r['created_at'], 'updated_at': r['updated_at'],
            })
        chapter_items.sort(key=lambda c: (c['segment_index'] is None, c['segment_index'] or 0))
        out.extend(chapter_items)
    return out


@app.route('/api/books/<int:book_id>/corrections')
def list_corrections(book_id):
    return jsonify(_list_corrections(book_id))


@app.route('/api/books/<int:book_id>/corrections', methods=['DELETE'])
def delete_corrections(book_id):
    """Reset corrections: given anchors of one chapter, or all of that chapter's if anchors is omitted.
    Works for corrections whose sentence no longer exists, which the speaker route can't reach."""
    body = request.get_json(silent=True) or {}
    chapter_id, anchors = body.get('chapter_id'), body.get('anchors')
    if not isinstance(chapter_id, int):
        return jsonify({'error': 'chapter_id is required'}), 400
    if anchors is not None and not isinstance(anchors, list):
        return jsonify({'error': 'anchors must be a list'}), 400
    with get_conn() as conn:
        if anchors is None:
            deleted = conn.execute('DELETE FROM segment_speakers WHERE book_id=? AND chapter_id=?',
                                   (book_id, chapter_id)).rowcount
        else:
            deleted = conn.executemany(
                'DELETE FROM segment_speakers WHERE book_id=? AND chapter_id=? AND anchor=?',
                [(book_id, chapter_id, a) for a in anchors]).rowcount
        if not deleted:
            return jsonify({'error': 'No matching corrections'}), 404
        _invalidate_chapter(conn, book_id, chapter_id)
    return jsonify({'ok': True, 'deleted': deleted})


@app.route('/corrections/<int:book_id>')
def corrections_page(book_id):
    book = _load_book(book_id)
    if not book:
        return 'Book not found', 404
    return render_template('corrections.html', book=dict(book))


CONTEXT_SENTENCES = 3       # sentences shown on each side of an excerpt
MAX_EXCERPT_HITS = 5        # the most of a character's sentences shown together in one excerpt


def _canonical_character(book_id: int, char_id: int):
    """The character row for char_id, or the one it is merged into (segments carry that name); None if unknown."""
    with get_conn() as conn:
        char = conn.execute('SELECT * FROM characters WHERE id=? AND book_id=?', (char_id, book_id)).fetchone()
        target = char and conn.execute(
            'SELECT * FROM characters WHERE id=? AND book_id=?', (char['alias_of'], book_id)).fetchone()
    return target or char


@app.route('/api/books/<int:book_id>/characters/<int:char_id>/excerpts')
def character_excerpt_chapters(book_id, char_id):
    """The chapters a character speaks in, with how many sentences: cheap, from what is already built.
    A chapter that is not built yet has count null (unknown until it is opened); built chapters without
    the character are left out."""
    char = _canonical_character(book_id, char_id)
    if not char:
        return jsonify({'error': 'Character not found'}), 404
    with get_conn() as conn:
        chapters = conn.execute(
            'SELECT id, title, enrichment_version FROM chapters WHERE book_id=? ORDER BY order_num',
            (book_id,)).fetchall()
        counts = dict(conn.execute(           # a chapter with no stored segments is not built yet
            'SELECT chapter_id, SUM(character_name=?) FROM tts_segments WHERE book_id=? GROUP BY chapter_id',
            (char['name'], book_id)).fetchall())
    out = []
    for ch in chapters:
        count = counts.get(ch['id']) if ch['enrichment_version'] == ENRICHMENT_VERSION else None
        if count is None or count:              # not built (unknown), or built and has some
            out.append({'id': ch['id'], 'title': ch['title'], 'count': count})
    return jsonify({'character': {'id': char['id'], 'name': char['name']}, 'chapters': out})


@app.route('/api/books/<int:book_id>/characters/<int:char_id>/excerpts/<int:chapter_id>')
def character_chapter_excerpts(book_id, char_id, chapter_id):
    """The sentences a character speaks in one chapter (built now if it is not yet), with the sentence
    before and after each."""
    char = _canonical_character(book_id, char_id)
    if not char:
        return jsonify({'error': 'Character not found'}), 404
    with _get_chapter_build_lock(book_id, chapter_id):
        rows = _ensure_chapter_segments(book_id, chapter_id)
    return jsonify(_excerpt_groups(rows, char['name']))


def _excerpt_groups(rows, name: str) -> list[dict]:
    """A character's sentences in a chapter as excerpts. Sentences close enough that their context
    would overlap (and so be repeated) are shown together as one excerpt, up to MAX_EXCERPT_HITS of them. An excerpt
    keeps the chapter's paragraphs: {segment_index, paragraphs: [[{text, anchor, hit, overridden}]]}."""
    hits = [i for i, r in enumerate(rows) if r['character_name'] == name]
    groups = []
    for i in hits:
        if groups and len(groups[-1]) < MAX_EXCERPT_HITS and i - groups[-1][-1] <= 2 * CONTEXT_SENTENCES + 1:
            groups[-1].append(i)
        else:
            groups.append([i])
    out = []
    for group in groups:
        window = range(max(0, group[0] - CONTEXT_SENTENCES), min(len(rows), group[-1] + CONTEXT_SENTENCES + 1))
        paragraphs = []
        for _, idxs in groupby(window, key=lambda i: rows[i]['paragraph']):
            sentences = [{'text': rows[i]['text'], 'anchor': rows[i]['anchor'], 'hit': i in group,
                          'overridden': bool(rows[i]['overridden'])} for i in idxs if rows[i]['text']]
            if sentences:
                paragraphs.append(sentences)
        out.append({'segment_index': rows[group[0]]['segment_index'], 'paragraphs': paragraphs})
    return out


@app.route('/characters/<int:book_id>/<int:char_id>')
def character_page(book_id, char_id):
    book = _load_book(book_id)
    if not book:
        return 'Book not found', 404
    char = _canonical_character(book_id, char_id)
    if not char:
        return 'Character not found', 404
    return render_template('character.html', book=dict(book), character=dict(char), other_speaker=OTHER_SPEAKER)


def _store_segments(book_id, chapter_id, segs):
    with get_conn() as conn:
        conn.execute(
            'DELETE FROM tts_segments WHERE book_id=? AND chapter_id=?',
            (book_id, chapter_id)
        )
        for i, s in enumerate(segs):
            cache_key = f'pending:{book_id}:{chapter_id}:{i}:{uuid.uuid4().hex}'
            conn.execute(
                'INSERT INTO tts_segments '
                '(book_id, chapter_id, segment_index, paragraph, text, enriched_text, '
                'character_name, instruct, speed, is_dialogue, span, anchor, overridden, cache_key) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (book_id, chapter_id, i, s['paragraph'], s['text'], s['enriched_text'],
                 s['character_name'], s['instruct'], s['speed'],
                 int(s['is_dialogue']), s['span'], s['anchor'], int(s['overridden']), cache_key)
            )
        conn.execute(
            'UPDATE chapters SET enrichment_version=? WHERE id=? AND book_id=?',
            (ENRICHMENT_VERSION, chapter_id, book_id)
        )


_CACHE_KEY_RE = re.compile(r'^[0-9a-fA-F]{32}$')


@app.route('/api/audio/<cache_key>')
def serve_audio(cache_key):
    if not _CACHE_KEY_RE.match(cache_key):
        return '', 404
    from core.base_engine import AUDIO_CACHE_DIR
    path = os.path.join(AUDIO_CACHE_DIR, f'{cache_key}.wav')
    if not os.path.exists(path):
        return '', 404
    return send_file(path, mimetype='audio/wav')


@app.route('/api/audio/<cache_key>/fmp4')
def serve_audio_fmp4(cache_key):
    """WAV converted to fragmented MP4 (AAC) for MediaSource Extensions streaming."""
    if not _CACHE_KEY_RE.match(cache_key):
        return '', 404
    from core.base_engine import AUDIO_CACHE_DIR
    from core.exporter import _ffmpeg_available
    wav_path = os.path.join(AUDIO_CACHE_DIR, f'{cache_key}.wav')
    if not os.path.exists(wav_path):
        return '', 404
    fmp4_path = os.path.join(AUDIO_CACHE_DIR, f'{cache_key}.fmp4')
    if not os.path.exists(fmp4_path):
        if not _ffmpeg_available():
            return jsonify({'error': 'ffmpeg not available'}), 503
        import subprocess
        try:
            subprocess.run(
                [
                    'ffmpeg', '-i', wav_path,
                    '-c:a', 'aac', '-b:a', '128k', '-ac', '1',
                    '-f', 'mp4',
                    '-movflags', 'frag_keyframe+empty_moov+default_base_moof',
                    '-y', fmp4_path,
                ],
                check=True, capture_output=True,
            )
        except subprocess.CalledProcessError:
            return '', 500
    return send_file(fmp4_path, mimetype='audio/mp4')


# ════════════════════════════════════════════════════════════════════════════
# Chapter prerender API (full-chapter fMP4 for smooth mobile playback)
# ════════════════════════════════════════════════════════════════════════════

def _run_chapter_prerender(job_id: str, book_id: int, chapter_id: int):
    job = _export_jobs[job_id]
    try:
        job['state'] = 'running'
        job['message'] = 'Loading segments...'
        segs = _get_chapter_segments(chapter_id, book_id)
        job['total'] = len(segs)
        job['done'] = 0
        job['message'] = f'Generating audio ({len(segs)} segments)...'
        _ensure_audio_for_chapter(book_id, chapter_id, segs, job)

        job['message'] = 'Merging audio...'
        from core.exporter import SAMPLE_RATE, _merge_wavs, build_timeline

        timeline = build_timeline(segs)
        if not timeline:
            job['state'] = 'failed'
            job['error'] = 'No audio generated for this chapter'
            return

        merged = _merge_wavs([s['audio_path'] for s in timeline])

        job['message'] = 'Encoding audio...'
        import subprocess
        raw = merged.tobytes()

        fmp4_path = _chapter_render_fmp4(book_id, chapter_id)
        proc = subprocess.Popen(
            ['ffmpeg',
             '-f', 'f32le', '-ar', str(SAMPLE_RATE), '-ac', '1', '-i', 'pipe:0',
             '-c:a', 'aac', '-b:a', '128k',
             '-f', 'mp4', '-movflags', 'frag_keyframe+empty_moov+default_base_moof',
             '-y', fmp4_path],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        _, stderr = proc.communicate(raw)
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, 'ffmpeg', stderr=stderr)

        m4a_path = _chapter_render_m4a(book_id, chapter_id)
        proc2 = subprocess.Popen(
            ['ffmpeg',
             '-f', 'f32le', '-ar', str(SAMPLE_RATE), '-ac', '1', '-i', 'pipe:0',
             '-c:a', 'aac', '-b:a', '128k', '-movflags', 'faststart',
             '-y', m4a_path],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        _, stderr2 = proc2.communicate(raw)
        if proc2.returncode != 0:
            raise subprocess.CalledProcessError(proc2.returncode, 'ffmpeg', stderr=stderr2)

        tl = [{'idx': s['segment_index'], 't_start': s['t_start'], 't_end': s['t_end']} for s in timeline]
        duration_sec = tl[-1]['t_end'] if tl else 0
        with open(_chapter_render_meta(book_id, chapter_id), 'w') as f:
            json.dump({'timeline': tl, 'total_segments': len(segs), 'duration_sec': duration_sec}, f)

        job['state'] = 'complete'
        job['message'] = 'Done'
        job['result'] = {
            'audio_url': f'/api/chapter-audio/{book_id}/{chapter_id}',
            'timeline': tl,
        }
    except Exception as e:
        log.exception('Chapter prerender job %s failed', job_id)
        job['state'] = 'failed'
        job['error'] = str(e)
    finally:
        _chapter_prerender_jobs.pop((book_id, chapter_id), None)


@app.route('/api/books/<int:book_id>/chapters/<int:chapter_id>/prerender', methods=['POST'])
def start_chapter_prerender(book_id, chapter_id):
    fmp4 = _chapter_render_fmp4(book_id, chapter_id)
    meta_p = _chapter_render_meta(book_id, chapter_id)
    if os.path.exists(fmp4) and os.path.exists(meta_p):
        with open(meta_p) as f:
            meta = json.load(f)
        return jsonify({
            'cached': True,
            'audio_url': f'/api/chapter-audio/{book_id}/{chapter_id}',
            'timeline': meta['timeline'],
            'total_segments': meta.get('total_segments'),
        })
    key = (book_id, chapter_id)
    existing = _chapter_prerender_jobs.get(key)
    if existing and _export_jobs.get(existing, {}).get('state') in ('pending', 'running'):
        return jsonify({'job_id': existing})
    job_id, job = _make_export_job()
    job['message'] = 'Queued'
    with get_conn() as conn:
        ch_row = conn.execute('SELECT title FROM chapters WHERE id=?', (chapter_id,)).fetchone()
    title = ch_row['title'] if ch_row else None
    job['chapter_label'] = title or f'book {book_id} / chapter {chapter_id}'
    _chapter_prerender_jobs[key] = job_id
    _prerender_queue.put((job_id, book_id, chapter_id))
    return jsonify({'job_id': job_id})


@app.route('/api/books/<int:book_id>/chapters/<int:chapter_id>/prerender', methods=['DELETE'])
def delete_chapter_prerender(book_id, chapter_id):
    fmp4 = _chapter_render_fmp4(book_id, chapter_id)
    meta_p = _chapter_render_meta(book_id, chapter_id)
    m4a = _chapter_render_m4a(book_id, chapter_id)
    removed = []
    for p in (fmp4, m4a, meta_p):
        try:
            if os.path.exists(p):
                os.remove(p)
                removed.append(os.path.basename(p))
        except OSError as e:
            return jsonify({'error': str(e)}), 500
    _chapter_prerender_jobs.pop((book_id, chapter_id), None)
    return jsonify({'ok': True, 'removed': removed})


@app.route('/api/chapter-audio/<int:book_id>/<int:chapter_id>')
def serve_chapter_audio(book_id, chapter_id):
    fmp4 = _chapter_render_fmp4(book_id, chapter_id)
    if not os.path.exists(fmp4):
        return '', 404
    return send_file(fmp4, mimetype='audio/mp4')


@app.route('/api/podcast-audio/<int:book_id>/<int:chapter_id>')
def serve_podcast_audio(book_id, chapter_id):
    m4a = _chapter_render_m4a(book_id, chapter_id)
    if not os.path.exists(m4a):
        import subprocess
        fmp4 = _chapter_render_fmp4(book_id, chapter_id)
        if not os.path.exists(fmp4):
            return '', 404
        proc = subprocess.run(
            ['ffmpeg', '-i', fmp4, '-c:a', 'aac', '-b:a', '128k', '-movflags', 'faststart', '-y', m4a],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        if proc.returncode != 0:
            return '', 500
    return send_file(m4a, mimetype='audio/mp4')


def _chapter_pub_timestamp(added_at: str, order_num: int) -> float:
    from datetime import datetime, timezone
    try:
        dt = datetime.strptime(added_at, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        dt = datetime.now(timezone.utc)
    return dt.timestamp() + order_num * 60


@app.route('/api/books/<int:book_id>/podcast.rss')
def podcast_rss(book_id):
    with get_conn() as conn:
        book = conn.execute('SELECT * FROM books WHERE id=?', (book_id,)).fetchone()
        if not book:
            return '', 404
        chapters = conn.execute(
            'SELECT id, title, order_num FROM chapters WHERE book_id=? ORDER BY order_num DESC',
            (book_id,)
        ).fetchall()

    base = request.host_url.rstrip('/')
    has_cover = bool(book['cover_b64'])

    rss = ET.Element('rss', {
        'version': '2.0',
        'xmlns:itunes': 'http://www.itunes.com/dtds/podcast-1.0.dtd',
    })
    channel = ET.SubElement(rss, 'channel')

    def sub(parent, tag, text=None, **attrib):
        el = ET.SubElement(parent, tag, attrib)
        if text is not None:
            el.text = text
        return el

    sub(channel, 'title', book['title'])
    sub(channel, 'description', f'{book["title"]} by {book["author"] or "Unknown"}')
    sub(channel, 'link', f'{base}/reader/{book_id}')
    sub(channel, 'language', book['language'] or 'en')
    sub(channel, 'itunes:author', book['author'] or 'Unknown')
    if has_cover:
        sub(channel, 'itunes:image', href=f'{base}/api/books/{book_id}/cover')
        img = sub(channel, 'image')
        sub(img, 'url', f'{base}/api/books/{book_id}/cover')
        sub(img, 'title', book['title'])
        sub(img, 'link', f'{base}/reader/{book_id}')

    for ch in chapters:
        fmp4 = _chapter_render_fmp4(book_id, ch['id'])
        meta_p = _chapter_render_meta(book_id, ch['id'])
        try:
            fmp4_stat = os.stat(fmp4)
        except FileNotFoundError:
            continue
        try:
            with open(meta_p) as f:
                meta = json.load(f)
        except FileNotFoundError:
            continue

        m4a = _chapter_render_m4a(book_id, ch['id'])
        try:
            size = os.stat(m4a).st_size
        except FileNotFoundError:
            size = fmp4_stat.st_size  # m4a not yet generated; use fmp4 size as hint
        tl = meta.get('timeline', [])
        duration_sec = meta.get('duration_sec') or (tl[-1]['t_end'] if tl else 0)
        dur_str = exporter._fmt_itunes_duration(duration_sec)

        audio_url = f'{base}/api/podcast-audio/{book_id}/{ch["id"]}'
        pub_date = email.utils.formatdate(_chapter_pub_timestamp(book['added_at'], ch['order_num']))

        item = sub(channel, 'item')
        sub(item, 'title', ch['title'])
        sub(item, 'guid', audio_url)
        sub(item, 'pubDate', pub_date)
        sub(item, 'itunes:duration', dur_str)
        sub(item, 'itunes:episode', str(ch['order_num']))
        sub(item, 'enclosure', url=audio_url, length=str(size), type='audio/mp4')

    xml_bytes = ET.tostring(rss, encoding='unicode', xml_declaration=False)
    body = '<?xml version="1.0" encoding="UTF-8"?>\n' + xml_bytes
    return Response(body, mimetype='application/rss+xml; charset=utf-8')


# ════════════════════════════════════════════════════════════════════════════
# Export API
# ════════════════════════════════════════════════════════════════════════════

def _ensure_audio_for_chapter(book_id: int, chapter_id: int, segs: list[dict], job: dict | None = None):
    """Generate TTS for any segment in segs that has no audio yet, updating DB and segs in-place."""
    with get_conn() as conn:
        chars = {
            r['name']: dict(r)
            for r in conn.execute(
                'SELECT * FROM characters WHERE book_id=?', (book_id,)
            ).fetchall()
        }
    narrator_ref = _book_narrator_ref_audio(book_id)

    def _ref_for(seg):
        char = chars.get(seg['character_name'])
        return char.get('ref_audio_path') or None if char else narrator_ref

    def _tick():
        if job is not None:
            job['done'] += 1

    items, pending_segs = [], []
    for seg in segs:
        if _segment_audio_current(seg):
            _tick()
            continue
        if not seg.get('text'):  # section break — use shared silence, not TTS
            wav_path = _ensure_section_break_audio(seg)
            seg['audio_path'] = wav_path
            seg['duration_sec'] = _SILENCE_DURATION
            _tick()
            continue
        items.append({'text': seg['enriched_text'], 'instruct': seg['instruct'],
                      'ref_audio': _ref_for(seg), 'speed': seg['speed']})
        pending_segs.append(seg)

    if not pending_segs:
        return

    results = tts.generate_batch(items, progress_cb=_tick)

    for seg, result in zip(pending_segs, results):
        if result is None:
            log.warning('Audio generation failed for segment %s', seg.get('id'))
        else:
            try:
                with get_conn() as conn:
                    _record_segment_audio(conn, seg['id'], result['audio_path'], result['duration_sec'],
                                          result['cache_key'], result['generation_sec'])
                seg['audio_path'] = result['audio_path']
                seg['duration_sec'] = result['duration_sec']
                seg['cache_key'] = result['cache_key']
            except Exception as e:
                log.warning('Failed to save audio for segment %s: %s', seg.get('id'), e)


def _get_char_colors(book_id):
    with get_conn() as conn:
        rows = conn.execute(
            'SELECT name, color_hex FROM characters WHERE book_id=?', (book_id,)
        ).fetchall()
    return {r['name']: r['color_hex'] for r in rows}


def _get_chapter_segments(chapter_id, book_id):
    with get_conn() as conn:
        rows = conn.execute(
            'SELECT * FROM tts_segments WHERE book_id=? AND chapter_id=? ORDER BY segment_index',
            (book_id, chapter_id)
        ).fetchall()
    if not rows:
        ch_lock = _get_chapter_build_lock(book_id, chapter_id)
        with ch_lock:
            with get_conn() as conn:
                rows = conn.execute(
                    'SELECT * FROM tts_segments WHERE book_id=? AND chapter_id=? ORDER BY segment_index',
                    (book_id, chapter_id)
                ).fetchall()
            if not rows:
                _build_segments_for_chapter(book_id, chapter_id)
                with get_conn() as conn:
                    rows = conn.execute(
                        'SELECT * FROM tts_segments WHERE book_id=? AND chapter_id=? ORDER BY segment_index',
                        (book_id, chapter_id)
                    ).fetchall()
    return [dict(r) for r in rows]


def _make_export_job() -> tuple[str, dict]:
    job_id = str(uuid.uuid4())
    job: dict = {'state': 'pending', 'message': 'Starting...', 'done': 0, 'total': 0, 'result': None, 'error': None}
    _export_jobs[job_id] = job
    return job_id, job


def _run_chapter_export(job_id: str, book_id: int, chapter_id: int, audio_fmt: str, sub_fmt: str):
    job = _export_jobs[job_id]
    try:
        job['state'] = 'running'
        job['message'] = 'Loading segments...'
        with get_conn() as conn:
            ch = conn.execute('SELECT * FROM chapters WHERE id=? AND book_id=?',
                              (chapter_id, book_id)).fetchone()
            book = conn.execute('SELECT title FROM books WHERE id=?', (book_id,)).fetchone()
        if not ch:
            job['state'] = 'failed'
            job['error'] = 'Chapter not found'
            return
        segs = _get_chapter_segments(chapter_id, book_id)
        job['total'] = len(segs)
        job['done'] = 0
        job['message'] = f'Generating audio ({len(segs)} segments)...'
        _ensure_audio_for_chapter(book_id, chapter_id, segs, job)
        job['message'] = 'Merging audio...'
        colors = _get_char_colors(book_id)
        result = exporter.export_single_chapter(ch['title'], book['title'], segs, colors, audio_fmt, sub_fmt)
        job['state'] = 'complete'
        job['message'] = 'Done'
        job['result'] = {
            'audio_download': f'/api/export/download?path={result["audio_path"]}',
            'subtitle_download': f'/api/export/download?path={result["subtitle_path"]}',
        }
    except Exception as e:
        log.exception('Export job %s failed', job_id)
        job['state'] = 'failed'
        job['error'] = str(e)


def _run_full_export(job_id: str, book_id: int, audio_fmt: str, sub_fmt: str):
    job = _export_jobs[job_id]
    try:
        job['state'] = 'running'
        job['message'] = 'Loading chapters...'
        with get_conn() as conn:
            book = conn.execute('SELECT * FROM books WHERE id=?', (book_id,)).fetchone()
            chapters = conn.execute(
                'SELECT id FROM chapters WHERE book_id=? ORDER BY order_num', (book_id,)
            ).fetchall()
        chapters_segs: list[tuple[int, list[dict]]] = []
        for ch in chapters:
            segs = _get_chapter_segments(ch['id'], book_id)
            chapters_segs.append((ch['id'], segs))
        total = sum(len(s) for _, s in chapters_segs)
        job['total'] = total
        job['done'] = 0
        job['message'] = f'Generating audio ({total} segments)...'
        for ch_id, segs in chapters_segs:
            _ensure_audio_for_chapter(book_id, ch_id, segs, job)
        job['message'] = 'Merging audio...'
        all_segs = [s for _, segs in chapters_segs for s in segs]
        colors = _get_char_colors(book_id)
        result = exporter.export_full_book(book['title'], all_segs, colors, audio_fmt, sub_fmt)
        job['state'] = 'complete'
        job['message'] = 'Done'
        job['result'] = {
            'audio_download': f'/api/export/download?path={result["audio_path"]}',
            'subtitle_download': f'/api/export/download?path={result["subtitle_path"]}',
        }
    except Exception as e:
        log.exception('Export job %s failed', job_id)
        job['state'] = 'failed'
        job['error'] = str(e)


def _run_chapterwise_export(job_id: str, book_id: int, audio_fmt: str, sub_fmt: str):
    job = _export_jobs[job_id]
    try:
        job['state'] = 'running'
        job['message'] = 'Loading chapters...'
        with get_conn() as conn:
            book = conn.execute('SELECT * FROM books WHERE id=?', (book_id,)).fetchone()
            chapters = conn.execute(
                'SELECT id, title FROM chapters WHERE book_id=? ORDER BY order_num', (book_id,)
            ).fetchall()
        chapters_data: list[dict] = []
        for ch in chapters:
            segs = _get_chapter_segments(ch['id'], book_id)
            chapters_data.append({'chapter_title': ch['title'], 'ch_id': ch['id'], 'segments': segs})
        total = sum(len(c['segments']) for c in chapters_data)
        job['total'] = total
        job['done'] = 0
        job['message'] = f'Generating audio ({total} segments)...'
        for ch_data in chapters_data:
            _ensure_audio_for_chapter(book_id, ch_data['ch_id'], ch_data['segments'], job)
        job['message'] = 'Packaging ZIP...'
        colors = _get_char_colors(book_id)
        zip_path = exporter.export_chapter_zip(
            book['title'],
            [{'chapter_title': c['chapter_title'], 'segments': c['segments']} for c in chapters_data if c['segments']],
            colors, audio_fmt, sub_fmt,
        )
        job['state'] = 'complete'
        job['message'] = 'Done'
        job['result'] = {'zip_download': f'/api/export/download?path={zip_path}'}
    except Exception as e:
        log.exception('Export job %s failed', job_id)
        job['state'] = 'failed'
        job['error'] = str(e)


def _resolve_sub_fmt(book_id: int, requested: str) -> str:
    book = _load_book(book_id)
    if book and _book_single_narrator_mode(dict(book)):
        return 'srt'
    return requested


@app.route('/api/books/<int:book_id>/export/chapter/<int:chapter_id>', methods=['POST'])
def export_chapter(book_id, chapter_id):
    body = request.get_json(force=True) or {}
    audio_fmt = body.get('audio_fmt', 'wav')
    sub_fmt = _resolve_sub_fmt(book_id, body.get('sub_fmt', 'srt'))

    if tts.status()['state'] != 'ready':
        return jsonify({'error': 'TTS model not ready'}), 503

    job_id, _ = _make_export_job()
    threading.Thread(
        target=_run_chapter_export,
        args=(job_id, book_id, chapter_id, audio_fmt, sub_fmt),
        daemon=True,
    ).start()
    return jsonify({'job_id': job_id})


@app.route('/api/books/<int:book_id>/export/full', methods=['POST'])
def export_full(book_id):
    body = request.get_json(force=True) or {}
    audio_fmt = body.get('audio_fmt', 'wav')
    sub_fmt = _resolve_sub_fmt(book_id, body.get('sub_fmt', 'srt'))

    if tts.status()['state'] != 'ready':
        return jsonify({'error': 'TTS model not ready'}), 503

    job_id, _ = _make_export_job()
    threading.Thread(
        target=_run_full_export,
        args=(job_id, book_id, audio_fmt, sub_fmt),
        daemon=True,
    ).start()
    return jsonify({'job_id': job_id})


@app.route('/api/books/<int:book_id>/export/chapterwise', methods=['POST'])
def export_chapterwise(book_id):
    body = request.get_json(force=True) or {}
    audio_fmt = body.get('audio_fmt', 'wav')
    sub_fmt = _resolve_sub_fmt(book_id, body.get('sub_fmt', 'srt'))

    if tts.status()['state'] != 'ready':
        return jsonify({'error': 'TTS model not ready'}), 503

    job_id, _ = _make_export_job()
    threading.Thread(
        target=_run_chapterwise_export,
        args=(job_id, book_id, audio_fmt, sub_fmt),
        daemon=True,
    ).start()
    return jsonify({'job_id': job_id})


@app.route('/api/export/status/<job_id>')
def export_job_status(job_id):
    job = _export_jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Unknown job'}), 404
    return jsonify(job)


@app.route('/api/export/download')
def export_download():
    path = request.args.get('path', '')
    exports_dir = os.path.abspath(exporter.EXPORTS_DIR)
    abs_path = os.path.abspath(path)
    try:
        within_exports = os.path.commonpath([exports_dir, abs_path]) == exports_dir
    except ValueError:
        within_exports = False  # different drive on Windows, etc.
    if not within_exports:
        return 'Forbidden', 403
    if not os.path.exists(abs_path):
        return 'Not found', 404
    return send_file(abs_path, as_attachment=True)


# ════════════════════════════════════════════════════════════════════════════
# Bookmarks API
# ════════════════════════════════════════════════════════════════════════════

@app.route('/api/books/<int:book_id>/bookmarks')
def list_bookmarks(book_id):
    with get_conn() as conn:
        rows = conn.execute(
            'SELECT b.*, c.title as chapter_title FROM bookmarks b '
            'JOIN chapters c ON b.chapter_id = c.id '
            'WHERE b.book_id=? ORDER BY b.created_at DESC',
            (book_id,)
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/books/<int:book_id>/bookmarks', methods=['POST'])
def add_bookmark(book_id):
    body = request.get_json(force=True) or {}
    chapter_id = body.get('chapter_id')
    segment_index = body.get('segment_index', 0)
    text_excerpt = (body.get('text_excerpt', '') or '')[:200]
    label = body.get('label', '')
    if not chapter_id:
        return jsonify({'error': 'chapter_id required'}), 400
    with get_conn() as conn:
        cur = conn.execute(
            'INSERT INTO bookmarks (book_id, chapter_id, segment_index, text_excerpt, label) '
            'VALUES (?,?,?,?,?)',
            (book_id, chapter_id, segment_index, text_excerpt, label)
        )
    return jsonify({'ok': True, 'id': cur.lastrowid})


@app.route('/api/books/<int:book_id>/bookmarks/<int:bm_id>', methods=['DELETE'])
def delete_bookmark(book_id, bm_id):
    with get_conn() as conn:
        conn.execute('DELETE FROM bookmarks WHERE id=? AND book_id=?', (bm_id, book_id))
    return jsonify({'ok': True})


# ════════════════════════════════════════════════════════════════════════════
# Settings API
# ════════════════════════════════════════════════════════════════════════════

@app.route('/settings')
def settings_page():
    return render_template('settings.html')


@app.route('/api/settings', methods=['GET'])
def get_settings():
    return jsonify(app_settings.load())


@app.route('/api/settings', methods=['POST'])
def save_settings():
    body = request.get_json(force=True) or {}
    previous = app_settings.load()
    allowed = {
        'tts_engine', 'model_source', 'model_path', 'model_repo', 'hf_endpoint', 'auto_download_model',
        'narrator_instruct', 'single_narrator_mode', 'default_speed', 'audio_format',
        'subtitle_format', 'theme', 'font_size', 'font_family', 'line_height',
    }
    updates = {k: v for k, v in body.items() if k in allowed}
    if 'tts_engine' in updates:
        try:  # switch first so an invalid/unloadable engine is never persisted
            if tts.set_engine(updates['tts_engine']):
                _prune_stale_renders()
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400
    result = app_settings.save(updates)

    tts.apply_settings(updates)

    if 'narrator_instruct' in updates and updates['narrator_instruct'] != previous.get('narrator_instruct'):
        with get_conn() as conn:
            conn.execute(
                'DELETE FROM tts_segments WHERE book_id IN (SELECT id FROM books WHERE narrator_instruct IS NULL)'
            )

    return jsonify({'ok': True, 'settings': result})


@app.route('/api/settings/spacy-status')
def spacy_status_route():
    status = app_settings.spacy_status()
    status['error'] = char_module.spacy_error()
    return jsonify(status)


@app.route('/api/settings/spacy-install', methods=['POST'])
def spacy_install():
    result = app_settings.install_spacy_model()
    if result['ok']:
        # Reset spaCy NLP so it reloads the new model
        import core.characters as cm
        cm._nlp = None
        cm._spacy_error = ''
    return jsonify(result)


@app.route('/api/settings/model-download', methods=['POST'])
def start_download():
    body = request.get_json(force=True) or {}
    repo_id = body.get('repo_id', app_settings.get('model_repo', 'k2-fsa/OmniVoice'))
    dest = body.get('dest', app_settings.get('model_path'))
    hf_endpoint = body.get('hf_endpoint', app_settings.get('hf_endpoint', ''))
    app_settings.start_model_download(repo_id, dest, hf_endpoint)
    return jsonify({'ok': True, 'dest': dest})


@app.route('/api/settings/model-download/progress')
def download_progress():
    return jsonify(app_settings.download_state())


@app.route('/api/settings/tts-reload', methods=['POST'])
def tts_reload():
    tts.reload()
    return jsonify({'ok': True})


@app.route('/api/settings/check-model-path', methods=['POST'])
def check_model_path():
    body = request.get_json(force=True) or {}
    path = body.get('path', '')
    exists = os.path.isdir(path)
    has_config = os.path.exists(os.path.join(path, 'config.json'))
    return jsonify({'exists': exists, 'has_config': has_config, 'path': path})


# ════════════════════════════════════════════════════════════════════════════
# Run
# ════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default=os.environ.get('AURIS_HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('AURIS_PORT', 7860)))
    parser.add_argument('--debug', action='store_true',
                        default=os.environ.get('AURIS_DEBUG', '').lower() in ('1', 'true'))
    args = parser.parse_args()
    display_host = '127.0.0.1' if args.host == '0.0.0.0' else args.host
    print(f" * Open your browser at: http://{display_host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True, use_reloader=False)
