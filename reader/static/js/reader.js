const BOOK_ID = window.BOOK_ID;
const MOBILE_BREAKPOINT = 768;

// ── State ─────────────────────────────────────────────────────────────────────
let chapters        = [];
// Everything that belongs to the chapter being read. openChapter replaces the whole object, so
// anything that captured the previous chapter (an in-flight request, the buffer loop) keeps
// working on that one and can never write into the new chapter.
//   id, segments (see /api/tts/segments), durations (segIdx -> seconds)
//   cache  segIdx -> Promise<{audio_url, duration_sec, ...}>: the single source of truth for "this
//          segment's audio is ready or being generated" (see _seedAudioCache, fetchSegmentData)
let chapter = { id: null, segments: [], cache: new Map(), durations: {} };
let currentSegIdx   = 0;
let isPlaying       = false;
let speedMultiplier = parseFloat(localStorage.getItem('speedMultiplier') || '1.0');
let fontSize        = parseInt(localStorage.getItem('fontSize') || '18');
let fontFamily      = localStorage.getItem('fontFamily') || 'serif';
let lineHeight      = parseFloat(localStorage.getItem('lineHeight') || '1.9');
let currentTheme    = localStorage.getItem('theme') || 'night';
let showVoices      = localStorage.getItem('showVoices') !== '0';
let showGutter      = localStorage.getItem('showGutter') !== '0';   // speaker names in the margin (wide windows)
let charColors      = {};   // character name -> #RRGGBB
let charList        = [];   // the book's characters in Voice Studio order (merged ones follow their group's head)

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
let _followPlayback = true;   // playback scrolls to keep the current sentence centred; manual scrolling pauses this

// Two audio elements for gapless double-buffering
const _audioA = document.getElementById('tts-audio');
const _audioB = (() => { const a = new Audio(); a.preload = 'auto'; return a; })();
let audio = _audioA; // currently active (playing) element

const sleep = ms => new Promise(r => setTimeout(r, ms));

// Which segment index is pre-buffered in the standby element, and its data
let _preloadIdx = -1;
let _preloadData = null;

// Monotonic counter — incremented on every new playSegment and on stopPlayback.
// Each playSegment captures its generation at entry; stale async continuations
// bail out when their generation no longer matches the current one.
let _playGen = 0;
let _seekingIntentionally = false; // suppress _onAudioSeeked during our own seeks

// ── Sleep timer ───────────────────────────────────────────────────────────────
let _sleepTimerId       = null;  // setTimeout id
let _sleepTimerEnd      = 0;     // absolute ms timestamp when timer fires (0 = off)
let _sleepOnChapterEnd  = false; // stop at the next chapter boundary instead
let _sleepTimerFired    = false; // set when timer fires; cleared by setSleepTimer / playSegment
let _sleepCountdownId   = null;  // setInterval for the countdown display
let _autoAdvancing      = false; // true while _advanceChapter awaits openChapter; keeps OS lock-screen alive

// Cancellation token for the background buffer loop — incremented on each
// chapter open so the previous loop exits without touching the new chapter.
let _bufferGenId = 0;
let _bufferWake = null;   // resolves the buffer loop's current sleep (see _kickBuffer)

function _standby() { return audio === _audioA ? _audioB : _audioA; }
function _swapAudio() { audio = (audio === _audioA ? _audioB : _audioA); }

function clampSegmentIndex(idx, segList = chapter.segments) {
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

function queueProgressSave(chapterId = chapter.id, position = currentSegIdx) {
  if (!chapterId) return;
  clearTimeout(_progressSaveTimer);
  _progressSaveTimer = setTimeout(() => {
    sendProgress(chapterId, position);
  }, 250);
}

function flushProgressSave(options = {}) {
  clearTimeout(_progressSaveTimer);
  if (chapter.id) {
    sendProgress(chapter.id, currentSegIdx, options);
  }
}

// The playback-bar label doubles as the "correct speaker" button.
function updateSpeakerLabel() {
  const seg = chapter.segments[currentSegIdx];
  document.getElementById('pb-character').textContent = seg && seg.text ? (seg.character_name || 'Narrator') : '—';
}

function setCurrentSegment(idx, options = {}) {
  const { highlight = false, behavior = 'smooth', save = true } = options;
  currentSegIdx = clampSegmentIndex(idx);
  _kickBuffer();
  updateSpeakerLabel();
  if (highlight) {
    highlightSegment(currentSegIdx, { behavior });
  }
  updatePlaybackUI();
  updateProgress();
  if (save) {
    queueProgressSave(chapter.id, currentSegIdx);
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

  // A link like /reader/1?chapter=35&segment=12 (from the corrections page) opens that sentence.
  const params = new URLSearchParams(location.search);
  const linkedChapterId = Number.parseInt(params.get('chapter'), 10);
  if (chapters.some(ch => ch.id === linkedChapterId)) {
    history.replaceState(null, '', location.pathname);        // so a reload goes back to normal
    openChapter(linkedChapterId, {
      resumePosition: Number.parseInt(params.get('segment'), 10) || 0,
      persistOpened: false,
      highlightOnLoad: true,
    });
    loadBookmarks();
    return;
  }

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

  if (persistCurrent && chapter.id && chapter.id !== chapterId) {
    sendProgress(chapter.id, currentSegIdx, { force: true });
  }

  stopPlayback();
  // A fresh object: nothing from the previous chapter can leak into this one, and its
  // stale segments can't be played while this one loads.
  const ch = { id: chapterId, segments: [], cache: new Map(), durations: {} };
  chapter = ch;
  currentSegIdx    = 0;

  document.querySelectorAll('.toc-item').forEach(el => {
    el.classList.toggle('active', +el.dataset.id === chapterId);
  });
  document.querySelector('.toc-item.active')
    ?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

  const meta = await fetch(`/api/books/${BOOK_ID}/chapters/${chapterId}`).then(r => r.json());
  if (chapter !== ch) return;      // a newer open superseded this one: don't overwrite it
  document.getElementById('chapter-title').textContent = meta.title;
  initMediaSession();

  const wpm = 250;
  const minutes = Math.round((meta.word_count || 0) / wpm);
  const estText = minutes > 0 ? `~${minutes} min read` : '';
  const estEl = document.getElementById('reading-estimate');
  if (estEl) estEl.textContent = estText;

  if (_mobileChapterHeader) {
    _mobileChapterHeader.innerHTML = `<span class="chapter-title-bar">${esc(meta.title)}</span>${minutes > 0 ? `<span class="reading-estimate">${estText}</span>` : ''}`;
    _mobileChapterHeader.classList.remove('hidden');
  }

  const [segs, chars] = await Promise.all([
    fetch(`/api/tts/segments/${BOOK_ID}/${chapterId}`).then(r => r.json()),
    fetch(`/api/books/${BOOK_ID}/characters`).then(r => r.json()).catch(() => []),
  ]);
  if (chapter !== ch) return;
  ch.segments = segs;
  _seedAudioCache(segs);
  // Pre-populate durations from API data so resume-offset calculation is accurate
  segs.forEach((s, i) => { if (s.duration_sec) ch.durations[i] = s.duration_sec; });
  charList = chars;
  charColors = Object.fromEntries(chars.map(c => [c.name, c.color_hex]));
  renderContent(segs);

  document.getElementById('chapter-content').scrollTop = 0;
  window.scrollTo(0, 0);

  const startIdx = clampSegmentIndex(resumePosition);
  setCurrentSegment(startIdx, {
    highlight: highlightOnLoad,
    behavior: 'auto',
    save: persistOpened,
  });
  if (!_fullChapterMode) _startBackgroundBuffer();
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

  const renderSeg = (seg, i, startsRun = false) => {
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
    const cls = 'sentence' + (seg.is_dialogue ? ' dialogue-sent' : '') + (seg.overridden ? ' overridden' : '')
              + (startsRun ? ' speaker-start' : '');
    const tip = (name || 'Narrator') + (seg.overridden ? ' (corrected)' : '');
    return `<span class="${cls}" data-idx="${i}"${charAttr} title="${esc(tip)}" onclick="jumpTo(${i}, event)" ondblclick="playFrom(${i})" oncontextmenu="openSpeakerEditor(${i}); return false;">${wordSpans}</span> `;
  };

  // The margin names a speaker where their lines begin in a paragraph, not on every sentence.
  const html = paras.map(p => {
    let lastSpeaker = null;
    const sentences = p.items.map(([seg, i]) => {
      const name = seg.character_name;
      const startsRun = !!name && name !== lastSpeaker;
      if (name) lastSpeaker = name;
      return renderSeg(seg, i, startsRun);
    }).join('');
    return `<p>${sentences}</p>`;
  }).join('');
  container.innerHTML = `<div>${html}</div>`;
  renderVoiceLegend(segs);
  applyShowVoices();
  applyShowGutter();

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

function applyShowGutter() {
  document.getElementById('chapter-content').classList.toggle('show-gutter', showGutter);
  document.getElementById('gutter-btn').classList.toggle('active', showGutter);
}

function toggleGutter() {
  showGutter = !showGutter;
  localStorage.setItem('showGutter', showGutter ? '1' : '0');
  applyShowGutter();
  showToast(showGutter ? 'Speaker names in the margin' : 'Margin names hidden');
}

function toggleVoices() {
  showVoices = !showVoices;
  localStorage.setItem('showVoices', showVoices ? '1' : '0');
  applyShowVoices();
  showToast(showVoices ? 'Voices shown' : 'Voices hidden');
}

// Double-click a sentence: start playing from it, whether or not anything was playing.
function playFrom(idx) {
  window.getSelection()?.removeAllRanges();      // a double-click also selects the word under it
  resumeFollow({ scroll: false });
  if (isPlaying && currentSegIdx === idx) return;   // its first click already started it
  playSegment(idx);
}

function jumpTo(idx, event) {
  if (event?.detail > 1) return;       // the second click of a double-click: playFrom() handles it
  resumeFollow({ scroll: false });
  if (isPlaying) playSegment(idx);
  else { setCurrentSegment(idx, { highlight: true, save: true }); }
}

// ── Playback ──────────────────────────────────────────────────────────────────

// While the user is reading (not playing), keep the next BUFFER_AHEAD segments after
// the cursor generated, following the cursor as it moves. It never walks the whole
// chapter, makes no request for segments that already have audio, and is idle
// (no requests) when the window is full. During playback it stands aside: playback
// preloads its own next segments, and two requests would compete for the TTS lock.
const BUFFER_AHEAD = 10;
const BUFFER_RETRY_MS = 5000;     // pause after a failed generation before trying again

// The buffer sleeps with no timer at all until something it depends on changes, and
// _kickBuffer() wakes it: the cursor moved, playback stopped, the tab became visible,
// or a chapter opened. (A timer-polling loop costs battery on a phone even when idle.)
function _kickBuffer() { _bufferWake?.(); }
const _bufferIdle = () => new Promise(resolve => { _bufferWake = resolve; });

async function _startBackgroundBuffer() {
  const myId = ++_bufferGenId;
  const ch = chapter;
  _kickBuffer();                                       // let a previous chapter's loop notice and exit

  while (_bufferGenId === myId && chapter === ch) {
    // Look for work and go to sleep in the same step, so a wake-up can never slip in between.
    let next = -1;
    if (!isPlaying && !document.hidden) {              // while playing, playback preloads its own segments
      const end = Math.min(ch.segments.length, currentSegIdx + BUFFER_AHEAD + 1);
      for (let i = currentSegIdx; i < end; i++) {
        if (!ch.cache.has(i)) { next = i; break; }
      }
    }
    if (next < 0) { await _bufferIdle(); continue; }

    try {
      await fetchSegmentData(next, ch);
      await sleep(100);                                // let the event loop breathe between generations
    } catch (_) {
      await sleep(BUFFER_RETRY_MS);                    // e.g. the model is still loading: don't hammer it
    }
  }
}

// Segments the server already has audio for need no request: record what the generate
// endpoint would return for them (see generate_segment in app.py) from the segment list.
function _seedAudioCache(segs) {
  segs.forEach((seg, idx) => {
    if (!seg.audio_url || !seg.duration_sec) return;       // audio_url is only sent for current audio
    chapter.cache.set(idx, Promise.resolve({
      audio_url: seg.audio_url, duration_sec: seg.duration_sec, text: seg.text,
      character_name: seg.character_name, is_dialogue: seg.is_dialogue, segment_index: idx, cached: true,
    }));
  });
}

// Audio that failed to load: forget it, so the next lookup asks the server (which regenerates it).
function _forgetAudio(idx, ch = chapter) {
  ch.cache.delete(idx);
}

function fetchSegmentData(idx, ch = chapter) {          // ch: the chapter this request belongs to
  if (!ch.cache.has(idx)) {
    const post = () => fetch('/api/tts/generate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ book_id: BOOK_ID, chapter_id: ch.id, segment_index: idx }),
    }).then(r => r.ok ? r.json() : r.json().then(e => { throw new Error(e.error || `HTTP ${r.status}`); }));

    // A 0-duration reply means a bad cache entry: ask once more for a fresh one.
    const p = post().then(d => d.duration_sec ? d : post());
    // Evict on failure so the next call retries rather than re-throwing the cached rejection.
    p.catch(() => { if (ch.cache.get(idx) === p) ch.cache.delete(idx); });
    ch.cache.set(idx, p);
  }
  return ch.cache.get(idx);
}

// ── MSE helpers ───────────────────────────────────────────────────────────────

function _awaitSeek() {
  return new Promise(r => {
    audio.addEventListener('seeked', r, { once: true });
    audio.addEventListener('error',  r, { once: true });
  });
}

function _segDur(idx) {
  return _segEnd[idx] !== undefined ? _segEnd[idx] - _segStart[idx] : 0;
}

function _teardownMSE({ keepSrc = false } = {}) {
  _sbReady = false;
  _mseNext = 0;
  for (const { rej } of _sbQ) rej(new Error('MSE closed'));
  _sbQ = [];
  _sb = null;
  if (_ms) {
    if (_ms.readyState === 'open') try { _ms.endOfStream(); } catch (_) {}
    _ms = null;
  }
  if (!keepSrc && !_autoAdvancing) audio.src = '';
  _segStart = {};
  _segEnd   = {};
  _chapterAudioChId    = null;
  _chapterPrerenderChId = null;
}

function _initMSE(startOffset = 0) {
  _teardownMSE({ keepSrc: true });
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

// Appends are serialized through this chain. _fetchAndAppendSeg is reached from
// several places at once (playSegment, _onSegmentChange, the RAF retry), and
// each used to check `_segStart[idx] === undefined` *before* its fetch awaits,
// so two callers could both append the same segment — the stream then played
// A B B C C … and the timeline (_segStart/_segEnd) drifted a segment behind
// the audio. Fetching stays parallel; the dedupe check + append happen in turn.
let _appendChain = Promise.resolve(true);

async function _fetchAndAppendSeg(idx, genId) {
  if (_mseGenId !== genId || !_sbReady) return false;
  if (_segStart[idx] !== undefined) return true;
  const ch = chapter;
  let data, buf;
  try {
    data = await fetchSegmentData(idx, ch);
    if (_mseGenId !== genId || !_sbReady) return false;
    const resp = await fetch(`${data.audio_url}/fmp4`);
    if (_mseGenId !== genId || !_sbReady) return false;
    if (!resp.ok) { _forgetAudio(idx, ch); return false; }   // e.g. the file was deleted: re-request next time
    buf = await resp.arrayBuffer();
  } catch (_) { return false; }

  const run = () => _appendSeg(idx, genId, data, buf, ch);
  return (_appendChain = _appendChain.then(run, run));
}

async function _appendSeg(idx, genId, data, buf, ch) {
  if (_mseGenId !== genId || !_sbReady) return false;
  if (_segStart[idx] !== undefined) return true; // another caller appended it first

  _segStart[idx] = _mseNext;
  ch.durations[idx] = data.duration_sec;

  try { await _sbAppend(buf); }
  catch (_) { delete _segStart[idx]; return false; }

  const actualEnd = _sb?.buffered.length > 0
    ? _sb.buffered.end(_sb.buffered.length - 1)
    : (_mseNext + (data.duration_sec || 0));
  _segEnd[idx]  = actualEnd;
  _mseNext      = actualEnd;

  // Finalize stream after last segment
  if (idx === ch.segments.length - 1 && _ms?.readyState === 'open') {
    try { _ms.endOfStream(); } catch (_) {}
  }
  return true;
}

async function _appendAhead(fromIdx, genId) {
  for (let i = fromIdx; i < Math.min(fromIdx + 3, chapter.segments.length); i++) {
    if (_mseGenId !== genId) return;
    if (_segStart[i] !== undefined) continue;
    await _fetchAndAppendSeg(i, genId);
  }
}

function _onSegmentChange(idx) {
  if (idx >= chapter.segments.length) return;
  setCurrentSegment(idx, { highlight: true, save: false });
  const seg = chapter.segments[idx];
  document.getElementById('pb-character').textContent = seg.character_name || 'Narrator';
  updatePlaybackUI();
  _updateMediaPosition(idx);
  startWordHighlight(idx, _segDur(idx));
  _appendAhead(idx + 1, _mseGenId);
}

async function _schedulePreload(playingIdx) {
  if (!isPlaying) return;
  const ch = chapter;                  // a chapter switch while we wait must not receive this audio
  const nextIdx = playingIdx + 1;
  if (nextIdx >= ch.segments.length) return;

  // Fire N+1 first then N+2 so they arrive at the server TTS lock in the
  // correct order (N+1 queued before N+2).  Two-deep pipeline means a slow
  // TTS generation for N+1 overlaps with N-1's remaining play time *and*
  // N's full play time rather than just N's play time.
  fetchSegmentData(nextIdx, ch);
  if (nextIdx + 1 < ch.segments.length) fetchSegmentData(nextIdx + 1, ch);

  try {
    const data = await fetchSegmentData(nextIdx, ch);
    if (!isPlaying || chapter !== ch) return;

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
  const ch = chapter;
  const charLabel = ch.segments[idx]?.character_name || 'Narrator';

  if (_chapterAudioChId !== ch.id) {
    const chId = ch.id;

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
      ch.durations[i] = t_end - t_start;
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
    _chapterAudioChId = chId;
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
    _seekingIntentionally = true;
    audio.currentTime = start;
    await _awaitSeek();
    if (gen !== _playGen || !isPlaying) return true;
  }

  charEl.textContent = charLabel;
  startWordHighlight(idx, _segDur(idx));
  audio.playbackRate = speedMultiplier;
  if (audio.paused) await audio.play();
  if (gen !== _playGen) return true;

  _updateMediaPosition(idx);
  return true;
}

async function playSegment(idx) {
  if (idx >= chapter.segments.length) { stopPlayback(); return; }

  const gen = ++_playGen;
  isPlaying = true;
  _sleepTimerFired = false; // explicit play clears fired state so auto-advance works again
  updateMediaSession(true);
  setCurrentSegment(idx, { highlight: true, save: false });

  const charEl    = document.getElementById('pb-character');
  const seg       = chapter.segments[idx];
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
          initOffset += chapter.segments[i]?.duration_sec ?? chapter.durations[i] ?? 0;
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

      // Seek to segment start unless already paused inside it.
      // Await the 'seeked' event so the RAF starts with a settled currentTime —
      // a stale pre-seek position causes immediate spurious segment transitions.
      const start = _segStart[idx];
      if (start !== undefined) {
        const inRange = audio.currentTime >= start &&
                        audio.currentTime <  (_segEnd[idx] ?? start + 1);
        if (!inRange) {
          _seekingIntentionally = true;
          audio.currentTime = start;
          await _awaitSeek();
          if (gen !== _playGen || !isPlaying) return;
        }
      }

      charEl.textContent = charLabel;
      startWordHighlight(idx, _segDur(idx));
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
    chapter.durations[idx] = data.duration_sec;
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
  const idx = chapters.findIndex(c => c.id === chapter.id);
  if (idx < 0 || idx >= chapters.length - 1) return;
  const nextChId = chapters[idx + 1].id;
  fetch(`/api/books/${BOOK_ID}/chapters/${nextChId}/prerender`, { method: 'POST' })
    .catch(() => {});
}

async function _advanceChapter() {
  const idx = chapters.findIndex(c => c.id === chapter.id);
  if (idx >= 0 && idx < chapters.length - 1) {
    // Keep audio.src and media session alive across the chapter gap so the OS
    // lock-screen notification doesn't disappear during the API fetches.
    _autoAdvancing = true;
    const nextId = chapters[idx + 1].id;
    try {
      await openChapter(nextId, { resumePosition: 0, persistOpened: true });
    } finally {
      _autoAdvancing = false;
    }
    // Timer may have fired during openChapter's awaits — don't resume if so
    if (_sleepTimerFired) {
      _sleepTimerFired = false;
      // Media session teardown was deferred above — clear it now
      updateMediaSession(false);
      if ('mediaSession' in navigator) {
        try { navigator.mediaSession.setPositionState({}); } catch (_) {}
      }
    } else if (chapter.id === nextId) {          // not if a newer open replaced it meanwhile
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
  const wholeChapter = (_mseOk && _sbReady) || (_fullChapterMode && _chapterAudioChId === chapter.id);
  if (wholeChapter) {
    // Whole chapter played as one file; RAF was paused if screen was locked.
    // Advance to the last segment before saving/stopping so unlock shows correct position.
    currentSegIdx = chapter.segments.length - 1;
    queueProgressSave(chapter.id, currentSegIdx);
    if (_sleepOnChapterEnd) { _sleepOnChapterEnd = false; _updateSleepTimerUI(); stopPlayback(); return; }
    _advanceChapter();
    return;
  }
  // WAV segment-by-segment fallback: advance segment, or chapter when done
  const next = currentSegIdx + 1;
  if (next < chapter.segments.length) playSegment(next);
  else {
    queueProgressSave(chapter.id, currentSegIdx);
    if (_sleepOnChapterEnd) { _sleepOnChapterEnd = false; _updateSleepTimerUI(); stopPlayback(); return; }
    _advanceChapter();
  }
}

function _onAudioError(e) {
  if (e?.target !== audio) return;
  stopWordHighlight();
  if (!isPlaying) return;
  // Forget the audio so the segment is re-requested (and regenerated) on retry
  _forgetAudio(currentSegIdx);
  // Skip the broken segment rather than stopping entirely
  const next = currentSegIdx + 1;
  if (next < chapter.segments.length) playSegment(next);
  else stopPlayback();
}

_audioA.addEventListener('ended', _onAudioEnded);
_audioB.addEventListener('ended', _onAudioEnded);
for (const a of [_audioA, _audioB]) {
  a.addEventListener('timeupdate', e => { if (e.target === audio) _followTimeline(); });
}
_audioA.addEventListener('error', _onAudioError);
_audioB.addEventListener('error', _onAudioError);

// Re-sync segment after any native seek (e.g. iOS lock-screen controls that
// seek the audio element directly without calling the mediaSession handlers).
// Suppressed for our own seeks via _seekingIntentionally: MP4 keyframe snapping
// can land slightly before the target _segStart, which would wrongly move
// currentSegIdx back by one if we let the scan run.
function _onAudioSeeked(e) {
  if (e.target !== audio) return;
  if (_seekingIntentionally) { _seekingIntentionally = false; return; }
  if (!isPlaying || (!_mseOk && !_fullChapterMode)) return;
  const t = audio.currentTime;
  let correct = currentSegIdx;
  for (let i = 0; i < chapter.segments.length; i++) {
    if (_segStart[i] !== undefined && t >= _segStart[i]) correct = i;
    else if (_segStart[i] !== undefined) break;
  }
  if (correct !== currentSegIdx) {
    setCurrentSegment(correct, { highlight: true, save: false });
    startWordHighlight(correct, _segDur(correct));
    _updateMediaPosition(correct);
  }
}
_audioA.addEventListener('seeked', _onAudioSeeked);
_audioB.addEventListener('seeked', _onAudioSeeked);

function stopPlayback() {
  isPlaying = false;
  _kickBuffer();
  _playGen++;
  stopWordHighlight();
  if (_mseOk) {
    _teardownMSE();
  } else {
    _audioA.pause();
    _audioB.pause();
    if (!_autoAdvancing) {
      _audioA.src = '';
      _audioB.src = '';
    }
    audio = _audioA;
    _preloadIdx       = -1;
    _preloadData      = null;
    _chapterAudioChId    = null;
    _chapterPrerenderChId = null;
    _segStart            = {};
    _segEnd              = {};
  }
  if (chapter.id) queueProgressSave(chapter.id, currentSegIdx);
  const btn = document.getElementById('btn-play');
  btn.innerHTML = '&#9654;';
  btn.classList.add('paused');
  updateSpeakerLabel();
  updatePlaybackUI();
  if (!_autoAdvancing) {
    updateMediaSession(false);
    if ('mediaSession' in navigator) {
      try { navigator.mediaSession.setPositionState({}); } catch (_) {}
    }
  }
}

// ── Word-level highlighting ───────────────────────────────────────────────────

let _wordRafId = null;
let _progressInterval = null;

// In timeline playback (MSE or the full-chapter file): move the cursor to the segment now
// playing, and keep the next segments appended to the stream. Returns true if it moved.
// This runs from the animation frame (for smooth highlighting) AND from the audio element's
// own 'timeupdate'. Browsers stop animation frames while the screen is locked or blacked out;
// when only the frame loop did this, nothing extended the stream then and the audio ran out of
// buffered segments a minute or two after the screen went dark.
let _timelineRetryAt = 0;
function _followTimeline() {
  if (!isPlaying || !(_mseOk || _fullChapterMode)) return false;
  const t = audio.currentTime;
  let reached = -1, firstMissing = -1;
  for (let ni = currentSegIdx + 1; ni < chapter.segments.length; ni++) {
    const start = _segStart[ni];
    if (start === undefined) {
      if (firstMissing < 0) firstMissing = ni;     // not appended yet
      continue;
    }
    if (t >= start) reached = ni; else break;      // the next appended segment hasn't started yet
  }
  if (reached >= 0) { _onSegmentChange(reached); return true; }
  // Waiting on a segment whose TTS wasn't ready: retry the append, but not on every update.
  if (firstMissing >= 0 && Date.now() >= _timelineRetryAt) {
    _timelineRetryAt = Date.now() + 2000;
    _appendAhead(firstMissing, _mseGenId);
  }
  return false;
}

function startWordHighlight(segIdx, durationSec) {
  stopWordHighlight();
  _progressInterval = setInterval(() => queueProgressSave(), 30_000);
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

  function tick() {
    const t = audio.currentTime;
    if (_followTimeline()) return;       // moved to the next segment: a new tick runs for it

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
  if (_progressInterval) { clearInterval(_progressInterval); _progressInterval = null; }
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
  if (active && _followPlayback) active.scrollIntoView({ behavior, block: 'center' });
}

// ── Following playback ────────────────────────────────────────────────────────
// While playing, the page scrolls to keep the current sentence centred. Scrolling by hand
// (wheel, touch, scrollbar, page keys) pauses that, so you can read ahead or back without
// being pulled away, and a button appears to return. Playing again, picking a sentence, or
// the button resumes it. Anything not playing always follows.

function suspendFollow() {
  if (!isPlaying || !_followPlayback) return;
  _followPlayback = false;
  updateFollowButton();
}

function resumeFollow({ scroll = true } = {}) {
  _followPlayback = true;
  updateFollowButton();
  if (scroll) highlightSegment(currentSegIdx);
}

function updateFollowButton() {
  if (!isPlaying) _followPlayback = true;           // pausing or stopping ends a suspension
  document.getElementById('follow-btn').classList.toggle('hidden', !isPlaying || _followPlayback);
}

// Scrolling the user did, as opposed to the page scrolling itself (re-centring on a sentence, a
// reflow after a font change, a scroll position restored after a reload). Only the former may
// pause following or move the reading position: a scroll event alone proves nothing, and a stray
// one used to move the cursor from the paused sentence to whatever sat at 35% of the screen height.
let _userScrollAt = 0;
let _scrollbarDragging = false;
const _userIsScrolling = () => _scrollbarDragging || Date.now() - _userScrollAt < 1500;   // momentum scrolling lingers

function noteUserScroll() {
  _userScrollAt = Date.now();
  suspendFollow();
}

(function wireManualScroll() {
  const content = document.getElementById('chapter-content');
  content.addEventListener('wheel', noteUserScroll, { passive: true });
  content.addEventListener('touchmove', noteUserScroll, { passive: true });
  content.addEventListener('touchend', () => { _userScrollAt = Date.now(); }, { passive: true });
  // Dragging or clicking the scrollbar: the click lands on the scroller itself, beyond its content width.
  content.addEventListener('pointerdown', e => {
    if (e.target === content && e.offsetX >= content.clientWidth) { _scrollbarDragging = true; noteUserScroll(); }
  });
  const dragEnd = () => { if (_scrollbarDragging) { _scrollbarDragging = false; _userScrollAt = Date.now(); } };
  document.addEventListener('pointerup', dragEnd);
  document.addEventListener('pointercancel', dragEnd);
  document.addEventListener('keydown', e => {
    if (['PageUp', 'PageDown', 'Home', 'End', 'ArrowUp', 'ArrowDown'].includes(e.key)
        && !/^(input|textarea|select)$/i.test(document.activeElement.tagName)) noteUserScroll();
  });
})();

// ── Progress bar ──────────────────────────────────────────────────────────────

function updateProgress() {
  if (!chapter.segments.length) return;
  const pct = ((currentSegIdx) / chapter.segments.length) * 100;
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
  updateFollowButton();
  if (chapter.segments.length) {
    prog.textContent = `${currentSegIdx + 1} / ${chapter.segments.length}`;
    if (fill) fill.style.width = `${((currentSegIdx + 1) / chapter.segments.length) * 100}%`;
  }
}

// ── Media Session (lock-screen / notification controls) ───────────────────────

// Time-based seek for MSE / Full Chapter modes. Returns false in WAV mode
// (caller falls back to segment-hop). Scans all _segStart entries since
// direction of seek is unknown (forward or backward).
function _seekToTime(targetTime) {
  if (!_mseOk && !_fullChapterMode) return false;
  const t = Math.max(0, Math.min(targetTime, audio.duration || targetTime));
  _seekingIntentionally = true;
  audio.currentTime = t;
  let correct = 0;
  for (let i = 0; i < chapter.segments.length; i++) {
    if (_segStart[i] !== undefined && t >= _segStart[i]) correct = i;
    else if (_segStart[i] !== undefined) break;
  }
  if (correct !== currentSegIdx) {
    setCurrentSegment(correct, { highlight: true, save: false });
    if (isPlaying) startWordHighlight(correct, _segDur(correct));
  }
  return true;
}

function updateMediaSession(playing) {
  if (!('mediaSession' in navigator)) return;
  navigator.mediaSession.playbackState = playing ? 'playing' : 'paused';
}

function _updateMediaPosition(idx) {
  if (!('mediaSession' in navigator)) return;
  // Build position from known durations; unknown segments estimated at 0
  let position = 0;
  for (let i = 0; i < idx; i++) position += chapter.durations[i] || 0;
  // Total duration: sum of known durations + 0 for unknown future segments
  let total = 0;
  for (let i = 0; i < chapter.segments.length; i++) total += chapter.durations[i] || 0;
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
    if (isPlaying) return;
    // Resume from the exact paused position when audio is already loaded.
    // Routing through playSegment() re-seeks to segment start in WAV mode,
    // and relies on currentTime being preserved (not guaranteed on iOS after
    // backgrounding with MSE). Direct resume matches HTML play button.
    if (audio.src && !audio.ended) {
      isPlaying = true;
      _sleepTimerFired = false;
      updateMediaSession(true);
      updatePlaybackUI();
      const dur = _segDur(currentSegIdx) || chapter.durations[currentSegIdx] || 0;
      startWordHighlight(currentSegIdx, dur);
      audio.play().catch(() => {
        isPlaying = false;
        updateMediaSession(false);
        updatePlaybackUI();
        playSegment(currentSegIdx);
      });
    } else {
      playSegment(currentSegIdx);
    }
  });
  navigator.mediaSession.setActionHandler('pause', () => {
    if (isPlaying) { isPlaying = false; _kickBuffer(); stopWordHighlight(); audio.pause(); updatePlaybackUI(); updateMediaSession(false); }
  });
  // previoustrack/nexttrack are reliably fired on iOS; seekbackward/seekforward
  // have limited iOS Safari support. Wire previous/next to ±10s time seek so
  // the lock-screen << >> buttons replay/advance within the chapter.
  const _seekBack = (details) => {
    if (!_seekToTime(audio.currentTime - (details?.seekOffset ?? 10))) {
      const prev = Math.max(currentSegIdx - 1, 0);
      if (isPlaying) playSegment(prev); else setCurrentSegment(prev, { highlight: true, save: true });
    }
  };
  const _seekFwd = (details) => {
    if (!_seekToTime(audio.currentTime + (details?.seekOffset ?? 10))) {
      const next = Math.min(currentSegIdx + 1, chapter.segments.length - 1);
      if (isPlaying) playSegment(next); else setCurrentSegment(next, { highlight: true, save: true });
    }
  };
  navigator.mediaSession.setActionHandler('previoustrack', _seekBack);
  navigator.mediaSession.setActionHandler('nexttrack',     _seekFwd);
  navigator.mediaSession.setActionHandler('seekbackward',  _seekBack);
  navigator.mediaSession.setActionHandler('seekforward',   _seekFwd);
  navigator.mediaSession.setActionHandler('stop', () => stopPlayback());
}

// ── Sleep timer ───────────────────────────────────────────────────────────────

function _clearSleepTimer() {
  clearTimeout(_sleepTimerId);  _sleepTimerId = null;
  clearInterval(_sleepCountdownId); _sleepCountdownId = null;
}

// minutes: 0 = off, -1 = end of chapter, otherwise a countdown (a preset, or any value from the dial).
// The menu closes after picking, except when fine-tuning from the dial or the nudge buttons.
function setSleepTimer(minutes, { keepOpen = false } = {}) {
  _clearSleepTimer();
  _sleepTimerEnd     = 0;
  _sleepOnChapterEnd = false;
  _sleepTimerFired   = false;

  if (minutes < 0) {                     // special value: end of chapter
    _sleepOnChapterEnd = true;
  } else if (minutes > 0) {
    _startSleepCountdown(minutes * 60_000);
  }
  _updateSleepTimerUI();
  if (!keepOpen) document.getElementById('sleep-dropdown').classList.add('hidden');
}

function _startSleepCountdown(ms) {
  _sleepTimerEnd = Date.now() + ms;
  _sleepTimerId  = setTimeout(_onSleepTimerFired, ms);
  _sleepCountdownId = setInterval(_updateSleepTimerUI, 1000);
}

// Move a running timer's end by a few minutes (never below one minute from now).
function adjustSleepTimer(deltaMinutes) {
  if (_sleepTimerEnd <= 0) return;
  const remaining = _sleepTimerEnd - Date.now();
  _clearSleepTimer();
  _startSleepCountdown(Math.max(60_000, remaining + deltaMinutes * 60_000));
  _updateSleepTimerUI();
}

const _minutesLabel = m => m >= 60 ? `${Math.floor(m / 60)} h${m % 60 ? ` ${m % 60} min` : ''}` : `${m} min`;
let _sleepDialDragging = false;      // while the dial is being moved, the running timer does not move it back

function onSleepDial(value) {
  _sleepDialDragging = true;
  document.getElementById('sleep-dial-label').textContent = _minutesLabel(+value);
}

function _onSleepTimerFired() {
  _clearSleepTimer();
  _sleepTimerEnd   = 0;
  _sleepTimerFired = true;
  // Resync segment index before stopPlayback() clears audio.src / _segStart.
  // The screen may have been locked while we counted down, so the RAF hasn't
  // updated currentSegIdx; capture the real position now so unlock shows it.
  if (_mseOk || _fullChapterMode) {
    const t = audio.currentTime;
    for (let i = currentSegIdx + 1; i < chapter.segments.length; i++) {
      const s = _segStart[i];
      if (s === undefined || t < s) break;
      currentSegIdx = i;
    }
  }
  stopPlayback();
  _updateSleepTimerUI();
}

function _updateSleepTimerUI() {
  const btn = document.getElementById('sleep-btn');
  if (!btn) return;
  const status = document.getElementById('sleep-status');
  const remaining = document.getElementById('sleep-remaining');
  const dial = document.getElementById('sleep-dial');
  status.classList.toggle('hidden', !(_sleepOnChapterEnd || _sleepTimerEnd > 0));
  if (_sleepOnChapterEnd) {
    btn.textContent = '☽ ch';
    btn.classList.add('sleep-active');
    remaining.textContent = 'end of chapter';
    status.querySelectorAll('.sleep-nudge').forEach(b => b.classList.add('hidden'));
  } else if (_sleepTimerEnd > 0) {
    const ms  = Math.max(0, _sleepTimerEnd - Date.now());
    const m   = Math.floor(ms / 60_000);
    const s   = Math.floor((ms % 60_000) / 1000);
    btn.textContent = `${m}:${String(s).padStart(2, '0')}`;
    btn.classList.add('sleep-active');
    remaining.textContent = btn.textContent;
    status.querySelectorAll('.sleep-nudge').forEach(b => b.classList.remove('hidden'));
    if (!_sleepDialDragging) {                      // the dial follows the clock
      dial.value = Math.min(+dial.max, Math.max(1, Math.ceil(ms / 60_000)));
      document.getElementById('sleep-dial-label').textContent = _minutesLabel(+dial.value);
    }
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
    _kickBuffer();
    stopWordHighlight();
    audio.pause();
    updatePlaybackUI();
    queueProgressSave(chapter.id, currentSegIdx);
  } else {
    playSegment(currentSegIdx);
  }
};

document.getElementById('btn-stop').onclick = () => {
  stopPlayback();
  // True stop: go back to the beginning of the chapter, not just pause in place.
  if (chapter.id) setCurrentSegment(0, { highlight: true, behavior: 'smooth', save: true });
};

document.getElementById('btn-next-seg').onclick = () => {
  resumeFollow({ scroll: false });
  const next = Math.min(currentSegIdx + 1, chapter.segments.length - 1);
  if (isPlaying) playSegment(next);
  else { setCurrentSegment(next, { highlight: true, save: true }); }
};

document.getElementById('btn-prev-seg').onclick = () => {
  resumeFollow({ scroll: false });
  const prev = Math.max(currentSegIdx - 1, 0);
  if (isPlaying) playSegment(prev);
  else { setCurrentSegment(prev, { highlight: true, save: true }); }
};

document.getElementById('btn-prev-ch').onclick = () => {
  const idx = chapters.findIndex(c => c.id === chapter.id);
  if (idx > 0) openChapter(chapters[idx - 1].id);
};
document.getElementById('btn-next-ch').onclick = () => {
  const idx = chapters.findIndex(c => c.id === chapter.id);
  if (idx >= 0 && idx < chapters.length - 1) openChapter(chapters[idx + 1].id);
};

document.getElementById('pb-seek-bar').onclick = (e) => {
  if (!chapter.segments.length) return;
  resumeFollow({ scroll: false });
  const bar = e.currentTarget;
  const frac = Math.max(0, Math.min(1, e.offsetX / bar.offsetWidth));
  const targetIdx = Math.round(frac * (chapter.segments.length - 1));
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
      if (!chapter.id) return;
      reBtn.disabled = true;
      try {
        await fetch(`/api/books/${BOOK_ID}/chapters/${chapter.id}/prerender`, { method: 'DELETE' });
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
  if (!chapter.segments.length || isPlaying || Date.now() < _ignoreScrollTrackingUntil || !_userIsScrolling()) return;

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
    _kickBuffer();
    updatePlaybackUI();
    updateProgress();
    queueProgressSave(chapter.id, currentSegIdx);
  }
}

function scheduleViewportProgressUpdate() {
  if (!chapter.segments.length || isPlaying || Date.now() < _ignoreScrollTrackingUntil || !_userIsScrolling()) return;
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
  if (!chapter.id) { showToast('Open a chapter first.'); return; }
  const seg = chapter.segments[currentSegIdx];
  const excerpt = seg ? seg.text.slice(0, 120) : '';
  const r = await fetch(`/api/books/${BOOK_ID}/bookmarks`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      chapter_id:    chapter.id,
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
  if (chapterId !== chapter.id) {
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
  if (!chapter.id) { showToast('Open a chapter first.'); return; }

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
  if (mode === 'chapter')          url = `/api/books/${BOOK_ID}/export/chapter/${chapter.id}`;
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
      await sleep(800);
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
  if (!chapter.id) { showToast('Open a chapter first.'); return; }
  const status = document.getElementById('preload-status');
  const btn    = document.getElementById('preload-btn');
  const idx    = chapters.findIndex(c => c.id === chapter.id);
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
    case 'g': case 'G':
      toggleGutter();
      break;
    case 'e': case 'E':
      openSpeakerEditor();
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
      closeSpeakerEditor();
      hideShortcuts();
      document.getElementById('export-dropdown').classList.add('hidden');
      break;
  }
});

function showShortcuts()  { document.getElementById('shortcuts-overlay').classList.remove('hidden'); }
function hideShortcuts()  { document.getElementById('shortcuts-overlay').classList.add('hidden'); }

// ── Speaker correction ────────────────────────────────────────────────────────

let _speakerEditIdx = -1;

// Indices of the segments a correction at `idx` covers for the chosen scope.
function _speakerScopeIndices(idx, scope) {
  const seg = chapter.segments[idx];
  if (scope === 'segment') return [idx];
  return [...chapter.segments.keys()].filter(i => {
    const s = chapter.segments[i];
    if (s.paragraph !== seg.paragraph) return false;
    return scope === 'quote' ? s.span === seg.span : (s.is_dialogue || i === idx);
  });
}

function openSpeakerEditor(idx = currentSegIdx) {
  // Section breaks have no text to attribute: use the nearest sentence instead.
  if (!chapter.segments[idx]?.text) {
    const after = chapter.segments.findIndex((s, i) => i >= idx && s.text);
    idx = after >= 0 ? after : chapter.segments.slice(0, idx).findLastIndex(s => s.text);
  }
  const seg = chapter.segments[idx];
  if (!seg || !seg.text) { showToast('Nothing to correct here', 'err'); return; }
  _speakerEditIdx = idx;

  _speakerChoice = seg.character_name || '';
  _speakerTyped = false;
  document.getElementById('speaker-input').value = _speakerChoice || 'Narrator';
  hideSpeakerList();

  const n = scope => _speakerScopeIndices(idx, scope).length;
  document.getElementById('speaker-quote-n').textContent = `(${n('quote')} sentence${n('quote') === 1 ? '' : 's'})`;
  document.getElementById('speaker-para-n').textContent  = `(${n('paragraph')} spoken)`;
  // A sentence that opens or closes with a quotation mark is part of a quote: default to the whole quote.
  const quoted = /^["“”]|["“”]$/.test(seg.text.trim());
  document.querySelector(`input[name="speaker-scope"][value="${quoted ? 'quote' : 'segment'}"]`).checked = true;
  document.getElementById('speaker-new-name').value = '';
  onSpeakerSelectChange();
  document.getElementById('speaker-modal').classList.remove('hidden');
}

// Show the whole paragraph; underline what the chosen scope will change, in the
// chosen speaker's colour.
function renderSpeakerPreview() {
  const idx = _speakerEditIdx;
  if (idx < 0) return;
  const scope = document.querySelector('input[name="speaker-scope"]:checked').value;
  const color = _speakerChoice && _speakerChoice !== '__new__' ? charColors[_speakerChoice] : '';
  const affected = new Set(_speakerScopeIndices(idx, scope));
  const para = chapter.segments[idx].paragraph;
  const box = document.getElementById('speaker-text');
  box.style.setProperty('--sp-color', color || 'var(--text3)');
  box.innerHTML = chapter.segments.map((s, i) => ({ s, i })).filter(({ s }) => s.paragraph === para && s.text)
    .map(({ s, i }) => {
      const cls = 'sp-sent' + (affected.has(i) ? ' affected' : '') + (i === idx ? ' target' : '');
      return `<span class="${cls}">${esc(s.text)}</span>`;
    }).join(' ');
  box.querySelector('.target')?.scrollIntoView({ block: 'nearest' });
}

function closeSpeakerEditor() {
  hideSpeakerList();
  document.getElementById('speaker-modal').classList.add('hidden');
  _speakerEditIdx = -1;
  resetImpactPreview();
}

// ── New-character impact preview ──────────────────────────────────────────────
// While filling in "New character…", show who-speaks-what changes it would cause
// across the book: a transition summary plus a diff of every changed sentence.

let _impactTimer = null;
let _impactToken = 0;       // discards responses that arrive after the form changed

function resetImpactPreview() {
  clearTimeout(_impactTimer);
  _impactToken++;
  const box = document.getElementById('speaker-impact');
  box.classList.add('hidden');
  box.innerHTML = '';
}

function scheduleImpactPreview() {
  clearTimeout(_impactTimer);
  const name = document.getElementById('speaker-new-name').value.trim();
  if (_speakerChoice !== '__new__' || !name) {
    resetImpactPreview();
    return;
  }
  const box = document.getElementById('speaker-impact');
  box.classList.remove('hidden');
  box.innerHTML = '<span class="impact-muted">Checking what this would change…</span>';
  _impactTimer = setTimeout(loadImpactPreview, 400);
}

async function loadImpactPreview() {
  const token = ++_impactToken;
  const box = document.getElementById('speaker-impact');
  try {
    const r = await fetch(`/api/books/${BOOK_ID}/characters/preview`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: document.getElementById('speaker-new-name').value.trim(),
        gender: document.getElementById('speaker-new-gender').value,
      }),
    });
    const data = await r.json();
    if (token !== _impactToken) return;
    box.innerHTML = r.ok ? renderImpact(data)
      : `<span class="impact-muted">${esc(data.error || 'Preview failed')}</span>`;
  } catch (e) {
    if (token === _impactToken) box.innerHTML = '<span class="impact-muted">Could not load a preview.</span>';
  }
}

function renderImpact(d) {
  const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
  if (!d.total) {
    return `<div class="impact-head">Adding <b>${esc(d.name)}</b> would not change any other sentence.</div>`;
  }
  const toNew = d.transitions.filter(t => t.to === d.name);
  const others = d.transitions.filter(t => t.to !== d.name);

  let html = `<div class="impact-head">Adding <b>${esc(d.name)}</b> would change who speaks
    <b>${plural(d.total, 'sentence')}</b> in <b>${plural(d.chapters.length, 'chapter')}</b>.</div>`;
  if (toNew.length) {
    html += `<div class="impact-row">→ <b>${esc(d.name)}</b> gains ${d.gained}: ` +
      toNew.map(t => `${t.count} from ${esc(speakerLabel(t.from))}`).join(', ') + '</div>';
  }
  if (others.length) {
    html += `<div class="impact-row">⇄ Knock-on shifts: ` +
      others.map(t => `${esc(speakerLabel(t.from))} → ${esc(speakerLabel(t.to))} <span class="impact-muted">×${t.count}</span>`).join(', ') + '</div>';
  }
  const shown = d.chapters.slice(0, 6).map(c => `${esc(c.title)} <span class="impact-muted">(${c.changed})</span>`);
  const more = d.chapters.length - shown.length;
  html += `<div class="impact-row impact-muted">Chapters: ${shown.join(', ')}${more > 0 ? `, +${more} more` : ''}</div>`;

  let lastChapter = null;
  const items = d.changes.map(c => {
    const heading = c.chapter_id !== lastChapter ? `<div class="diff-chapter">${esc(c.chapter_title)}</div>` : '';
    lastChapter = c.chapter_id;
    return heading + excerptCard({
      prev: c.prev, text: c.text,
      lines: [{ kind: 'del', label: speakerLabel(c.before) }, { kind: 'add', label: speakerLabel(c.after) }],
    });
  }).join('');
  html += `<details><summary>Show the changes</summary><div class="diff-list">${items}</div>
    ${d.truncated ? `<div class="impact-muted">Showing the first ${d.changes.length} of ${d.total}.</div>` : ''}</details>`;
  return html;
}

function onSpeakerSelectChange() {
  const isNew = _speakerChoice === '__new__';
  document.getElementById('speaker-new').classList.toggle('hidden', !isNew);
  renderSpeakerPreview();
  scheduleImpactPreview();
}

// ── Speaker picker ────────────────────────────────────────────────────────────
// A text box with a list under it. Empty (or just opened) it shows everyone: the narrator,
// the last few characters assigned by hand, those speaking close to this sentence (nearest
// first), then the rest in Voice Studio's order. A character that is merged into another is not listed on its own; its group
// is (under the head's name), and typing the merged name still finds the group.

let _speakerChoice = '';        // '' = narrator, '__new__', or a character name
let _speakerTyped = false;      // the box holds something the user typed, so the list is filtered by it
let _speakerRows = [];          // what the list shows
let _speakerActive = -1;        // index in _speakerRows of the highlighted row
const SPEAKERS_NEARBY = 5;
const SPEAKERS_RECENT = 3;

function _speakerCandidates(idx) {
  const byName = new Map(charList.map(c => [c.name, c]));
  const headOf = name => {
    const c = byName.get(name);
    return c?.alias_of != null ? (charList.find(h => h.id === c.alias_of)?.name || name) : name;
  };
  const heads = charList.filter(c => c.alias_of == null);
  // The characters most recently assigned by hand, as their group's head, newest first.
  const recent = [];
  charList.filter(c => c.last_override_at)
    .sort((a, b) => b.last_override_at.localeCompare(a.last_override_at))
    .forEach(c => { const h = headOf(c.name); if (!recent.includes(h) && recent.length < SPEAKERS_RECENT) recent.push(h); });
  const near = [];
  for (let d = 1; d < chapter.segments.length && near.length < SPEAKERS_NEARBY; d++) {
    for (const i of [idx - d, idx + d]) {
      const name = chapter.segments[i]?.character_name;
      const head = name && headOf(name);
      if (head && byName.has(head) && !near.includes(head) && !recent.includes(head)) near.push(head);
    }
  }
  const entry = h => ({ name: h.name, aliases: charList.filter(c => c.alias_of === h.id).map(c => c.name) });
  return {
    recent: recent.map(n => entry(byName.get(n))),
    nearby: near.map(n => entry(byName.get(n))),
    rest: heads.filter(h => !near.includes(h.name) && !recent.includes(h.name)).map(entry),
  };
}

function buildSpeakerRows(query) {
  const q = query.trim().toLowerCase();
  const { recent, nearby, rest } = _speakerCandidates(_speakerEditIdx);
  const row = (e, extra = {}) => ({
    kind: 'char', value: e.name, label: e.name, color: charColors[e.name],
    sub: q && !e.name.toLowerCase().includes(q) ? e.aliases.find(a => a.toLowerCase().includes(q)) : '',
    ...extra,
  });
  const rows = [];
  if (!q) {
    rows.push({ kind: 'narrator', value: '', label: 'Narrator' });
    if (recent.length) rows.push({ kind: 'heading', label: 'Recently assigned' }, ...recent.map(e => row(e)));
    if (nearby.length) rows.push({ kind: 'heading', label: 'Speaking nearby' }, ...nearby.map(e => row(e)));
    if (rest.length) rows.push({ kind: 'heading', label: recent.length || nearby.length ? 'Everyone else' : 'Characters' }, ...rest.map(e => row(e)));
    rows.push({ kind: 'new', value: '__new__', label: '+ New character…' });
    return rows;
  }
  const everyone = [...recent, ...nearby, ...rest];
  const matches = everyone.filter(e =>
    e.name.toLowerCase().includes(q) || e.aliases.some(a => a.toLowerCase().includes(q)));
  matches.sort((a, b) => (b.name.toLowerCase().startsWith(q)) - (a.name.toLowerCase().startsWith(q)));   // stable: names starting with it first
  if ('narrator'.startsWith(q)) rows.push({ kind: 'narrator', value: '', label: 'Narrator' });
  rows.push(...matches.map(e => row(e)));
  const known = everyone.some(e => [e.name, ...e.aliases].some(n => n.toLowerCase() === q));
  if (!known && q !== 'narrator') rows.push({ kind: 'new', value: '__new__', label: `+ Add “${query.trim()}” as a new character`, newName: query.trim() });
  return rows;
}

function showSpeakerList() {
  const input = document.getElementById('speaker-input');
  _speakerRows = buildSpeakerRows(_speakerTyped ? input.value : '');
  const selectable = _speakerRows.filter(r => r.kind !== 'heading');
  const current = selectable.findIndex(r => r.kind !== 'new' && r.value === _speakerChoice);
  _speakerActive = _speakerRows.indexOf(selectable[_speakerTyped ? 0 : Math.max(current, 0)]);
  const list = document.getElementById('speaker-list');
  list.innerHTML = selectable.length ? _speakerRows.map((r, i) => {
    if (r.kind === 'heading') return `<li class="heading" role="presentation">${esc(r.label)}</li>`;
    const dot = r.color ? `<span class="dot" style="background:${esc(r.color)}"></span>` : '';
    const sub = r.sub ? `<span class="sub">(${esc(r.sub)})</span>` : '';
    return `<li role="option" data-i="${i}" class="${r.kind === 'new' ? 'new-row' : ''}${i === _speakerActive ? ' active' : ''}">${dot}${esc(r.label)}${sub}</li>`;
  }).join('') : '<li class="empty">No match</li>';
  list.classList.remove('hidden');
  input.setAttribute('aria-expanded', 'true');
  list.querySelector('.active')?.scrollIntoView({ block: 'nearest' });
}

function hideSpeakerList() {
  document.getElementById('speaker-list').classList.add('hidden');
  document.getElementById('speaker-input').setAttribute('aria-expanded', 'false');
}

function chooseSpeakerRow(row) {
  const input = document.getElementById('speaker-input');
  _speakerChoice = row.value;
  _speakerTyped = false;
  input.value = row.kind === 'new' ? 'New character…' : row.label;
  hideSpeakerList();
  const nameBox = document.getElementById('speaker-new-name');
  if (row.kind === 'new' && row.newName) nameBox.value = row.newName;
  onSpeakerSelectChange();
  if (row.kind === 'new') nameBox.focus();
}

function moveSpeakerActive(step) {
  const at = _speakerRows.map((r, i) => r.kind === 'heading' ? -1 : i).filter(i => i >= 0);
  if (!at.length) return;
  const next = at[(Math.max(at.indexOf(_speakerActive), 0) + step + at.length) % at.length];
  _speakerActive = next;
  document.querySelectorAll('#speaker-list li').forEach(li => li.classList.toggle('active', +li.dataset.i === next));
  document.querySelector('#speaker-list li.active')?.scrollIntoView({ block: 'nearest' });
}

(function wireSpeakerPicker() {
  const input = document.getElementById('speaker-input');
  const list = document.getElementById('speaker-list');
  input.addEventListener('focus', () => { input.select(); showSpeakerList(); });
  input.addEventListener('click', () => { if (list.classList.contains('hidden')) showSpeakerList(); });
  input.addEventListener('input', () => { _speakerTyped = true; showSpeakerList(); });
  input.addEventListener('keydown', e => {
    const open = !list.classList.contains('hidden');
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (!open) showSpeakerList(); else moveSpeakerActive(e.key === 'ArrowDown' ? 1 : -1);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (open && _speakerRows[_speakerActive]) chooseSpeakerRow(_speakerRows[_speakerActive]);
    } else if (e.key === 'Escape' && open) {
      e.preventDefault();
      hideSpeakerList();
      input.value = _speakerChoice === '__new__' ? 'New character…' : (_speakerChoice || 'Narrator');
      _speakerTyped = false;
    }
  });
  // Pressing on a row must not take focus from the box (that would close the list before the click lands).
  list.addEventListener('mousedown', e => {
    e.preventDefault();
    const li = e.target.closest('li[data-i]');
    if (li) chooseSpeakerRow(_speakerRows[+li.dataset.i]);
  });
  input.addEventListener('blur', () => {
    if (_speakerTyped) {           // typed an exact name and moved on: take it; anything else goes back to the last choice
      const q = input.value.trim().toLowerCase();
      const exact = buildSpeakerRows(input.value).find(r => r.kind !== 'new' && r.label.toLowerCase() === q);
      if (exact) { chooseSpeakerRow(exact); return; }
      input.value = _speakerChoice === '__new__' ? 'New character…' : (_speakerChoice || 'Narrator');
      _speakerTyped = false;
    }
    hideSpeakerList();
  });
})();

// Reload the open chapter so changed attribution, colours and voices take effect, leaving
// everything else as it was: the same sentence current (and highlighted only if it was), the
// same scroll position, and playback resumed from that sentence if it was playing.
async function reloadChapterInPlace() {
  const keep = currentSegIdx;
  const wasPlaying = isPlaying;
  const wasHighlighted = !!document.querySelector('#chapter-content .playing');
  const scroller = document.getElementById('chapter-content');
  const scrolls = [scroller.scrollTop, window.scrollY];
  await openChapter(chapter.id, {
    resumePosition: keep, persistCurrent: false, persistOpened: false, highlightOnLoad: wasHighlighted,
  });
  scroller.scrollTop = scrolls[0];
  window.scrollTo(0, scrolls[1]);
  if (wasPlaying) playSegment(keep);
}

async function applySpeaker(reset) {
  const idx = _speakerEditIdx;
  if (idx < 0) return;
  const scope = document.querySelector('input[name="speaker-scope"]:checked').value;
  let character = _speakerChoice || null;

  try {
    if (!reset && character === '__new__') {
      const name = document.getElementById('speaker-new-name').value.trim();
      if (!name) { showToast('Enter a name for the new character', 'err'); return; }
      const r = await fetch(`/api/books/${BOOK_ID}/characters`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, gender: document.getElementById('speaker-new-gender').value }),
      });
      const data = await r.json();
      if (!r.ok) { showToast(data.error || 'Could not add character', 'err'); return; }
      character = data.name;
    }

    const anchors = _speakerScopeIndices(idx, scope).map(i => chapter.segments[i].anchor).filter(Boolean);
    const r = await fetch(`/api/books/${BOOK_ID}/chapters/${chapter.id}/speaker`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ character, anchors, reset: !!reset }),
    });
    const data = await r.json();
    if (!r.ok) { showToast(data.error || 'Could not update speaker', 'err'); return; }

    closeSpeakerEditor();
    showToast(reset ? 'Speaker reset' : `Speaker updated (${data.updated})`);
    await reloadChapterInPlace();
  } catch (e) {
    showToast(e.message || 'Request failed', 'err');
  }
}

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
    return;
  }
  _kickBuffer();
  // On unlock: resync currentSegIdx to audio.currentTime regardless of play
  // state. The RAF pauses while the screen is locked, so currentSegIdx can lag
  // behind even when the user paused mid-chapter on the lock screen.
  if (!_mseOk && !_fullChapterMode) return;
  const t = audio.currentTime;
  let correct = currentSegIdx;
  for (let i = currentSegIdx + 1; i < chapter.segments.length; i++) {
    const s = _segStart[i];
    if (s === undefined || t < s) break;
    correct = i;
  }
  if (correct !== currentSegIdx) {
    setCurrentSegment(correct, { highlight: true, save: false });
    if (isPlaying) startWordHighlight(correct, _segDur(correct));
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
