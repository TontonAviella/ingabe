import { describe, expect, it } from 'vitest';
import { pendingText, questionsHeld } from '@/lib/sagePending';
import type { MapTreeResponse, SanitizedMessage } from '@/lib/types';

const said = (role: string, content: string): SanitizedMessage => ({ role, content, has_tool_calls: false });
const tree = (...messages: SanitizedMessage[]): MapTreeResponse => ({
  project_id: 'P1',
  tree: [{ map_id: 'M1', messages, fork_reason: null, created_on: '2026-11-23T10:00:00Z', diff_from_previous: null }],
});

describe('the question just sent', () => {
  const first = tree(said('user', 'Is it drier than normal here right now?'), said('assistant', 'No.'));

  it("is the reader's line until the conversation gains a question", () => {
    const pending = { text: 'And last year?', after: questionsHeld(first) };
    expect(pendingText(pending, questionsHeld(first))).toBe('And last year?');
    const arrived = tree(...first.tree[0].messages, said('user', 'And last year?'));
    expect(pendingText(pending, questionsHeld(arrived))).toBeNull();
  });

  it('is shown when the same words are asked a second time', () => {
    const again = { text: 'Is it drier than normal here right now?', after: questionsHeld(first) };
    expect(pendingText(again, questionsHeld(first))).toBe('Is it drier than normal here right now?');
  });

  it('does not come back when a later question arrives', () => {
    const pending = { text: 'And last year?', after: 1 };
    const later = tree(
      ...first.tree[0].messages,
      said('user', 'And last year?'),
      said('assistant', 'Wetter.'),
      said('user', 'Another one'),
    );
    expect(pendingText(pending, questionsHeld(later))).toBeNull();
  });

  it('counts nothing where there is no conversation yet', () => {
    expect(questionsHeld(null)).toBe(0);
    expect(pendingText(null, 0)).toBeNull();
  });
});
