import type { MapTreeResponse } from '@/lib/types';

// A question is shown in Sage's conversation the moment it is sent; the server's copy arrives a moment later. The
// reader's own line is shown only until the conversation gains a question, so it is never on the screen twice
// and never comes back later (matching by its words would hide a question asked a second time, and would show an
// old one again when someone else's question arrived).

export interface PendingQuestion {
  text: string;
  /** How many questions the conversation held when this one was sent. */
  after: number;
}

/** How many questions (the reader's messages) the conversation on screen holds. */
export function questionsHeld(tree: MapTreeResponse | null | undefined): number {
  return (tree?.tree ?? []).reduce((n, node) => n + node.messages.filter((m) => m.role === 'user').length, 0);
}

/** The words to show as the reader's line, or null once the conversation holds the question. */
export function pendingText(pending: PendingQuestion | null, held: number): string | null {
  return pending && pending.after === held ? pending.text : null;
}
