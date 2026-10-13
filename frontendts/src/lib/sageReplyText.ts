// Sage ends an answer with one "Sources: a · b" line (AnswerStyle in src/dependencies/system_prompt.py); the
// reply panel shows those as chips under the answer.

const SOURCES = /^\s*(?:\*\*|_)?Sources?:(?:\*\*|_)?\s*(.+?)\s*$/i;

/** The reply's text and its sources, taken off a last "Sources: a · b" line. */
export function splitSources(text: string): { body: string; sources: string[] } {
  const lines = text.trimEnd().split('\n');
  const match = (lines[lines.length - 1] ?? '').match(SOURCES);
  if (!match) return { body: text, sources: [] };
  const sources = match[1]
    .replace(/[_*()]/g, '')
    .split(/\s*(?:·|•|;|\|)\s*/)
    .map((s) => s.trim().replace(/\.$/, ''))
    .filter(Boolean);
  return { body: lines.slice(0, -1).join('\n').trimEnd(), sources };
}
