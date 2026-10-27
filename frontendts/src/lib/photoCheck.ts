import type { Box, Point } from '@/lib/photoView';
import type { CountedPart, PartLabel, PhotoWork, Suggestion, TowerPart } from '@/lib/towerContract';
import { jobState, type PhotoDraft } from '@/lib/towerReview';

// The photo screen's check: the suggestions of the parts the trained models found by themselves, asked about one at
// a time, part by part in the screen's order and best first within a part (the server's order). Nothing here
// counts: the server counts each part once its suggestions are all answered and the review saved.

/** Each part's colour on the photo: colour means which part, nothing else. */
export const PART_COLOR: Record<TowerPart, string> = {
  tower_antenna: '#8FD3FF',
  tower_radio_unit: '#FFB35C',
  tower_dish: '#FF8FB4',
  maize_seedling: '#F2C14E',
  tower_bolt_missing: '#FF8FB4',
  tower_bolt_loose: '#FFB35C',
};

export interface DeckItem {
  part: TowerPart;
  suggestion: Suggestion;
  /** `${part}:${suggestion id}`: the screen's key for it. */
  key: string;
}

/** Every suggestion of the scanned parts whose finding has ended, in the order they are asked about. */
export function deckOf(work: PhotoWork | undefined, scanned: PartLabel[]): DeckItem[] {
  if (!work) return [];
  return scanned.flatMap(({ id }) => {
    const partWork = work.parts[id];
    if (!partWork || !jobState(partWork.job).done) return [];
    return partWork.suggestions.map((suggestion) => ({ part: id, suggestion, key: `${id}:${suggestion.id}` }));
  });
}

/** The items the person has not answered yet, in order. */
export function unanswered(deck: DeckItem[], draft: PhotoDraft): DeckItem[] {
  return deck.filter(({ part, suggestion }) => !draft[part].review.decisions[suggestion.id]);
}

/** The next item to ask about after `afterKey` (going round), or null when every one is answered. */
export function nextToAsk(deck: DeckItem[], draft: PhotoDraft, afterKey: string | null): DeckItem | null {
  const open = unanswered(deck, draft);
  if (open.length === 0) return null;
  const at = afterKey === null ? -1 : deck.findIndex((d) => d.key === afterKey);
  return open.find((d) => deck.indexOf(d) > at) ?? open[0];
}

function inside([x, y]: Point, [x0, y0, x1, y1]: Box): boolean {
  return x >= x0 && x <= x1 && y >= y0 && y <= y1;
}

/** The close look's outlined parts of the same part inside one of the model's boxes: "Yes, 3 separate" when 2 or more. */
export function countedInside(counted: CountedPart[], part: TowerPart, box: Box): CountedPart[] {
  return counted.filter((c) => c.part === part && c.check === null && inside(c.point, box));
}

export interface MissedPart {
  /** `x:${index in the close look's parts}`: the screen's key for it. */
  key: string;
  counted: CountedPart;
}

/** The parts the close look counted that the model did not suggest: their point is in none of the model's boxes of
 * that part (whatever the person answered) nor in a box the person added. Only the scanned parts, once found. */
export function missedByModel(counted: CountedPart[], work: PhotoWork | undefined, draft: PhotoDraft, scanned: PartLabel[]): MissedPart[] {
  if (!work) return [];
  return counted.flatMap((c, i) => {
    const partWork = work.parts[c.part];
    if (!scanned.some((p) => p.id === c.part) || !partWork || !jobState(partWork.job).done) return [];
    const review = draft[c.part].review;
    const boxes = [
      ...partWork.suggestions.map((s) => s.box),
      ...Object.values(review.decisions).flatMap((d) => (d.kind === 'fixed' ? [d.box] : [])),
      ...review.added,
    ];
    return boxes.some((b) => inside(c.point, b)) ? [] : [{ key: `x:${i}`, counted: c }];
  });
}

/** "8 panel antennas and 3 radio units outlined, 2 panel antennas and 4 radio units to check": what the close look counted, part by part
 * in the screen's order. Suggestions for a person, never the photo's count. */
export function countedWords(counted: CountedPart[], parts: PartLabel[]): string | null {
  const rows = parts
    .map(({ id, label, plural }) => {
      const outlined = counted.filter((c) => c.part === id && c.check === null).length;
      const toCheck = counted.filter((c) => c.part === id && c.check !== null).length;
      const name = (n: number) => (n === 1 ? label : plural).toLowerCase();
      return { outlined, toCheck, name };
    })
    .filter((r) => r.outlined + r.toCheck > 0);
  if (rows.length === 0) return null;
  const join = (words: string[]) => (words.length > 1 ? `${words.slice(0, -1).join(', ')} and ${words[words.length - 1]}` : words[0]);
  const line = `${join(rows.map((r) => `${r.outlined} ${r.name(r.outlined)}`))} outlined`;
  const more = rows.filter((r) => r.toCheck > 0);
  return more.length ? `${line}, ${join(more.map((r) => `${r.toCheck} ${r.name(r.toCheck)}`))} to check` : line;
}

/** The parts a question lights up on the photo (PhotoQuestion.lights), among this inspection's parts. */
export function litParts(lights: string | null, parts: PartLabel[]): TowerPart[] {
  const ids = parts.map((p) => p.id);
  if (lights === 'antennas') return ids.filter((id) => id === 'tower_antenna');
  if (lights === 'defects') return ids.filter((id) => id === 'tower_bolt_missing' || id === 'tower_bolt_loose');
  if (lights === 'equipment') return ids.filter((id) => id !== 'tower_bolt_missing' && id !== 'tower_bolt_loose');
  return [];
}

/** "6 panel antennas and 6 radio units": what the model suggests on the photo, part by part (suggestions, never a
 * count), and the parts it suggests none of. */
export function suggestedLine(work: PhotoWork | undefined, scanned: PartLabel[]): { some: string; none: string[] } {
  const some: string[] = [];
  const none: string[] = [];
  for (const { id, label, plural } of scanned) {
    const partWork = work?.parts[id];
    if (!partWork || !jobState(partWork.job).done) continue;
    const n = partWork.suggestions.length;
    if (n === 0) none.push(label.toLowerCase());
    else some.push(`${n} ${n === 1 ? label.toLowerCase() : plural}`);
  }
  const joined = some.length > 1 ? `${some.slice(0, -1).join(', ')} and ${some[some.length - 1]}` : (some[0] ?? '');
  return { some: joined, none };
}

/** A score as the check card shows it: high, or lower (look closely). It is the model's own score, not a measured
 * chance of being right. */
export function scoreWords(score: number): string {
  return score >= 0.7 ? 'High model score' : 'Lower model score: look closely';
}
