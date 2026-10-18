function fallbackCopy(input, done) {
  input.select();
  input.setSelectionRange(0, 99999);
  try { document.execCommand('copy'); done(); } catch (_) {}
}

function trashIcon() {
  return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="14" height="14" fill="currentColor" aria-hidden="true"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>';
}

async function loadBooks() {
  const grid = document.getElementById('book-grid');
  const books = await fetch('/api/books').then(r => r.json());

  const countEl = document.getElementById('library-count');
  if (countEl) countEl.textContent = books.length
    ? books.length + (books.length === 1 ? ' title' : ' titles')
    : '';

  if (!books.length) {
    grid.innerHTML = `
      <div class="empty-library">
        <p>Your library is empty.</p>
        <p class="sub">Import an EPUB, PDF, or TXT file to get started.</p>
      </div>`;
    return;
  }

  grid.innerHTML = books.map(b => {
    const coverHtml = b.cover_url
      ? `<img src="${b.cover_url}" alt="" loading="lazy">`
      : `<div class="book-cover-placeholder">${esc(b.title)}</div>`;
    const hasProgress = Number.isInteger(b.progress_chapter_id) || Number.isInteger(Number.parseInt(b.progress_chapter_id, 10));
    const progressPosition = Math.max(0, Number.parseInt(b.progress_position, 10) || 0);
    const actionLabel = hasProgress ? 'Continue' : 'Read';
    const progressMeta = hasProgress
      ? `<div class="book-progress-hint">Continue from ${esc(b.progress_chapter_title || 'saved position')} &middot; seg ${progressPosition + 1}</div>`
      : '';

    return `
    <div class="book-card" data-id="${b.id}">
      <div class="book-cover">${coverHtml}</div>
      <span class="book-type-badge">${esc(b.file_type)}</span>
      <div class="book-info">
        <div class="book-title">${esc(b.title)}</div>
        <div class="book-author">${esc(b.author || 'Unknown')}</div>
        <div class="book-author" style="margin-top:3px;font-size:.68rem">
          ${b.total_chapters} section${b.total_chapters !== 1 ? 's' : ''}
        </div>
        ${progressMeta}
      </div>
      <div class="book-actions">
        <a href="/reader/${b.id}">${actionLabel}</a>
        <button class="rss-btn" onclick="showRssFeed(event,${b.id})" title="Podcast RSS feed"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="14" height="14" fill="currentColor" aria-hidden="true"><circle cx="6.18" cy="17.82" r="2.18"/><path d="M4 4.44v2.83c7.03 0 12.73 5.7 12.73 12.73h2.83c0-8.59-6.97-15.56-15.56-15.56zm0 5.66v2.83c3.9 0 7.07 3.17 7.07 7.07h2.83c0-5.47-4.43-9.9-9.9-9.9z"/></svg></button>
        <button class="del-btn" onclick="deleteBook(event,${b.id})" title="Remove book">${trashIcon()}</button>
      </div>
    </div>`;
  }).join('');
}

function showRssFeed(e, id) {
  e.stopPropagation();
  const url = `${location.origin}/api/books/${id}/podcast.rss`;
  let popup = document.getElementById('rss-popup');
  if (!popup) {
    popup = document.createElement('div');
    popup.id = 'rss-popup';
    popup.className = 'rss-popup';
    popup.innerHTML = `
      <div class="rss-popup-label">Podcast RSS feed — paste into your podcast player</div>
      <div class="rss-popup-row">
        <input class="rss-popup-url" readonly>
        <button class="rss-popup-copy">Copy</button>
      </div>`;
    popup.querySelector('.rss-popup-copy').addEventListener('click', () => {
      const input = popup.querySelector('.rss-popup-url');
      const btn = popup.querySelector('.rss-popup-copy');
      const done = () => { btn.textContent = 'Copied!'; setTimeout(() => { btn.textContent = 'Copy'; }, 2000); };
      if (navigator.clipboard) {
        navigator.clipboard.writeText(input.value).then(done).catch(() => fallbackCopy(input, done));
      } else {
        fallbackCopy(input, done);
      }
    });
    document.body.appendChild(popup);
    document.addEventListener('click', (ev) => {
      if (!popup.contains(ev.target) && !ev.target.classList.contains('rss-btn')) {
        popup.classList.remove('visible');
      }
    }, { capture: true });
  }
  popup.querySelector('.rss-popup-url').value = url;
  popup.classList.add('visible');
}

async function deleteBook(e, id) {
  e.stopPropagation();
  const btn = e.currentTarget;
  const resetBtn = () => {
    btn.innerHTML = trashIcon();
    btn.title = 'Remove book';
    btn.classList.remove('del-confirming');
    delete btn.dataset.confirming;
    delete btn.dataset.timer;
  };
  if (btn.dataset.confirming) {
    clearTimeout(Number(btn.dataset.timer));
    resetBtn();
    await fetch(`/api/books/${id}`, { method: 'DELETE' });
    loadBooks();
    return;
  }
  btn.dataset.confirming = '1';
  btn.innerHTML = '<span style="font-size:.7rem;font-weight:600;letter-spacing:.01em">Sure?</span>';
  btn.title = 'Click again to confirm';
  btn.classList.add('del-confirming');
  btn.dataset.timer = setTimeout(resetBtn, 3000);
}

document.getElementById('file-input').addEventListener('change', async function() {
  const file = this.files[0];
  if (!file) return;
  const status = document.getElementById('import-status');
  status.textContent = `Importing “${file.name}”…`;
  status.className = 'import-status';
  status.classList.remove('hidden');
  const fd = new FormData();
  fd.append('file', file);
  try {
    const r = await fetch('/api/books/import', { method: 'POST', body: fd });
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    status.textContent = `“${d.title}” imported — ${d.chapters} sections. Detecting characters in background…`;
    loadBooks();
  } catch(e) {
    status.textContent = e.message;
    status.className = 'import-status error';
  }
  this.value = '';
});

function esc(s) {
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

loadBooks();
