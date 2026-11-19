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
// Context is one line of preceding text (`prev`), or the passage as the chapter has it: `paragraphs`, a list
// of paragraphs of sentences [{text, anchor, hit}], where the hits are the sentences the card is about.
function excerptCard({ prev, text, paragraphs, lines = [], meta = '', actions = '' }) {
  const lineHtml = lines.map(l => {
    const sign = l.kind === 'del' ? '− ' : l.kind === 'add' ? '+ ' : '';
    return `<div class="diff-line ${l.kind === 'plain' ? '' : l.kind}">${sign}${escapeHtml(l.label)}</div>`;
  }).join('');
  const shown = escapeHtml(text ?? '(sentence no longer in the text)');
  const sentence = x => x.hit
    ? `<span class="hit" data-anchor="${escapeHtml(x.anchor)}">${escapeHtml(x.text)}</span>`
    : escapeHtml(x.text);
  const context = paragraphs
    ? paragraphs.map(p => `<p class="passage-para">${p.map(sentence).join(' ')}</p>`).join('')
    : `${prev ? `<div class="diff-ctx">${escapeHtml(prev)}</div>` : ''}
    <div class="diff-text">${shown}</div>`;
  return `<div class="excerpt">
    ${context}
    ${lineHtml}
    ${meta ? `<div class="excerpt-meta">${meta}</div>` : ''}
    ${actions ? `<div class="excerpt-actions">${actions}</div>` : ''}
  </div>`;
}
