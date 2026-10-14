import { describe, expect, it } from 'vitest';
import { deckOf, litParts, nextToAsk, suggestedLine, unanswered } from '@/lib/photoCheck';
import type { PartLabel, PartWork, PhotoWork } from '@/lib/towerContract';
import { emptyPhotoDraft, photoReducer } from '@/lib/towerReview';

const PARTS: PartLabel[] = [
  { id: 'tower_dish', label: 'Microwave dish', plural: 'microwave dishes', scanned: true },
  { id: 'tower_antenna', label: 'Panel antenna', plural: 'panel antennas', scanned: true },
  { id: 'tower_radio_unit', label: 'Radio unit', plural: 'radio units', scanned: true },
];
const none: PartWork = {
  marks: [],
  lookalike_marks: [],
  job: null,
  suggestions: [],
  reviewed: { kept: [], rejected: [], fixed: [], added: [] },
  counted_by_you: null,
  waiting: 0,
};
const found = (n: number, status = 'done'): PartWork => ({
  ...none,
  job: { job_id: 'e'.repeat(64), status },
  suggestions: Array.from({ length: n }, (_, i) => ({ id: `s${i + 1}`, box: [i, i, i + 10, i + 10], score: 0.9 - i / 10 })),
  waiting: n,
});
const WORK: PhotoWork = { parts: { tower_dish: found(0), tower_antenna: found(2), tower_radio_unit: found(1, 'running') } };

describe('the photo check', () => {
  it('asks only about parts whose finding ended, part by part in the screen order, best first', () => {
    expect(deckOf(WORK, PARTS).map((d) => d.key)).toEqual(['tower_antenna:s1', 'tower_antenna:s2']);
    expect(deckOf(undefined, PARTS)).toEqual([]);
  });

  it('goes on to the next unanswered one, going round, and ends when every one is answered', () => {
    const deck = deckOf({ parts: { ...WORK.parts, tower_radio_unit: found(1) } }, PARTS);
    let draft = emptyPhotoDraft();
    expect(nextToAsk(deck, draft, null)?.key).toBe('tower_antenna:s1');
    draft = photoReducer(draft, { type: 'review/keep', part: 'tower_antenna', id: 's2' });
    expect(nextToAsk(deck, draft, 'tower_antenna:s1')?.key).toBe('tower_radio_unit:s1');
    expect(nextToAsk(deck, draft, 'tower_radio_unit:s1')?.key).toBe('tower_antenna:s1');
    draft = photoReducer(draft, { type: 'review/reject', part: 'tower_antenna', id: 's1' });
    draft = photoReducer(draft, { type: 'review/keep', part: 'tower_radio_unit', id: 's1' });
    expect(unanswered(deck, draft)).toEqual([]);
    expect(nextToAsk(deck, draft, null)).toBeNull();
  });

  it('says what the model suggests as suggestions, and the parts it suggests none of', () => {
    expect(suggestedLine(WORK, PARTS)).toEqual({ some: '2 panel antennas', none: ['microwave dish'] });
    expect(suggestedLine({ parts: { ...WORK.parts, tower_radio_unit: found(1) } }, PARTS).some).toBe('2 panel antennas and 1 radio unit');
  });

  it('lights up the parts a question names, among the inspection parts', () => {
    const withBolts: PartLabel[] = [...PARTS, { id: 'tower_bolt_missing', label: 'Missing bolt', plural: 'missing bolts' }];
    expect(litParts('equipment', withBolts)).toEqual(['tower_dish', 'tower_antenna', 'tower_radio_unit']);
    expect(litParts('defects', withBolts)).toEqual(['tower_bolt_missing']);
    expect(litParts('antennas', withBolts)).toEqual(['tower_antenna']);
    expect(litParts(null, withBolts)).toEqual([]);
  });
});
