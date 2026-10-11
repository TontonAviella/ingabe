import { describe, expect, it } from 'vitest';
import type { Box } from './photoView';
import type { PartWork, PhotoWork, Suggestion } from './towerContract';
import {
  countLine,
  draftFromServer,
  emptyPhotoDraft,
  jobState,
  marksPayload,
  nextPhotoToCheck,
  nextUndecided,
  type PhotoDraft,
  photoNeedsCheck,
  photoReducer,
  reviewPayload,
  syncPart,
} from './towerReview';

// Shapes copied from the shared API contract (GET .../photos/{sha256}/work).
const D1: Box = [100, 120, 220, 240];
const D2: Box = [400, 110, 515, 228];
const D3: Box = [700, 130, 812, 245];
const A1: Box = [1500, 300, 1530, 520];

function suggestion(id: string, box: Box, score = 0.42): Suggestion {
  return { id, box, score };
}

function part(over: Partial<PartWork> = {}): PartWork {
  return {
    marks: [],
    lookalike_marks: [],
    job: null,
    suggestions: [],
    reviewed: { kept: [], rejected: [], fixed: [], added: [] },
    counted_by_you: null,
    waiting: 0,
    ...over,
  };
}

function work(over: Partial<PhotoWork['parts']> = {}): PhotoWork {
  return { parts: { tower_dish: part(), tower_antenna: part(), tower_radio_unit: part(), ...over } };
}

const SUGGESTIONS = [suggestion('s1', [900, 100, 1010, 215]), suggestion('s2', [1100, 90, 1205, 200]), suggestion('s3', [40, 40, 60, 60])];

function synced(w: PhotoWork): PhotoDraft {
  return photoReducer(emptyPhotoDraft(), { type: 'sync', work: w });
}

describe('marking: add, delete and fix marks', () => {
  it('adds marks to the chosen part only', () => {
    let s = synced(work());
    s = photoReducer(s, { type: 'mark/add', part: 'tower_dish', box: D1 });
    s = photoReducer(s, { type: 'mark/add', part: 'tower_dish', box: D2 });
    s = photoReducer(s, { type: 'mark/add', part: 'tower_antenna', box: A1 });
    expect(s.tower_dish.marks).toEqual([D1, D2]);
    expect(s.tower_antenna.marks).toEqual([A1]);
    expect(s.tower_radio_unit.marks).toEqual([]);
    expect(s.tower_dish.marksDirty).toBe(true);
    expect(s.tower_radio_unit.marksDirty).toBe(false);
  });

  it('deletes one mark and corrects another in place', () => {
    let s = synced(work({ tower_dish: part({ marks: [D1, D2, D3] }) }));
    s = photoReducer(s, { type: 'mark/delete', part: 'tower_dish', index: 1 });
    expect(s.tower_dish.marks).toEqual([D1, D3]);
    const fixed: Box = [702, 128, 820, 250];
    s = photoReducer(s, { type: 'mark/fix', part: 'tower_dish', index: 1, box: fixed });
    expect(s.tower_dish.marks).toEqual([D1, fixed]);
  });

  it('sends the other parts’ marks as look-alikes', () => {
    let s = synced(work());
    for (const box of [D1, D2, D3]) s = photoReducer(s, { type: 'mark/add', part: 'tower_dish', box });
    s = photoReducer(s, { type: 'mark/add', part: 'tower_antenna', box: A1 });
    expect(marksPayload('tower_dish', s)).toEqual({ part: 'tower_dish', marks: [D1, D2, D3], lookalike_marks: [A1] });
    expect(marksPayload('tower_antenna', s)).toEqual({ part: 'tower_antenna', marks: [A1], lookalike_marks: [D1, D2, D3] });
    expect(marksPayload('tower_radio_unit', s)).toEqual({ part: 'tower_radio_unit', marks: [], lookalike_marks: [D1, D2, D3, A1] });
  });

  it("keeps unsaved marks when the server's work refreshes, and takes the server's once saved", () => {
    let s = synced(work({ tower_dish: part({ marks: [D1] }) }));
    s = photoReducer(s, { type: 'mark/add', part: 'tower_dish', box: D2 });
    s = photoReducer(s, { type: 'sync', work: work({ tower_dish: part({ marks: [D1], job: { job_id: 'j', status: 'running' } }) }) });
    expect(s.tower_dish.marks).toEqual([D1, D2]);
    s = photoReducer(s, { type: 'saved', part: 'tower_dish', work: part({ marks: [D1, D2] }) });
    expect(s.tower_dish).toMatchObject({ marks: [D1, D2], marksDirty: false });
  });
});

describe('review payload: kept, rejected, fixed, added', () => {
  const dishWork = part({ marks: [D1, D2, D3], suggestions: SUGGESTIONS, waiting: 3 });

  it('builds the review from each decision, in the order of the suggestions', () => {
    let s = synced(work({ tower_dish: dishWork }));
    const corrected: Box = [1098, 88, 1210, 204];
    const missed: Box = [1300, 95, 1404, 199];
    s = photoReducer(s, { type: 'review/reject', part: 'tower_dish', id: 's3' });
    s = photoReducer(s, { type: 'review/keep', part: 'tower_dish', id: 's1' });
    s = photoReducer(s, { type: 'review/fix', part: 'tower_dish', id: 's2', box: corrected });
    s = photoReducer(s, { type: 'review/add', part: 'tower_dish', box: missed });
    expect(reviewPayload('tower_dish', s.tower_dish.review, SUGGESTIONS)).toEqual({
      part: 'tower_dish',
      kept: ['s1'],
      rejected: ['s3'],
      fixed: [{ id: 's2', box: corrected }],
      added: [missed],
    });
  });

  it('keeps a correction when the box is then kept, and drops a decision on undo', () => {
    let s = synced(work({ tower_dish: dishWork }));
    const corrected: Box = [905, 102, 1000, 210];
    s = photoReducer(s, { type: 'review/fix', part: 'tower_dish', id: 's1', box: corrected });
    s = photoReducer(s, { type: 'review/keep', part: 'tower_dish', id: 's1' });
    s = photoReducer(s, { type: 'review/reject', part: 'tower_dish', id: 's2' });
    s = photoReducer(s, { type: 'review/undo', part: 'tower_dish', id: 's2' });
    expect(reviewPayload('tower_dish', s.tower_dish.review, SUGGESTIONS)).toEqual({
      part: 'tower_dish',
      kept: [],
      rejected: [],
      fixed: [{ id: 's1', box: corrected }],
      added: [],
    });
  });

  it('corrects and removes boxes the person added', () => {
    let s = synced(work({ tower_dish: dishWork }));
    const a: Box = [10, 10, 50, 50];
    const b: Box = [60, 60, 90, 90];
    s = photoReducer(s, { type: 'review/add', part: 'tower_dish', box: a });
    s = photoReducer(s, { type: 'review/add', part: 'tower_dish', box: b });
    s = photoReducer(s, { type: 'review/fix-added', part: 'tower_dish', index: 0, box: [12, 12, 48, 52] });
    s = photoReducer(s, { type: 'review/delete-added', part: 'tower_dish', index: 1 });
    expect(reviewPayload('tower_dish', s.tower_dish.review, SUGGESTIONS).added).toEqual([[12, 12, 48, 52]]);
  });

  it("starts from the server's saved review, and leaves out decisions about boxes no longer suggested", () => {
    const saved = part({
      suggestions: SUGGESTIONS,
      reviewed: { kept: ['s1'], rejected: ['old'], fixed: [{ id: 's2', box: [1, 2, 3, 4] }], added: [[5, 6, 7, 8]] },
    });
    const draft = draftFromServer(saved);
    expect(draft.dirty).toBe(false);
    expect(reviewPayload('tower_dish', draft, SUGGESTIONS)).toEqual({
      part: 'tower_dish',
      kept: ['s1'],
      rejected: [],
      fixed: [{ id: 's2', box: [1, 2, 3, 4] }],
      added: [[5, 6, 7, 8]],
    });
  });

  it('a new find replaces the decisions, even unsaved ones about the old boxes', () => {
    let s = synced(work({ tower_dish: dishWork }));
    s = photoReducer(s, { type: 'review/keep', part: 'tower_dish', id: 's1' });
    const refreshed = syncPart(s.tower_dish, { ...dishWork });
    expect(refreshed.review.decisions.s1).toEqual({ kind: 'kept' });
    const newFind = syncPart(s.tower_dish, part({ marks: [D1, D2, D3], suggestions: [suggestion('n1', [1, 1, 9, 9])], waiting: 1 }));
    expect(newFind.review).toMatchObject({ decisions: {}, dirty: false });
  });
});

describe('never a total while suggestions wait', () => {
  it('shows only how many wait while the server says some wait, even if it also sent a count', () => {
    const line = countLine(part({ suggestions: SUGGESTIONS, waiting: 3, counted_by_you: 7 }), null);
    expect(line).toBe('3 suggestions waiting for your check');
    expect(line).not.toContain('7');
  });

  it("shows the server's counted_by_you once nothing waits", () => {
    expect(countLine(part({ suggestions: SUGGESTIONS, waiting: 0, counted_by_you: 5 }), null)).toBe('5 counted by you');
    expect(countLine(part({ waiting: 0, counted_by_you: 0 }), null)).toBe('0 counted by you');
    expect(countLine(part(), null)).toBeNull();
  });

  it('while the person checks, counts the boxes left to check and never adds up a total', () => {
    const w = part({ marks: [D1, D2, D3], suggestions: SUGGESTIONS, waiting: 3, counted_by_you: null });
    let s = synced(work({ tower_dish: w }));
    s = photoReducer(s, { type: 'review/keep', part: 'tower_dish', id: 's1' });
    s = photoReducer(s, { type: 'review/add', part: 'tower_dish', box: [1, 1, 20, 20] });
    expect(countLine(w, s.tower_dish.review)).toBe('2 suggestions waiting for your check');
    s = photoReducer(s, { type: 'review/keep', part: 'tower_dish', id: 's2' });
    expect(countLine(w, s.tower_dish.review)).toBe('1 suggestion waiting for your check');
    s = photoReducer(s, { type: 'review/reject', part: 'tower_dish', id: 's3' });
    // Everything decided but not saved: still no number until the server counts it.
    const line = countLine(w, s.tower_dish.review);
    expect(line).toBe('All checked: save your review to count them');
    expect(line).not.toMatch(/\d/);
  });

  it("an unsaved draft hides the server's earlier count", () => {
    const w = part({
      suggestions: SUGGESTIONS,
      waiting: 0,
      counted_by_you: 6,
      reviewed: { kept: ['s1', 's2', 's3'], rejected: [], fixed: [], added: [] },
    });
    let s = synced(work({ tower_dish: w }));
    expect(countLine(w, s.tower_dish.review)).toBe('6 counted by you');
    s = photoReducer(s, { type: 'review/undo', part: 'tower_dish', id: 's2' });
    expect(countLine(w, s.tower_dish.review)).toBe('1 suggestion waiting for your check');
  });
});

describe('stepping through the suggestions', () => {
  it('goes to the next one still to check, after the one just decided', () => {
    let s = synced(work({ tower_dish: part({ suggestions: SUGGESTIONS, waiting: 3 }) }));
    expect(nextUndecided(s.tower_dish.review, SUGGESTIONS, null)?.id).toBe('s1');
    expect(nextUndecided(s.tower_dish.review, SUGGESTIONS, 's1')?.id).toBe('s2');
    s = photoReducer(s, { type: 'review/keep', part: 'tower_dish', id: 's3' });
    expect(nextUndecided(s.tower_dish.review, SUGGESTIONS, 's2')?.id).toBe('s1');
    s = photoReducer(s, { type: 'review/keep', part: 'tower_dish', id: 's1' });
    expect(nextUndecided(s.tower_dish.review, SUGGESTIONS, 's2')).toBeNull();
    expect(nextUndecided(s.tower_dish.review, [], null)).toBeNull();
  });
});

describe('find jobs and the next photo to check', () => {
  it('asks again only while a job is in line or running', () => {
    expect(jobState(null)).toEqual({ active: false, line: null, failed: false });
    expect(jobState({ job_id: 'j', status: 'queued' }).active).toBe(true);
    expect(jobState({ job_id: 'j', status: 'running' }).active).toBe(true);
    expect(jobState({ job_id: 'j', status: 'done' }).active).toBe(false);
    expect(jobState({ job_id: 'j', status: 'failed' })).toMatchObject({ active: false, failed: true });
    expect(jobState({ job_id: 'j', status: 'exploded' })).toMatchObject({ active: false, failed: true });
  });

  it('a photo needs a check when suggestions wait or nothing is counted yet', () => {
    expect(photoNeedsCheck(work())).toBe(true);
    expect(photoNeedsCheck(work({ tower_dish: part({ counted_by_you: 4 }) }))).toBe(false);
    expect(photoNeedsCheck(work({ tower_dish: part({ counted_by_you: 4 }), tower_antenna: part({ waiting: 2 }) }))).toBe(true);
  });

  it('finds the next photo that needs a check, going round once and asking no further than needed', async () => {
    const done = work({ tower_dish: part({ counted_by_you: 3 }) });
    const waiting = work({ tower_antenna: part({ waiting: 1 }) });
    const works = [waiting, done, done, waiting, done];
    const asked: number[] = [];
    const workOf = async (i: number) => {
      asked.push(i);
      return works[i];
    };
    expect(await nextPhotoToCheck(works.length, 0, workOf)).toBe(3);
    expect(asked).toEqual([1, 2, 3]);
    expect(await nextPhotoToCheck(works.length, 3, workOf)).toBe(0);
    expect(await nextPhotoToCheck(works.length, -1, workOf)).toBe(0);
    expect(await nextPhotoToCheck(2, 0, async () => done)).toBeNull();
  });
});
