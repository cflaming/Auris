// Sentence "cards" shared wherever the app lists excerpts of the book: a sentence with the one before
// it for context, one or more speaker lines, some metadata and actions. Used by the speaker
// corrections page and the reader's new-character preview; a per-character excerpts page can reuse it.

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

const speakerLabel = name => name || 'Narrator';

// Opens the reader at a sentence (the reader reads these query parameters on load).
function readerUrl(bookId, chapterId, segmentIndex) {
  return `/reader/${bookId}?chapter=${chapterId}` + (segmentIndex != null ? `&segment=${segmentIndex}` : '');
}

// lines: [{kind: 'del' | 'add' | 'plain', label}], meta/actions: HTML strings (already escaped).
function excerptCard({ prev, text, lines = [], meta = '', actions = '' }) {
  const lineHtml = lines.map(l => {
    const sign = l.kind === 'del' ? '− ' : l.kind === 'add' ? '+ ' : '';
    return `<div class="diff-line ${l.kind === 'plain' ? '' : l.kind}">${sign}${escapeHtml(l.label)}</div>`;
  }).join('');
  return `<div class="excerpt">
    ${prev ? `<div class="diff-ctx">${escapeHtml(prev)}</div>` : ''}
    <div class="diff-text">${escapeHtml(text ?? '(sentence no longer in the text)')}</div>
    ${lineHtml}
    ${meta ? `<div class="excerpt-meta">${meta}</div>` : ''}
    ${actions ? `<div class="excerpt-actions">${actions}</div>` : ''}
  </div>`;
}
