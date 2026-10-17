const BOOK_ID = window.BOOK_ID;
const MOBILE_BREAKPOINT = 768;

// ── State ─────────────────────────────────────────────────────────────────────
let chapters        = [];
let currentChapterId= null;
let segments        = [];
let currentSegIdx   = 0;
let isPlaying       = false;
let speedMultiplier = parseFloat(localStorage.getItem('speedMultiplier') || '1.0');
let fontSize        = parseInt(localStorage.getItem('fontSize') || '18');
let fontFamily      = localStorage.getItem('fontFamily') || 'serif';
let lineHeight      = parseFloat(localStorage.getItem('lineHeight') || '1.9');
let currentTheme    = localStorage.getItem('theme') || 'night';
let showVoices      = localStorage.getItem('showVoices') !== '0';
let charColors      = {};   // character name -> #RRGGBB
let _segDurations   = {};   // segIdx -> duration_sec (accumulated as segments play)

// ── MSE (MediaSource Extensions) — primary audio path ────────────────────────
// Falls back to WAV-per-segment if browser lacks MSE for AAC MP4.
const MSE_MIME = 'audio/mp4; codecs="mp4a.40.2"';
const _mseOk   = typeof MediaSource !== 'undefined' && MediaSource.isTypeSupported(MSE_MIME);
let _ms        = null;   // MediaSource
let _sb        = null;   // SourceBuffer
let _sbReady   = false;  // SourceBuffer ready to accept appends
let _sbQ       = [];     // pending append queue [{buf, res, rej}]
let _segStart  = {};     // segIdx -> chapter-timeline start (seconds)
let _segEnd    = {};     // segIdx -> chapter-timeline end   (seconds)
let _mseNext   = 0;      // expected start time for the next segment to append
let _mseGenId  = 0;      // incremented on teardown; stale appenders bail out

// ── Full-chapter mode ─────────────────────────────────────────────────────────
// When enabled, the whole chapter is pre-rendered to a single fMP4 and played
// as one file.  iOS sees one track instead of one per segment.
let _fullChapterMode     = localStorage.getItem('fullChapterMode') === '1';
let _chapterAudioChId    = null; // chapter ID whose audio is currently loaded in audio.src
let _chapterPrerenderChId = null; // chapter ID for which a prerender POST has been sent

let _progressSaveTimer = null;
let _scrollProgressTimer = null;
let _lastSavedProgressKey = '';
let _ignoreScrollTrackingUntil = 0;

// Two audio elements for gapless double-buffering
const _audioA = document.getElementById('tts-audio');
const _audioB = (() => { const a = new Audio(); a.preload = 'auto'; return a; })();
let audio = _audioA; // currently active (playing) element

// Client-side cache: segIdx -> Promise<{audio_url, duration_sec, cache_key, text}>
let _segCache = new Map();

// Which segment index is pre-buffered in the standby element, and its data
let _preloadIdx = -1;
let _preloadData = null;

// Monotonic counter — incremented on every new playSegment and on stopPlayback.
// Each playSegment captures its generation at entry; stale async continuations
// bail out when their generation no longer matches the current one.
let _playGen = 0;

// ── Sleep timer ───────────────────────────────────────────────────────────────
let _sleepTimerId       = null;  // setTimeout id
let _sleepTimerEnd      = 0;     // absolute ms timestamp when timer fires (0 = off)
let _sleepOnChapterEnd  = false; // stop at the next chapter boundary instead
let _sleepTimerFired    = false; // set when timer fires; cleared by setSleepTimer / playSegment
let _sleepCountdownId   = null;  // setInterval for the countdown display

// Cancellation token for the background buffer loop — incremented on each
// chapter open so the previous loop exits without touching the new chapter.
let _bufferGenId = 0;

function _standby() { return audio === _audioA ? _audioB : _audioA; }
function _swapAudio() { audio = (audio === _audioA ? _audioB : _audioA); }

function clampSegmentIndex(idx, segList = segments) {
  const parsed = Number.parseInt(idx, 10);
  const safe = Number.isFinite(parsed) ? parsed : 0;
  const max = Math.max((segList?.length || 1) - 1, 0);
  return Math.max(0, Math.min(safe, max));
}

function progressKey(chapterId, position) {
  return `${chapterId}:${position}`;
}

function sendProgress(chapterId, position, options = {}) {
  const { useBeacon = false, force = false } = options;
  if (!chapterId) return;

  const clamped = clampSegmentIndex(position);
  const key = progressKey(chapterId, clamped);
  if (!force && key === _lastSavedProgressKey) return;
  _lastSavedProgressKey = key;

  const payload = JSON.stringify({ chapter_id: chapterId, position: clamped });
  const url = `/api/books/${BOOK_ID}/progress`;

  if (useBeacon && navigator.sendBeacon) {
    const ok = navigator.sendBeacon(url, new Blob([payload], { type: 'application/json' }));
    if (ok) return;
  }

  fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: payload,
    keepalive: useBeacon,
  }).catch(() => {});
}

function queueProgressSave(chapterId = currentChapterId, position = currentSegIdx) {
  if (!chapterId) return;
  clearTimeout(_progressSaveTimer);
  _progressSaveTimer = setTimeout(() => {
    sendProgress(chapterId, position);
  }, 250);
}

function flushProgressSave(options = {}) {
  clearTimeout(_progressSaveTimer);
  if (currentChapterId) {
    sendProgress(currentChapterId, currentSegIdx, options);
  }
}

function setCurrentSegment(idx, options = {}) {
  const { highlight = false, behavior = 'smooth', save = true } = options;
  currentSegIdx = clampSegmentIndex(idx);
  if (highlight) {
    highlightSegment(currentSegIdx, { behavior });
  }
  updatePlaybackUI();
  updateProgress();
  if (save) {
    queueProgressSave(currentChapterId, currentSegIdx);
  }
  return currentSegIdx;
}

// ── Theme ─────────────────────────────────────────────────────────────────────

const THEMES = ['night', 'sepia', 'paper', 'amoled'];

function applyTheme(theme) {
  THEMES.forEach(t => document.body.classList.remove('theme-' + t));
  if (theme !== 'night') document.body.classList.add('theme-' + theme);
  currentTheme = theme;
  localStorage.setItem('theme', theme);
}

function cycleTheme() {
  const next = THEMES[(THEMES.indexOf(currentTheme) + 1) % THEMES.length];
  applyTheme(next);
  showToast('Theme: ' + next.charAt(0).toUpperCase() + next.slice(1));
}

applyTheme(currentTheme);

// ── Font settings ─────────────────────────────────────────────────────────────

const FONT_FAMILIES = {
  serif: "Georgia, 'Palatino Linotype', serif",
  sans:  "'Helvetica Neue', Arial, sans-serif",
  mono:  "'Courier New', Courier, monospace",
};

function applyFontFamily(ff) {
  fontFamily = ff;
  localStorage.setItem('fontFamily', ff);
  document.getElementById('chapter-content').style.fontFamily = FONT_FAMILIES[ff] || FONT_FAMILIES.serif;
}

function changeFontSize(delta) {
  fontSize = Math.min(30, Math.max(13, fontSize + delta));
  document.getElementById('chapter-content').style.fontSize = fontSize + 'px';
  localStorage.setItem('fontSize', fontSize);
}

function applyLineHeight(lh) {
  lineHeight = lh;
  localStorage.setItem('lineHeight', lh);
  document.getElementById('chapter-content').style.lineHeight = lh;
}

// Apply saved reading preferences
(function initReadingPrefs() {
  const cc = document.getElementById('chapter-content');
  if (!cc) return;
  cc.style.fontSize    = fontSize + 'px';
  cc.style.lineHeight  = lineHeight;
  cc.style.fontFamily  = FONT_FAMILIES[fontFamily] || FONT_FAMILIES.serif;
})();

// ── TOC ───────────────────────────────────────────────────────────────────────

async function loadTOC() {
  chapters = await fetch(`/api/books/${BOOK_ID}/chapters`).then(r => r.json());
  const list = document.getElementById('toc-list');
  list.innerHTML = chapters.map(ch => {
    const wc = ch.word_count ? ch.word_count.toLocaleString() + ' words' : '';
    const badge = ch.section_type !== 'chapter'
      ? `<span class="toc-section-badge">${esc(ch.section_type)}</span>` : '';
    return `
      <div class="toc-item" data-id="${ch.id}" onclick="openChapter(${ch.id})">
        ${badge}
        <span class="toc-item-title">${esc(ch.title)}</span>
        ${wc ? `<span class="toc-item-meta">${wc}</span>` : ''}
      </div>`;
  }).join('');

  const prog = await fetch(`/api/books/${BOOK_ID}/progress`).then(r => r.json());
  const savedChapterId = Number.parseInt(prog.chapter_id, 10);
  const savedPosition = Number.parseInt(prog.position, 10);
  const hasSavedChapter = chapters.some(ch => ch.id === savedChapterId);
  if (hasSavedChapter) {
    openChapter(savedChapterId, {
      resumePosition: savedPosition,
      persistOpened: false,
      highlightOnLoad: true,
    });
  } else if (chapters.length) {
    openChapter(chapters[0].id, {
      resumePosition: 0,
      persistOpened: false,
      highlightOnLoad: false,
    });
  }

  loadBookmarks();
}

async function openChapter(chapterId, options = {}) {
  closeTocOnMobile();
  const {
    resumePosition = 0,
    persistCurrent = true,
    persistOpened = true,
    highlightOnLoad = false,
  } = options;

  if (persistCurrent && currentChapterId && currentChapterId !== chapterId) {
    sendProgress(currentChapterId, currentSegIdx, { force: true });
  }

  stopPlayback();
  _segCache = new Map();
  segments = [];           // clear immediately so stale segments can't be played
  currentChapterId = chapterId;
  currentSegIdx    = 0;

  document.querySelectorAll('.toc-item').forEach(el => {
    el.classList.toggle('active', +el.dataset.id === chapterId);
  });
  document.querySelector('.toc-item.active')
    ?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

  const ch = await fetch(`/api/books/${BOOK_ID}/chapters/${chapterId}`).then(r => r.json());
  document.getElementById('chapter-title').textContent = ch.title;
  _segDurations = {};
  initMediaSession();

  const wpm = 250;
  const minutes = Math.round((ch.word_count || 0) / wpm);
  const estText = minutes > 0 ? `~${minutes} min read` : '';
  const estEl = document.getElementById('reading-estimate');
  if (estEl) estEl.textContent = estText;

  if (_mobileChapterHeader) {
    _mobileChapterHeader.innerHTML = `<span class="chapter-title-bar">${esc(ch.title)}</span>${minutes > 0 ? `<span class="reading-estimate">${estText}</span>` : ''}`;
    _mobileChapterHeader.classList.remove('hidden');
  }

  const [segs, chars] = await Promise.all([
    fetch(`/api/tts/segments/${BOOK_ID}/${chapterId}`).then(r => r.json()),
    fetch(`/api/books/${BOOK_ID}/characters`).then(r => r.json()).catch(() => []),
  ]);
  segments = segs;
  // Pre-populate durations from API data so resume-offset calculation is accurate
  segs.forEach((s, i) => { if (s.duration_sec) _segDurations[i] = s.duration_sec; });
  charColors = Object.fromEntries(chars.map(c => [c.name, c.color_hex]));
  renderContent(segments);
  _prewarmChapter();

  document.getElementById('chapter-content').scrollTop = 0;
  window.scrollTo(0, 0);

  const startIdx = clampSegmentIndex(resumePosition);
  setCurrentSegment(startIdx, {
    highlight: highlightOnLoad,
    behavior: 'auto',
    save: persistOpened,
  });
  _startBackgroundBuffer(startIdx);
}

// ── Content rendering ─────────────────────────────────────────────────────────

function renderContent(segs) {
  const container = document.getElementById('chapter-content');

  if (!segs || !segs.length) {
    container.innerHTML = '<div class="placeholder-text">No content available.</div>';
    return;
  }

  // Segments never span paragraphs; group consecutive segments that share a
  // paragraph index back into <p> elements so the page keeps its layout.
  const paras = [];
  segs.forEach((seg, i) => {
    const last = paras[paras.length - 1];
    if (last && last.para === seg.paragraph) last.items.push([seg, i]);
    else paras.push({ para: seg.paragraph, items: [[seg, i]] });
  });

  const renderSeg = (seg, i) => {
    if (seg.text === '') {
      return `<span class="section-break" data-idx="${i}" onclick="jumpTo(${i})">* * *</span>`;
    }
    const words = seg.text.split(/(\s+)/);
    const wordSpans = words.map((w, wi) => {
      if (/^\s+$/.test(w)) return w;
      return `<span class="word" data-seg="${i}" data-word="${wi}">${esc(w)}</span>`;
    }).join('');

    const name = seg.character_name;
    const color = name && charColors[name];
    const charAttr = name
      ? ` data-char="${esc(name)}"` + (color ? ` style="--char-color:${esc(color)}"` : '')
      : '';
    const cls = 'sentence' + (seg.is_dialogue ? ' dialogue-sent' : '');
    return `<span class="${cls}" data-idx="${i}"${charAttr} title="${esc(name || 'Narrator')}" onclick="jumpTo(${i})">${wordSpans}</span> `;
  };

  const html = paras.map(p => `<p>${p.items.map(([seg, i]) => renderSeg(seg, i)).join('')}</p>`).join('');
  container.innerHTML = `<div>${html}</div>`;
  renderVoiceLegend(segs);
  applyShowVoices();

  // Restore font prefs (font may be reset by innerHTML)
  container.style.fontSize   = fontSize + 'px';
  container.style.lineHeight = lineHeight;
  container.style.fontFamily = FONT_FAMILIES[fontFamily] || FONT_FAMILIES.serif;
}

function renderVoiceLegend(segs) {
  const legend = document.getElementById('voice-legend');
  const counts = new Map();
  segs.forEach(s => {
    const key = s.character_name || '';
    counts.set(key, (counts.get(key) || 0) + 1);
  });
  const names = [...counts.keys()].filter(Boolean).sort((a, b) => counts.get(b) - counts.get(a));
  const chip = (label, color, n) =>
    `<span class="voice-chip"><span class="voice-swatch"${color ? ` style="--char-color:${esc(color)}"` : ''}></span>${esc(label)} <span class="voice-count">${n}</span></span>`;
  legend.innerHTML = chip('Narrator', '', counts.get('') || 0) +
    names.map(n => chip(n, charColors[n], counts.get(n))).join('');
}

function applyShowVoices() {
  document.getElementById('chapter-content').classList.toggle('show-voices', showVoices);
  document.getElementById('voice-legend').hidden = !showVoices;
  document.getElementById('voices-btn').classList.toggle('active', showVoices);
}

function toggleVoices() {
  showVoices = !showVoices;
  localStorage.setItem('showVoices', showVoices ? '1' : '0');
  applyShowVoices();
  showToast(showVoices ? 'Voices shown' : 'Voices hidden');
}

function jumpTo(idx) {
  if (isPlaying) playSegment(idx);
  else { setCurrentSegment(idx, { highlight: true, save: true }); }
}

// ── Playback ──────────────────────────────────────────────────────────────────

// Kick off generation for segment 0 and, critically, the first segment that
// has no server-cached audio yet (the "frontier").  Firing the frontier at
// chapter-open time gives it the maximum possible lead before playback
// reaches it — without it, the chain only fires the frontier after 3–4
// cached segments play through, which is too late for slow TTS on CPU.
function _prewarmChapter() {
  if (!segments.length) return;
  fetchSegmentData(0);
  const frontier = segments.findIndex(s => !s.has_audio);
  if (frontier > 0) fetchSegmentData(frontier);
}

// Sequentially generates TTS audio for every segment from fromIdx onward.
// Waits until playback is idle before firing each request so it never
// competes with the active playback pipeline at the server TTS lock.
// Skips ahead to currentSegIdx+3 after each playback pause so it stays
// ahead of the cursor when the user is reading without playing.
async function _startBackgroundBuffer(fromIdx) {
  const myId = ++_bufferGenId;
  const myChapterId = currentChapterId;

  for (let i = fromIdx; i < segments.length; i++) {
    if (_bufferGenId !== myId || currentChapterId !== myChapterId) return;

    // Wait out any active playback before sending a new TTS request
    while (isPlaying) {
      await new Promise(r => setTimeout(r, 600));
      if (_bufferGenId !== myId || currentChapterId !== myChapterId) return;
      // Stay ahead of the cursor, not behind it
      i = Math.max(i, currentSegIdx + 3) - 1;
    }

    // Re-check cancellation after the wait
    if (_bufferGenId !== myId || currentChapterId !== myChapterId) return;

    // Skip segments already in the promise cache (fetched by preload or prewarm)
    if (_segCache.has(i)) {
      try { await _segCache.get(i); } catch (_) {}
      await new Promise(r => setTimeout(r, 100));
      continue;
    }

    try {
      await fetchSegmentData(i);
    } catch (_) {}

    // Small throttle between generations to let the event loop breathe
    await new Promise(r => setTimeout(r, 100));
  }
}

function fetchSegmentData(idx) {
  if (!_segCache.has(idx)) {
    const p = fetch('/api/tts/generate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ book_id: BOOK_ID, chapter_id: currentChapterId, segment_index: idx }),
    }).then(r => {
      if (!r.ok) return r.json().then(e => { throw new Error(e.error || `HTTP ${r.status}`); });
      return r.json();
    }).then(data => {
      if (!data.duration_sec) {
        // 0-duration means a bad cache entry — evict and fire one fresh request.
        _segCache.delete(idx);
        const retry = fetch('/api/tts/generate', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ book_id: BOOK_ID, chapter_id: currentChapterId, segment_index: idx }),
        }).then(r => r.ok ? r.json() : r.json().then(e => { throw new Error(e.error || `HTTP ${r.status}`); }));
        retry.catch(() => _segCache.delete(idx));
        _segCache.set(idx, retry);
        return retry;
      }
      return data;
    });
    // Evict on failure so the next call retries rather than re-throwing the cached rejection.
    p.catch(() => _segCache.delete(idx));
    _segCache.set(idx, p);
  }
  return _segCache.get(idx);
}

// ── MSE helpers ───────────────────────────────────────────────────────────────

function _teardownMSE() {
  _sbReady = false;
  _mseNext = 0;
  for (const { rej } of _sbQ) rej(new Error('MSE closed'));
  _sbQ = [];
  _sb = null;
  if (_ms) {
    if (_ms.readyState === 'open') try { _ms.endOfStream(); } catch (_) {}
    _ms = null;
  }
  audio.src = '';
  _segStart = {};
  _segEnd   = {};
  _chapterAudioChId    = null;
  _chapterPrerenderChId = null;
}

function _initMSE(startOffset = 0) {
  _teardownMSE();
  ++_mseGenId;
  const myId = _mseGenId;
  return new Promise(resolve => {
    _ms = new MediaSource();
    const url = URL.createObjectURL(_ms);
    audio.src = url;
    _ms.addEventListener('sourceopen', () => {
      URL.revokeObjectURL(url);
      if (_mseGenId !== myId) return; // was torn down before open fired
      _sb = _ms.addSourceBuffer(MSE_MIME);
      _sb.mode = 'sequence';
      _sb.timestampOffset = startOffset;
      _mseNext = startOffset;
      _sbReady = true;
      resolve();
    }, { once: true });
  });
}

function _drainSbQ() {
  if (!_sbReady || !_sb || _sb.updating || !_sbQ.length) return;
  const { buf, res, rej } = _sbQ.shift();
  const onEnd = () => { res(); _drainSbQ(); };
  const onErr = e  => { rej(e); _drainSbQ(); };
  _sb.addEventListener('updateend', onEnd, { once: true });
  _sb.addEventListener('error',    onErr, { once: true });
  try {
    _sb.appendBuffer(buf);
  } catch (e) {
    _sb.removeEventListener('updateend', onEnd);
    _sb.removeEventListener('error',    onErr);
    rej(e);
    _drainSbQ();
  }
}

function _sbAppend(buf) {
  return new Promise((res, rej) => {
    _sbQ.push({ buf, res, rej });
    _drainSbQ();
  });
}

function _isInBuffer(time) {
  if (!_sb) return false;
  for (let i = 0; i < _sb.buffered.length; i++) {
    if (time >= _sb.buffered.start(i) - 0.1 && time <= _sb.buffered.end(i) + 0.1) return true;
  }
  return false;
}

async function _fetchAndAppendSeg(idx, genId) {
  if (_mseGenId !== genId || !_sbReady) return false;
  let data;
  try { data = await fetchSegmentData(idx); }
  catch (_) { return false; }
  if (_mseGenId !== genId || !_sbReady) return false;

  let resp;
  try { resp = await fetch(`${data.audio_url}/fmp4`); }
  catch (_) { return false; }
  if (!resp.ok || _mseGenId !== genId || !_sbReady) return false;

  let buf;
  try { buf = await resp.arrayBuffer(); }
  catch (_) { return false; }
  if (_mseGenId !== genId || !_sbReady) return false;

  _segStart[idx] = _mseNext;
  _segDurations[idx] = data.duration_sec;

  try { await _sbAppend(buf); }
  catch (_) { delete _segStart[idx]; return false; }

  const actualEnd = _sb?.buffered.length > 0
    ? _sb.buffered.end(_sb.buffered.length - 1)
    : (_mseNext + (data.duration_sec || 0));
  _segEnd[idx]  = actualEnd;
  _mseNext      = actualEnd;

  // Finalize stream after last segment
  if (idx === segments.length - 1 && _ms?.readyState === 'open') {
    try { _ms.endOfStream(); } catch (_) {}
  }
  return true;
}

async function _appendAhead(fromIdx, genId) {
  for (let i = fromIdx; i < Math.min(fromIdx + 3, segments.length); i++) {
    if (_mseGenId !== genId) return;
    if (_segStart[i] !== undefined) continue;
    await _fetchAndAppendSeg(i, genId);
  }
}

function _onSegmentChange(idx) {
  if (idx >= segments.length) return;
  setCurrentSegment(idx, { highlight: true, save: true });
  const seg = segments[idx];
  document.getElementById('pb-character').textContent = seg.character_name || 'Narrator';
  updatePlaybackUI();
  _updateMediaPosition(idx);
  const dur = _segEnd[idx] !== undefined ? _segEnd[idx] - _segStart[idx] : 0;
  startWordHighlight(idx, dur);
  _appendAhead(idx + 1, _mseGenId);
}

async function _schedulePreload(playingIdx) {
  if (!isPlaying) return;
  const nextIdx = playingIdx + 1;
  if (nextIdx >= segments.length) return;

  // Fire N+1 first then N+2 so they arrive at the server TTS lock in the
  // correct order (N+1 queued before N+2).  Two-deep pipeline means a slow
  // TTS generation for N+1 overlaps with N-1's remaining play time *and*
  // N's full play time rather than just N's play time.
  fetchSegmentData(nextIdx);
  if (nextIdx + 1 < segments.length) fetchSegmentData(nextIdx + 1);

  try {
    const data = await fetchSegmentData(nextIdx);
    if (!isPlaying) return;

    if (_preloadIdx !== nextIdx) {
      const sb = _standby();
      sb.src = data.audio_url;
      sb.playbackRate = speedMultiplier;
      _preloadIdx = nextIdx;
      _preloadData = data;
    }
  } catch (e) {
    // silent — playSegment will retry via its own fetchSegmentData call
  }
}

// Returns true if chapter audio was ready and playback was started.
// Returns false if the render is still in progress — caller falls back to
// segment mode while the prerender continues in the background.
async function _playSegmentChapterMode(idx, gen) {
  const charEl    = document.getElementById('pb-character');
  const charLabel = segments[idx]?.character_name || 'Narrator';

  if (_chapterAudioChId !== currentChapterId) {
    const chId = currentChapterId;

    // Only POST once per chapter — avoid spawning a new GPU job on every segment
    // while falling back to WAV during the prerender.
    if (_chapterPrerenderChId === chId) {
      return false; // render already in progress, keep playing via segments
    }

    let resp, data;
    try {
      resp = await fetch(`/api/books/${BOOK_ID}/chapters/${chId}/prerender`, { method: 'POST' });
      data = await resp.json();
    } catch (e) {
      return false; // network error — fall back to segments
    }
    if (gen !== _playGen || !isPlaying) return true;
    if (data.error) { charEl.textContent = data.error; stopPlayback(); return true; }

    if (!data.audio_url) {
      // Render just kicked off — play via segments now, cache will be ready next time
      _chapterPrerenderChId = chId; // mark so subsequent segments don't re-POST
      return false;
    }

    // Already cached — but reject if the prerender is incomplete (some
    // segments failed TTS generation and are missing from the timeline).
    // Incomplete cache plays a truncated chapter and stalls the highlight.
    // Fall back to WAV segments and let the user ↺ regenerate.
    if (data.total_segments !== undefined && data.timeline.length < data.total_segments) {
      console.warn(`Prerender incomplete: ${data.timeline.length}/${data.total_segments} segments. Falling back to WAV.`);
      _chapterPrerenderChId = chId; // don't re-POST
      return false;
    }

    if (gen !== _playGen || !isPlaying) return true;
    _segStart = {};
    _segEnd   = {};
    for (const { idx: i, t_start, t_end } of data.timeline) {
      _segStart[i]     = t_start;
      _segEnd[i]       = t_end;
      _segDurations[i] = t_end - t_start;
    }

    // Silence the standby element so a leftover preloaded WAV segment doesn't
    // fire _onAudioEnded while the chapter file is playing.
    const sb = _standby();
    sb.pause();
    sb.src = '';
    _preloadIdx  = -1;
    _preloadData = null;

    audio.src = data.audio_url;
    audio.playbackRate = speedMultiplier;
    _chapterAudioChId = currentChapterId;
    _preloadNextChapter();
  }

  // Seek to segment start — must wait for metadata if src was just set,
  // otherwise currentTime assignment is silently ignored and playback
  // starts from 0 (wrong segment highlighted, spurious chapter advance).
  const start = _segStart[idx] ?? 0;
  const inRange = audio.currentTime >= start - 0.05 &&
                  audio.currentTime < (_segEnd[idx] ?? start + 1);
  if (!inRange) {
    if (audio.readyState < HTMLMediaElement.HAVE_METADATA) {
      await new Promise(r => {
        audio.addEventListener('loadedmetadata', r, { once: true });
        audio.addEventListener('error', r, { once: true });
      });
    }
    if (gen !== _playGen || !isPlaying) return true;
    audio.currentTime = start;
  }

  charEl.textContent = charLabel;
  const dur = _segEnd[idx] !== undefined ? _segEnd[idx] - _segStart[idx] : 0;
  startWordHighlight(idx, dur);
  audio.playbackRate = speedMultiplier;
  if (audio.paused) await audio.play();
  if (gen !== _playGen) return true;

  _updateMediaPosition(idx);
  return true;
}

async function playSegment(idx) {
  if (idx >= segments.length) { stopPlayback(); return; }

  const gen = ++_playGen;
  isPlaying = true;
  _sleepTimerFired = false; // explicit play clears fired state so auto-advance works again
  updateMediaSession(true);
  setCurrentSegment(idx, { highlight: true, save: true });

  const charEl    = document.getElementById('pb-character');
  const seg       = segments[idx];
  const charLabel = seg.character_name || 'Narrator';

  // ── Full-chapter mode (pre-rendered single file) ──────────────────────────
  // Falls through to segment mode if the render isn't cached yet.
  if (_fullChapterMode) {
    let handled = false;
    try { handled = await _playSegmentChapterMode(idx, gen); }
    catch (e) { if (gen === _playGen) { charEl.textContent = e.message; stopPlayback(); } return; }
    if (handled) return;
    // Not cached yet — continue below to play via segments while it generates
  }

  // ── MSE path ──────────────────────────────────────────────────────────────
  if (_mseOk) {
    try {
      // (Re-)init MSE if needed
      let needInit = !_sbReady;
      let initOffset = 0;
      if (!_sbReady) {
        for (let i = 0; i < idx; i++) {
          initOffset += segments[i]?.duration_sec ?? _segDurations[i] ?? 0;
        }
      } else if (_segStart[idx] !== undefined && !_isInBuffer(_segStart[idx])) {
        // Segment position known but evicted from buffer — restart from there
        needInit   = true;
        initOffset = _segStart[idx];
      }

      if (needInit) {
        await _initMSE(initOffset);
        if (gen !== _playGen || !isPlaying) return;
      }

      const genId = _mseGenId;

      // Append up to idx if not yet in buffer
      if (_segStart[idx] === undefined) {
        charEl.textContent = `⏳ ${charLabel}`;
        const last = Object.keys(_segStart).length
          ? Math.max(...Object.keys(_segStart).map(Number))
          : -1;
        for (let i = last + 1; i <= idx; i++) {
          if (gen !== _playGen || !isPlaying) return;
          if (!await _fetchAndAppendSeg(i, genId)) { stopPlayback(); return; }
        }
      }

      if (gen !== _playGen || !isPlaying) return;

      // Seek to segment start unless already paused inside it
      const start = _segStart[idx];
      if (start !== undefined) {
        const inRange = audio.currentTime >= start &&
                        audio.currentTime <  (_segEnd[idx] ?? start + 1);
        if (!inRange) audio.currentTime = start;
      }

      charEl.textContent = charLabel;
      const dur = _segEnd[idx] !== undefined ? _segEnd[idx] - _segStart[idx] : 0;
      startWordHighlight(idx, dur);
      audio.playbackRate = speedMultiplier;
      if (audio.paused) await audio.play();
      if (gen !== _playGen) return;

      _updateMediaPosition(idx);
      _appendAhead(idx + 1, genId);

    } catch(e) {
      if (gen !== _playGen) return;
      charEl.textContent = e.message;
      stopPlayback();
    }
    return;
  }

  // ── WAV fallback (original dual-buffer path) ──────────────────────────────
  charEl.textContent = charLabel;
  try {
    let data;
    if (_preloadIdx === idx && _preloadData) {
      _swapAudio();
      data      = _preloadData;
      _preloadIdx  = -1;
      _preloadData = null;
    } else {
      charEl.textContent = `⏳ ${charLabel}`;
      data = await fetchSegmentData(idx);
      if (gen !== _playGen || !isPlaying) return;
      charEl.textContent = charLabel;
      audio.src = data.audio_url;
    }
    _segDurations[idx] = data.duration_sec;
    audio.playbackRate = speedMultiplier;
    startWordHighlight(idx, data.duration_sec);
    await audio.play();
    _updateMediaPosition(idx);
    _schedulePreload(idx);
  } catch(e) {
    if (gen !== _playGen) return;
    charEl.textContent = e.message;
    stopPlayback();
  }
}

function _preloadNextChapter() {
  const idx = chapters.findIndex(c => c.id === currentChapterId);
  if (idx < 0 || idx >= chapters.length - 1) return;
  const nextChId = chapters[idx + 1].id;
  fetch(`/api/books/${BOOK_ID}/chapters/${nextChId}/prerender`, { method: 'POST' })
    .catch(() => {});
}

async function _advanceChapter() {
  const idx = chapters.findIndex(c => c.id === currentChapterId);
  if (idx >= 0 && idx < chapters.length - 1) {
    await openChapter(chapters[idx + 1].id, { resumePosition: 0, persistOpened: true });
    // Timer may have fired during openChapter's awaits — don't resume if so
    if (_sleepTimerFired) {
      _sleepTimerFired = false;
    } else {
      playSegment(0);
    }
  } else {
    stopPlayback();
  }
}

function _onAudioEnded(e) {
  // Ignore events from the inactive element — stale _audioB src from WAV
  // preloading can fire 'ended' while _audioA plays the full chapter file.
  if (e && e.target !== audio) return;
  stopWordHighlight();
  if (!isPlaying) return;
  // 'ended' means the whole chapter is done only when:
  //   - MSE SourceBuffer is active (_sbReady) — stream covers the full chapter, OR
  //   - Full-chapter file is loaded (_chapterAudioChId set for this chapter).
  // _mseOk alone is NOT enough: Full mode falls back to WAV segments when the
  // prerender isn't cached yet, leaving _sbReady=false even though _mseOk=true.
  // In that fallback every segment fires 'ended', which must advance a segment,
  // not the whole chapter.
  const wholeChapter = (_mseOk && _sbReady) || (_fullChapterMode && _chapterAudioChId === currentChapterId);
  if (wholeChapter) {
    queueProgressSave(currentChapterId, currentSegIdx);
    if (_sleepOnChapterEnd) { _sleepOnChapterEnd = false; _updateSleepTimerUI(); stopPlayback(); return; }
    _advanceChapter();
    return;
  }
  // WAV segment-by-segment fallback: advance segment, or chapter when done
  const next = currentSegIdx + 1;
  if (next < segments.length) playSegment(next);
  else {
    queueProgressSave(currentChapterId, currentSegIdx);
    if (_sleepOnChapterEnd) { _sleepOnChapterEnd = false; _updateSleepTimerUI(); stopPlayback(); return; }
    _advanceChapter();
  }
}

function _onAudioError() {
  stopWordHighlight();
  if (!isPlaying) return;
  // Evict cached promise so the segment will be re-fetched on retry
  _segCache.delete(currentSegIdx);
  // Skip the broken segment rather than stopping entirely
  const next = currentSegIdx + 1;
  if (next < segments.length) playSegment(next);
  else stopPlayback();
}

_audioA.addEventListener('ended', _onAudioEnded);
_audioB.addEventListener('ended', _onAudioEnded);
_audioA.addEventListener('error', _onAudioError);
_audioB.addEventListener('error', _onAudioError);

function stopPlayback() {
  isPlaying = false;
  _playGen++;
  stopWordHighlight();
  if (_mseOk) {
    _teardownMSE();
  } else {
    _audioA.pause();
    _audioB.pause();
    _audioA.src = '';
    _audioB.src = '';
    audio = _audioA;
    _preloadIdx       = -1;
    _preloadData      = null;
    _chapterAudioChId    = null;
    _chapterPrerenderChId = null;
    _segStart            = {};
    _segEnd              = {};
  }
  if (currentChapterId) queueProgressSave(currentChapterId, currentSegIdx);
  const btn = document.getElementById('btn-play');
  btn.innerHTML = '&#9654;';
  btn.classList.add('paused');
  document.getElementById('pb-character').textContent = '—';
  updatePlaybackUI();
  updateMediaSession(false);
  if ('mediaSession' in navigator) {
    try { navigator.mediaSession.setPositionState({}); } catch (_) {}
  }
}

// ── Word-level highlighting ───────────────────────────────────────────────────

let _wordRafId = null;

function startWordHighlight(segIdx, durationSec) {
  stopWordHighlight();
  // MSE or full-chapter: use the segment's absolute position in the chapter timeline
  const _useTimeline = _mseOk || _fullChapterMode;
  // In timeline mode keep the RAF running even with no duration — we still need
  // to detect segment transitions and retry appends for segments whose TTS was
  // still in progress when we reached them.
  if (!durationSec && !_useTimeline) return;

  const wordEls = document.querySelectorAll(`.word[data-seg="${segIdx}"]`);
  if (!wordEls.length && !_useTimeline) return;

  const n        = wordEls.length;
  const segStart = _useTimeline ? (_segStart[segIdx] ?? audio.currentTime) : audio.currentTime;
  let   retryAt  = 0; // throttle: retry _appendAhead when next segments are missing

  function tick() {
    const t = audio.currentTime;

    // MSE or full-chapter: detect natural transition to the next segment.
    // Scan forward past any segments that haven't been appended yet.
    if (_useTimeline && isPlaying) {
      let firstMissing = -1;
      for (let ni = segIdx + 1; ni < segments.length; ni++) {
        const nextStart = _segStart[ni];
        if (nextStart === undefined) {
          if (firstMissing < 0) firstMissing = ni; // first unappended segment
          continue;
        }
        if (t >= nextStart) { _onSegmentChange(ni); return; }
        break; // next defined segment hasn't started yet
      }
      // If we're waiting on a segment whose TTS wasn't ready, retry the append.
      if (firstMissing >= 0 && Date.now() >= retryAt) {
        retryAt = Date.now() + 2000;
        _appendAhead(firstMissing, _mseGenId);
      }
    }

    if (durationSec && n > 0) {
      const elapsed = t - segStart;
      const wordIdx = Math.min(Math.floor((elapsed / durationSec) * n), n - 1);
      wordEls.forEach((el, i) => el.classList.toggle('playing', i === wordIdx));
    }
    if (isPlaying && !audio.paused) _wordRafId = requestAnimationFrame(tick);
  }

  _wordRafId = requestAnimationFrame(tick);
}

function stopWordHighlight() {
  if (_wordRafId) { cancelAnimationFrame(_wordRafId); _wordRafId = null; }
  document.querySelectorAll('.word.playing').forEach(el => el.classList.remove('playing'));
}

// ── Segment highlighting & auto-scroll ────────────────────────────────────────

function highlightSegment(idx, options = {}) {
  const behavior = options.behavior || 'smooth';
  _ignoreScrollTrackingUntil = Date.now() + (behavior === 'smooth' ? 700 : 150);
  document.querySelectorAll('.sentence, .section-break').forEach(el => {
    const elIdx = parseInt(el.dataset.idx, 10);
    el.classList.toggle('playing', elIdx === idx);
    el.classList.toggle('spoken',  elIdx < idx);
  });
  const active = document.querySelector(`[data-idx="${idx}"]`);
  if (active) active.scrollIntoView({ behavior, block: 'center' });
}

// ── Progress bar ──────────────────────────────────────────────────────────────

function updateProgress() {
  if (!segments.length) return;
  const pct = ((currentSegIdx) / segments.length) * 100;
  const fill = document.getElementById('chapter-progress-fill');
  if (fill) fill.style.width = pct + '%';
}

// ── Playback UI ───────────────────────────────────────────────────────────────

function updatePlaybackUI() {
  const btn  = document.getElementById('btn-play');
  const prog = document.getElementById('pb-progress');
  const fill = document.getElementById('pb-seek-fill');
  if (isPlaying) {
    btn.innerHTML = '&#9646;&#9646;';
    btn.classList.remove('paused');
  } else {
    btn.innerHTML = '&#9654;';
    btn.classList.add('paused');
  }
  if (segments.length) {
    prog.textContent = `${currentSegIdx + 1} / ${segments.length}`;
    if (fill) fill.style.width = `${((currentSegIdx + 1) / segments.length) * 100}%`;
  }
}

// ── Media Session (lock-screen / notification controls) ───────────────────────

function updateMediaSession(playing) {
  if (!('mediaSession' in navigator)) return;
  navigator.mediaSession.playbackState = playing ? 'playing' : 'paused';
}

function _updateMediaPosition(idx) {
  if (!('mediaSession' in navigator)) return;
  // Build position from known durations; unknown segments estimated at 0
  let position = 0;
  for (let i = 0; i < idx; i++) position += _segDurations[i] || 0;
  // Total duration: sum of known durations + 0 for unknown future segments
  let total = 0;
  for (let i = 0; i < segments.length; i++) total += _segDurations[i] || 0;
  if (total > 0) {
    try {
      navigator.mediaSession.setPositionState({
        duration: total,
        playbackRate: speedMultiplier,
        position: Math.min(position, total),
      });
    } catch (_) {}
  }
}

function initMediaSession() {
  if (!('mediaSession' in navigator)) return;
  const chTitle = document.getElementById('chapter-title')?.textContent || 'Chapter';
  navigator.mediaSession.metadata = new MediaMetadata({
    title: chTitle,
    artist: document.querySelector('.book-title-short')?.textContent || '',
    album: 'Auris',
  });
  navigator.mediaSession.setActionHandler('play', () => {
    if (!isPlaying) playSegment(currentSegIdx);
  });
  navigator.mediaSession.setActionHandler('pause', () => {
    if (isPlaying) { isPlaying = false; stopWordHighlight(); audio.pause(); updatePlaybackUI(); updateMediaSession(false); }
  });
  navigator.mediaSession.setActionHandler('previoustrack', () => {
    const idx = chapters.findIndex(c => c.id === currentChapterId);
    if (idx > 0) openChapter(chapters[idx - 1].id);
  });
  navigator.mediaSession.setActionHandler('nexttrack', () => {
    const idx = chapters.findIndex(c => c.id === currentChapterId);
    if (idx >= 0 && idx < chapters.length - 1) openChapter(chapters[idx + 1].id);
  });
  navigator.mediaSession.setActionHandler('seekbackward', () => {
    const prev = Math.max(currentSegIdx - 1, 0);
    if (isPlaying) playSegment(prev); else setCurrentSegment(prev, { highlight: true, save: true });
  });
  navigator.mediaSession.setActionHandler('seekforward', () => {
    const next = Math.min(currentSegIdx + 1, segments.length - 1);
    if (isPlaying) playSegment(next); else setCurrentSegment(next, { highlight: true, save: true });
  });
  navigator.mediaSession.setActionHandler('stop', () => stopPlayback());
}

// ── Sleep timer ───────────────────────────────────────────────────────────────

function _clearSleepTimer() {
  clearTimeout(_sleepTimerId);  _sleepTimerId = null;
  clearInterval(_sleepCountdownId); _sleepCountdownId = null;
}

function setSleepTimer(minutes) {
  _clearSleepTimer();
  _sleepTimerEnd     = 0;
  _sleepOnChapterEnd = false;
  _sleepTimerFired   = false;

  if (minutes === 0) {
    _updateSleepTimerUI();
    return;
  }
  if (minutes < 0) {                     // special value: end of chapter
    _sleepOnChapterEnd = true;
  } else {
    _sleepTimerEnd = Date.now() + minutes * 60_000;
    _sleepTimerId  = setTimeout(_onSleepTimerFired, minutes * 60_000);
    _sleepCountdownId = setInterval(_updateSleepTimerUI, 1000);
  }
  _updateSleepTimerUI();
  document.getElementById('sleep-dropdown').classList.add('hidden');
}

function _onSleepTimerFired() {
  _clearSleepTimer();
  _sleepTimerEnd   = 0;
  _sleepTimerFired = true;
  stopPlayback();
  _updateSleepTimerUI();
}

function _updateSleepTimerUI() {
  const btn = document.getElementById('sleep-btn');
  if (!btn) return;
  if (_sleepOnChapterEnd) {
    btn.textContent = '☽ ch';
    btn.classList.add('sleep-active');
  } else if (_sleepTimerEnd > 0) {
    const ms  = Math.max(0, _sleepTimerEnd - Date.now());
    const m   = Math.floor(ms / 60_000);
    const s   = Math.floor((ms % 60_000) / 1000);
    btn.textContent = `${m}:${String(s).padStart(2, '0')}`;
    btn.classList.add('sleep-active');
  } else {
    btn.textContent = '☽';
    btn.classList.remove('sleep-active');
  }
}

document.getElementById('sleep-btn').onclick = () => {
  document.getElementById('sleep-dropdown').classList.toggle('hidden');
};
document.addEventListener('click', (e) => {
  if (!e.target.closest('.sleep-menu-wrap'))
    document.getElementById('sleep-dropdown').classList.add('hidden');
  if (!e.target.closest('.export-menu-wrap'))
    document.getElementById('export-dropdown').classList.add('hidden');
});

// ── Controls ──────────────────────────────────────────────────────────────────

document.getElementById('btn-play').onclick = () => {
  if (isPlaying) {
    isPlaying = false;
    stopWordHighlight();
    audio.pause();
    updatePlaybackUI();
    queueProgressSave(currentChapterId, currentSegIdx);
  } else {
    playSegment(currentSegIdx);
  }
};

document.getElementById('btn-stop').onclick = () => {
  stopPlayback();
  // True stop: go back to the beginning of the chapter, not just pause in place.
  if (currentChapterId) setCurrentSegment(0, { highlight: true, behavior: 'smooth', save: true });
};

document.getElementById('btn-next-seg').onclick = () => {
  const next = Math.min(currentSegIdx + 1, segments.length - 1);
  if (isPlaying) playSegment(next);
  else { setCurrentSegment(next, { highlight: true, save: true }); }
};

document.getElementById('btn-prev-seg').onclick = () => {
  const prev = Math.max(currentSegIdx - 1, 0);
  if (isPlaying) playSegment(prev);
  else { setCurrentSegment(prev, { highlight: true, save: true }); }
};

document.getElementById('btn-prev-ch').onclick = () => {
  const idx = chapters.findIndex(c => c.id === currentChapterId);
  if (idx > 0) openChapter(chapters[idx - 1].id);
};
document.getElementById('btn-next-ch').onclick = () => {
  const idx = chapters.findIndex(c => c.id === currentChapterId);
  if (idx >= 0 && idx < chapters.length - 1) openChapter(chapters[idx + 1].id);
};

document.getElementById('pb-seek-bar').onclick = (e) => {
  if (!segments.length) return;
  const bar = e.currentTarget;
  const frac = Math.max(0, Math.min(1, e.offsetX / bar.offsetWidth));
  const targetIdx = Math.round(frac * (segments.length - 1));
  if (isPlaying) playSegment(targetIdx);
  else setCurrentSegment(targetIdx, { highlight: true, save: true });
};

document.getElementById('speed-slider').oninput = function() {
  speedMultiplier = parseFloat(this.value);
  localStorage.setItem('speedMultiplier', speedMultiplier);
  document.getElementById('speed-val').textContent = speedMultiplier.toFixed(1) + '×';
  audio.playbackRate = speedMultiplier;
  if (!_mseOk) _audioB.playbackRate = speedMultiplier;
};
(function initSpeedUI() {
  const slider = document.getElementById('speed-slider');
  if (slider) slider.value = speedMultiplier;
  const label = document.getElementById('speed-val');
  if (label) label.textContent = speedMultiplier.toFixed(1) + '×';
})();

(function initModeToggle() {
  const btn       = document.getElementById('mode-btn');
  const reBtn     = document.getElementById('rerender-btn');
  if (!btn) return;

  function syncBtn() {
    btn.textContent = _fullChapterMode ? 'Full' : 'Seg';
    btn.classList.toggle('active', _fullChapterMode);
    if (reBtn) reBtn.classList.toggle('hidden', !_fullChapterMode);
  }
  syncBtn();

  btn.onclick = () => {
    const wasPlaying = isPlaying;
    const idx = currentSegIdx;
    if (wasPlaying) stopPlayback();
    _fullChapterMode = !_fullChapterMode;
    localStorage.setItem('fullChapterMode', _fullChapterMode ? '1' : '0');
    syncBtn();
    if (wasPlaying) playSegment(idx);
  };

  if (reBtn) {
    reBtn.onclick = async () => {
      if (!currentChapterId) return;
      reBtn.disabled = true;
      try {
        await fetch(`/api/books/${BOOK_ID}/chapters/${currentChapterId}/prerender`, { method: 'DELETE' });
      } catch (_) {}
      const wasPlaying = isPlaying;
      const idx = currentSegIdx;
      stopPlayback(); // clears _chapterAudioChId, _chapterPrerenderChId, _segStart, _segEnd
      reBtn.disabled = false;
      if (wasPlaying) playSegment(idx);
    };
  }
})();

// ── Sidebar toggle ────────────────────────────────────────────────────────────

const _tocSidebar         = document.getElementById('toc-sidebar');
const _tocBackdrop        = document.getElementById('toc-backdrop');
const _mobileChapterHeader = document.getElementById('mobile-chapter-header');
const _topnav             = document.querySelector('.topnav');

document.getElementById('toc-toggle').onclick = () => {
  const isMobile = window.innerWidth <= MOBILE_BREAKPOINT;
  _tocSidebar.classList.toggle('collapsed');
  if (isMobile && _tocBackdrop) _tocBackdrop.classList.toggle('hidden', _tocSidebar.classList.contains('collapsed'));
};

function closeTocOnMobile() {
  if (window.innerWidth > MOBILE_BREAKPOINT) return;
  _tocSidebar.classList.add('collapsed');
  if (_tocBackdrop) _tocBackdrop.classList.add('hidden');
}

function toggleBookmarkPanel() {
  document.getElementById('bookmarks-panel').classList.toggle('collapsed');
}

// ── Progress persistence ──────────────────────────────────────────────────────

function updateProgressFromViewport() {
  if (!segments.length || isPlaying || Date.now() < _ignoreScrollTrackingUntil) return;

  const sentenceEls = document.querySelectorAll('.sentence');
  if (!sentenceEls.length) return;

  const viewportCenter = window.innerHeight * 0.35;
  let bestIdx = currentSegIdx;
  let bestDistance = Number.POSITIVE_INFINITY;

  sentenceEls.forEach((el, idx) => {
    const rect = el.getBoundingClientRect();
    const mid = rect.top + (rect.height / 2);
    const distance = Math.abs(mid - viewportCenter);
    if (distance < bestDistance) {
      bestDistance = distance;
      bestIdx = idx;
    }
  });

  if (bestIdx !== currentSegIdx) {
    currentSegIdx = bestIdx;
    updatePlaybackUI();
    updateProgress();
    queueProgressSave(currentChapterId, currentSegIdx);
  }
}

function scheduleViewportProgressUpdate() {
  if (!segments.length || isPlaying || Date.now() < _ignoreScrollTrackingUntil) return;
  clearTimeout(_scrollProgressTimer);
  _scrollProgressTimer = setTimeout(updateProgressFromViewport, 120);
}

// ── Bookmarks ─────────────────────────────────────────────────────────────────

let _bookmarks = [];

async function loadBookmarks() {
  _bookmarks = await fetch(`/api/books/${BOOK_ID}/bookmarks`).then(r => r.json());
  renderBookmarks();
}

function renderBookmarks() {
  const list = document.getElementById('bookmark-list');
  if (!_bookmarks.length) {
    list.innerHTML = '<div style="padding:16px;font-size:.8rem;color:var(--text3);font-style:italic">No bookmarks yet.</div>';
    return;
  }
  list.innerHTML = _bookmarks.map(bm => `
    <div class="bookmark-item" onclick="gotoBookmark(${bm.chapter_id}, ${bm.segment_index})">
      <div class="bookmark-text">${esc(bm.text_excerpt || bm.label || '(no excerpt)')}</div>
      <div class="bookmark-loc">${esc(bm.chapter_title || '')} &middot; seg ${bm.segment_index + 1}</div>
      <button class="bookmark-del" onclick="removeBookmark(event,${bm.id})">&times;</button>
    </div>`).join('');
}

async function addBookmark() {
  if (!currentChapterId) { showToast('Open a chapter first.'); return; }
  const seg = segments[currentSegIdx];
  const excerpt = seg ? seg.text.slice(0, 120) : '';
  const r = await fetch(`/api/books/${BOOK_ID}/bookmarks`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      chapter_id:    currentChapterId,
      segment_index: currentSegIdx,
      text_excerpt:  excerpt,
    }),
  });
  if (r.ok) {
    showToast('Bookmark added');
    loadBookmarks();
    const btn = document.getElementById('bookmark-btn');
    btn.textContent = '★';
    setTimeout(() => { btn.textContent = '☆'; }, 1500);
  }
}

async function removeBookmark(e, id) {
  e.stopPropagation();
  await fetch(`/api/books/${BOOK_ID}/bookmarks/${id}`, { method: 'DELETE' });
  loadBookmarks();
}

function gotoBookmark(chapterId, segIdx) {
  if (chapterId !== currentChapterId) {
    openChapter(chapterId, {
      resumePosition: segIdx,
      persistCurrent: true,
      persistOpened: true,
      highlightOnLoad: true,
    });
  } else {
    jumpTo(segIdx);
  }
}

// ── Export ────────────────────────────────────────────────────────────────────

document.getElementById('export-btn').onclick = () => {
  document.getElementById('export-dropdown').classList.toggle('hidden');
};

document.getElementById('do-export-btn').onclick = async () => {
  if (!currentChapterId) { showToast('Open a chapter first.'); return; }

  const mode      = document.querySelector('input[name="exp-mode"]:checked').value;
  const audioFmt  = document.querySelector('input[name="exp-audio"]:checked').value;
  const subInput  = document.querySelector('input[name="exp-sub"]:checked');
  const subFmt    = subInput ? subInput.value : 'srt';

  const status    = document.getElementById('export-status');
  const progWrap  = document.getElementById('export-progress-wrap');
  const progFill  = document.getElementById('export-progress-fill');
  const doBtn     = document.getElementById('do-export-btn');

  doBtn.disabled = true;
  progWrap.classList.add('active');
  progFill.style.width = '0%';
  status.textContent = 'Starting export…';

  let url;
  if (mode === 'chapter')          url = `/api/books/${BOOK_ID}/export/chapter/${currentChapterId}`;
  else if (mode === 'chapterwise') url = `/api/books/${BOOK_ID}/export/chapterwise`;
  else                             url = `/api/books/${BOOK_ID}/export/full`;

  const finish = (msg) => {
    status.textContent = msg;
    setTimeout(() => {
      progWrap.classList.remove('active');
      progFill.style.width = '0%';
      doBtn.disabled = false;
    }, 2000);
  };

  try {
    const r = await fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ audio_fmt: audioFmt, sub_fmt: subFmt }),
    });
    const d = await r.json();
    if (d.error) { finish(d.error); return; }

    const jobId = d.job_id;

    while (true) {
      await new Promise(res => setTimeout(res, 800));
      let sr;
      try { sr = await fetch(`/api/export/status/${jobId}`).then(r => r.json()); }
      catch(_) { continue; }

      if (sr.total > 0) {
        const pct = Math.min(Math.round((sr.done / sr.total) * 95), 95);
        progFill.style.width = pct + '%';
      }
      status.textContent = sr.message || 'Working…';

      if (sr.state === 'complete') {
        progFill.style.width = '100%';
        const res = sr.result;
        if (res.zip_download) {
          window.location.href = res.zip_download;
        } else {
          if (res.audio_download)    window.open(res.audio_download);
          if (res.subtitle_download) setTimeout(() => window.open(res.subtitle_download), 500);
        }
        finish('Done. Downloading…');
        break;
      } else if (sr.state === 'failed') {
        finish('Export failed: ' + (sr.error || 'Unknown error'));
        break;
      }
    }
  } catch(e) {
    finish(e.message);
  }
};

document.getElementById('preload-btn').onclick = async () => {
  if (!currentChapterId) { showToast('Open a chapter first.'); return; }
  const status = document.getElementById('preload-status');
  const btn    = document.getElementById('preload-btn');
  const idx    = chapters.findIndex(c => c.id === currentChapterId);
  if (idx < 0) { showToast('Chapter not found.'); return; }
  const remaining = chapters.slice(idx);
  btn.disabled = true;
  status.textContent = `Queuing ${remaining.length} chapter${remaining.length === 1 ? '' : 's'}…`;
  let queued = 0, cached = 0;
  await Promise.all(remaining.map(async ch => {
    try {
      const d = await fetch(`/api/books/${BOOK_ID}/chapters/${ch.id}/prerender`, { method: 'POST' }).then(r => r.json());
      if (d.cached) cached++; else queued++;
    } catch (_) {}
  }));
  btn.disabled = false;
  const parts = [];
  if (queued) parts.push(`${queued} queued`);
  if (cached) parts.push(`${cached} already cached`);
  status.textContent = parts.join(', ') || 'Done.';
};

// ── Keyboard shortcuts ────────────────────────────────────────────────────────

document.addEventListener('keydown', e => {
  const tag = document.activeElement.tagName.toLowerCase();
  if (tag === 'input' || tag === 'textarea' || tag === 'select') return;

  switch(e.key) {
    case ' ':
      e.preventDefault();
      document.getElementById('btn-play').click();
      break;
    case 'ArrowLeft':
      e.preventDefault();
      document.getElementById('btn-prev-seg').click();
      break;
    case 'ArrowRight':
      e.preventDefault();
      document.getElementById('btn-next-seg').click();
      break;
    case 'b': case 'B':
      addBookmark();
      break;
    case 't': case 'T':
      cycleTheme();
      break;
    case 'v': case 'V':
      toggleVoices();
      break;
    case 'c': case 'C':
      document.getElementById('toc-toggle').click();
      break;
    case 'm': case 'M':
      toggleBookmarkPanel();
      break;
    case '?':
      showShortcuts();
      break;
    case 'Escape':
      hideShortcuts();
      document.getElementById('export-dropdown').classList.add('hidden');
      break;
  }
});

function showShortcuts()  { document.getElementById('shortcuts-overlay').classList.remove('hidden'); }
function hideShortcuts()  { document.getElementById('shortcuts-overlay').classList.add('hidden'); }

// ── Toasts ────────────────────────────────────────────────────────────────────

function showToast(msg, type = 'ok') {
  const tc   = document.getElementById('toast-container');
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.textContent = msg;
  tc.appendChild(toast);
  setTimeout(() => toast.remove(), 2500);
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function esc(s) {
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Init ──────────────────────────────────────────────────────────────────────

document.getElementById('chapter-content').addEventListener('scroll', scheduleViewportProgressUpdate, { passive: true });
window.addEventListener('pagehide', () => flushProgressSave({ useBeacon: true, force: true }));
window.addEventListener('beforeunload', () => flushProgressSave({ useBeacon: true, force: true }));
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    flushProgressSave({ useBeacon: true, force: true });
  }
});

// iOS Safari blocks audio.play() called after an async gap from a gesture.
// Touch this element in a user-gesture context so iOS grants page-level audio permission.
document.addEventListener('touchstart', function _unlockAudio() {
  const els = _mseOk ? [_audioA] : [_audioA, _audioB];
  els.forEach(a => {
    a.muted = true;
    a.play().then(() => { a.pause(); a.currentTime = 0; a.muted = false; }).catch(() => { a.muted = false; });
  });
}, { once: true, passive: true });

if (_topnav) {
  const _syncTopnavHeight = () => document.documentElement.style.setProperty('--topnav-height', _topnav.offsetHeight + 'px');
  _syncTopnavHeight();
  new ResizeObserver(_syncTopnavHeight).observe(_topnav);
}

closeTocOnMobile();

loadTOC();
