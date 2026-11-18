// Speaker corrections page: every correction in the book, grouped by chapter.

const BOOK = window.BOOK_ID;
let corrections = [];
let armed = null;          // the chapter-reset button waiting for a second click

const STATUS_TEXT = {
  active: '',
  ignored: 'Ignored: this character no longer exists',
  missing: 'Sentence no longer in the text',
};

async function loadCorrections() {
  corrections = await fetch(`/api/books/${BOOK}/corrections`).then(r => r.json());
  render();
}

function groupByChapter(items) {
  const groups = new Map();
  for (const c of items) {
    if (!groups.has(c.chapter_id)) groups.set(c.chapter_id, { id: c.chapter_id, title: c.chapter_title, items: [] });
    groups.get(c.chapter_id).items.push(c);
  }
  return [...groups.values()];
}

function cardFor(c) {
  const when = (c.updated_at || c.created_at || '').slice(0, 10);
  const meta = [STATUS_TEXT[c.status] && `<span class="badge-status ${c.status}">${escapeHtml(STATUS_TEXT[c.status])}</span>`,
                c.context_captured ? '' : '<span class="excerpt-note">context not captured</span>',
                when && `<span class="excerpt-note">${escapeHtml(when)}</span>`].filter(Boolean).join(' ');
  const open = c.segment_index != null
    ? `<a class="btn btn-sm btn-ghost" href="${readerUrl(BOOK, c.chapter_id, c.segment_index)}">Open in reader</a>` : '';
  const actions = `${open}<button class="btn btn-sm btn-ghost" data-reset="${escapeHtml(c.anchor)}" data-chapter="${c.chapter_id}">Reset</button>`;
  return excerptCard({
    prev: c.prev_text, text: c.text,
    lines: c.context_captured
      ? [{ kind: 'del', label: speakerLabel(c.detected_speaker) }, { kind: 'add', label: speakerLabel(c.corrected_speaker) }]
      : [{ kind: 'add', label: speakerLabel(c.corrected_speaker) }],
    meta, actions,
    attrs: `data-search="${escapeHtml([c.text, c.detected_speaker, c.corrected_speaker, c.chapter_title].join(' ').toLowerCase())}"`,
  });
}

function render() {
  const list = document.getElementById('corrections-list');
  const groups = groupByChapter(corrections);
  document.getElementById('summary').textContent = corrections.length
    ? `${corrections.length} correction${corrections.length === 1 ? '' : 's'} in ${groups.length} chapter${groups.length === 1 ? '' : 's'}`
    : 'No corrections yet.';
  document.getElementById('toolbar').hidden = corrections.length === 0;
  document.getElementById('empty').hidden = corrections.length !== 0;

  const collapse = corrections.length > 40;       // a long list starts collapsed
  list.innerHTML = groups.map(g => `
    <details class="chapter-group" data-chapter="${g.id}" ${collapse ? '' : 'open'}>
      <summary>
        <span class="chapter-group-title">${escapeHtml(g.title)}</span>
        <span class="excerpt-note">${g.items.length}</span>
        <span class="chapter-group-actions">
          <a class="btn btn-sm btn-ghost" href="${readerUrl(BOOK, g.id)}">Open chapter</a>
          <button class="btn btn-sm btn-ghost" data-reset-chapter="${g.id}">Reset chapter</button>
        </span>
      </summary>
      ${g.items.map(cardFor).join('')}
    </details>`).join('');
  applyFilter();
}

function applyFilter() {
  const q = document.getElementById('filter').value.trim().toLowerCase();
  document.querySelectorAll('#corrections-list .excerpt').forEach(el => { el.hidden = !!q && !el.dataset.search.includes(q); });
  document.querySelectorAll('#corrections-list .chapter-group').forEach(g => {
    g.hidden = !!q && ![...g.querySelectorAll('.excerpt')].some(el => !el.hidden);
  });
}

async function resetCorrections(chapterId, anchors) {
  const body = { chapter_id: chapterId, ...(anchors ? { anchors } : {}) };
  const r = await fetch(`/api/books/${BOOK}/corrections`, {
    method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  if (!r.ok) { document.getElementById('summary').textContent = (await r.json()).error || 'Reset failed'; return; }
  corrections = corrections.filter(c => !(c.chapter_id === chapterId && (!anchors || anchors.includes(c.anchor))));
  render();
}

document.addEventListener('click', e => {
  const one = e.target.closest('[data-reset]');
  if (one) { resetCorrections(+one.dataset.chapter, [one.dataset.reset]); return; }
  const chapterBtn = e.target.closest('[data-reset-chapter]');
  if (chapterBtn) {
    e.preventDefault();                                   // the button sits inside <summary>
    if (armed !== chapterBtn) {                           // two clicks, so a whole chapter isn't one slip away
      if (armed) armed.textContent = 'Reset chapter';
      armed = chapterBtn; chapterBtn.textContent = 'Click again to confirm';
      setTimeout(() => { if (armed === chapterBtn) { chapterBtn.textContent = 'Reset chapter'; armed = null; } }, 6000);
      return;
    }
    armed = null;
    resetCorrections(+chapterBtn.dataset.resetChapter, null);
  }
});

document.getElementById('filter').addEventListener('input', applyFilter);

// The JSON is the same data the page shows, including the detection and surrounding text that
// each correction captured, so it doubles as labelled examples for training.
document.getElementById('download').addEventListener('click', () => {
  const blob = new Blob([JSON.stringify(corrections, null, 2)], { type: 'application/json' });
  const a = Object.assign(document.createElement('a'), { href: URL.createObjectURL(blob), download: `speaker-corrections-book-${BOOK}.json` });
  a.click(); URL.revokeObjectURL(a.href);
});

loadCorrections();
