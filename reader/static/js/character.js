// A character's page: the sentences attributed to them, by chapter, each with a way to change the speaker.

const BOOK = window.BOOK_ID;
const OTHER_SPEAKER = 'Other speaker';
let character = null;
let speakerNames = [];

const list = document.getElementById('excerpt-list');

function speakerSelect(selected) {
  const options = [['', 'Narrator'], ...speakerNames.map(n => [n, n]), [OTHER_SPEAKER, OTHER_SPEAKER]];
  return `<select class="text-input" data-speaker aria-label="Speaker">${options.map(([value, label]) =>
    `<option value="${escapeHtml(value)}" ${value === selected ? 'selected' : ''}>${escapeHtml(label)}</option>`).join('')}</select>`;
}

// An excerpt can hold several of the character's sentences (close together); they are marked in the passage.
function cardFor(chapterId, item) {
  const hits = item.paragraphs.flat().filter(x => x.hit);
  const open = `<a class="btn btn-sm btn-ghost" href="${readerUrl(BOOK, chapterId, item.segment_index)}">Open in reader</a>`;
  const hint = hits.length > 1 ? '<span class="excerpt-note">Click a marked sentence to leave it out of a change</span>' : '';
  const set = hits.some(x => x.overridden) ? '<span class="excerpt-note">some set by you</span>' : '';
  const html = excerptCard({
    paragraphs: item.paragraphs, meta: [hint, set].filter(Boolean).join(' '),
    actions: `${open}<label class="excerpt-note">${hits.length > 1 ? 'Speaker for marked sentences' : 'Speaker'} ${speakerSelect(character.name)}</label>`,
  });
  return html.replace('class="excerpt"', `class="excerpt${hits.length > 1 ? ' multi' : ''}" data-chapter="${chapterId}"`);
}

// Chapters are listed up front; a chapter's sentences are fetched when it is first opened.
function renderChapters(chapters) {
  list.innerHTML = chapters.map(g => `
    <details class="chapter-group" data-chapter="${g.id}">
      <summary>
        <span class="chapter-group-title">${escapeHtml(g.title)}</span>
        <span class="excerpt-note" data-count>${g.count ?? '?'}</span>
        <span class="chapter-group-actions"><a class="btn btn-sm btn-ghost" href="${readerUrl(BOOK, g.id)}">Open chapter</a></span>
      </summary>
    </details>`).join('');
}

async function fillChapter(group) {
  if (group.dataset.loaded) return;
  group.dataset.loaded = '1';
  const chapterId = +group.dataset.chapter;
  const items = await fetch(`/api/books/${BOOK}/characters/${window.CHARACTER_ID}/excerpts/${chapterId}`).then(r => r.json());
  if (!items.length) { group.remove(); updateSummary(); return; }       // it turned out not to have any of their lines
  group.querySelector('[data-count]').textContent = items.reduce((n, e) => n + e.paragraphs.flat().filter(x => x.hit).length, 0);
  group.insertAdjacentHTML('beforeend', items.map(item => cardFor(chapterId, item)).join(''));
  updateSummary();
  applyFilter();
}

function updateSummary() {
  const groups = [...list.querySelectorAll('.chapter-group')];
  const known = groups.reduce((n, g) => n + (+g.querySelector('[data-count]').textContent || 0), 0);
  const unknown = groups.filter(g => g.querySelector('[data-count]').textContent === '?').length;
  document.getElementById('summary').textContent = groups.length
    ? `${known} sentence${known === 1 ? '' : 's'} spoken by ${character.name} in ${groups.length} chapter${groups.length === 1 ? '' : 's'}`
      + (unknown ? ` (${unknown} not counted until opened)` : '')
    : '';
  document.getElementById('toolbar').hidden = groups.length === 0;
  document.getElementById('empty').hidden = groups.length !== 0;
}

async function load() {
  const page = await fetch(`/api/books/${BOOK}/characters/${window.CHARACTER_ID}/excerpts`).then(r => r.json());
  character = page.character;
  renderChapters(page.chapters);
  updateSummary();
  const first = list.querySelector('.chapter-group');
  if (first) { first.open = true; fillChapter(first); }
}

function applyFilter() {
  const q = document.getElementById('filter').value.trim().toLowerCase();
  list.querySelectorAll('.chapter-group').forEach(group => {
    const titleMatches = group.querySelector('summary').textContent.toLowerCase().includes(q);
    const cards = [...group.querySelectorAll('.excerpt')];
    cards.forEach(el => { el.hidden = !!q && !titleMatches && !el.querySelector('.passage-para').parentElement.textContent.toLowerCase().includes(q); });
    group.hidden = !!q && !titleMatches && cards.every(el => el.hidden);   // chapters not opened yet have no cards to match
  });
}

async function changeSpeaker(select) {
  const card = select.closest('.excerpt');
  const value = select.value;
  const hits = [...card.querySelectorAll('.hit:not(.off)')];
  if (!hits.length) return;
  select.disabled = true;
  const r = await fetch(`/api/books/${BOOK}/chapters/${card.dataset.chapter}/speaker`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ anchors: hits.map(h => h.dataset.anchor), character: value || null }),
  });
  select.disabled = false;
  if (!r.ok) { document.getElementById('summary').textContent = (await r.json()).error || 'Could not change the speaker'; return; }
  // The sentences stay, so a slip can be put back from here; they just stop looking like one of theirs.
  hits.forEach(h => h.classList.toggle('moved', value !== character.name));
  card.querySelector('[data-moved]')?.remove();
  const moved = card.querySelectorAll('.hit.moved').length;
  if (moved) {
    const status = Object.assign(document.createElement('span'), { className: 'badge-status ignored', textContent: `${moved > 1 ? moved + ' sentences' : 'Now'} spoken by ${value || 'Narrator'}` });
    status.dataset.moved = '';
    card.querySelector('.excerpt-actions').append(status);
  }
}

list.addEventListener('toggle', e => { if (e.target.open) fillChapter(e.target); }, true);
list.addEventListener('click', e => {
  const hit = e.target.closest('.hit');
  if (hit?.closest('.excerpt').classList.contains('multi')) hit.classList.toggle('off');
});
list.addEventListener('change', e => { if (e.target.matches('[data-speaker]')) changeSpeaker(e.target); });
document.getElementById('filter').addEventListener('input', applyFilter);

(async () => {
  const chars = await fetch(`/api/books/${BOOK}/characters`).then(r => r.json());
  speakerNames = chars.map(c => c.name);
  character = chars.find(c => c.id === window.CHARACTER_ID);
  const target = character && chars.find(c => c.id === character.alias_of);
  if (target) character = target;                 // a merged character's lines are listed under who they are merged into
  await load();
})();
