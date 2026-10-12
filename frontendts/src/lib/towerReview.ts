import type { Box } from '@/lib/photoView';
import {
  ALL_PARTS,
  type FinderJob,
  type InspectionPhoto,
  type MarksPayload,
  type PartWork,
  type PhotoWork,
  type ReviewPayload,
  type Suggestion,
  type TowerCardAnswer,
  type TowerPart,
} from '@/lib/towerContract';

// What the person has drawn and decided on one photo before it is saved, and the requests built from it. The
// server owns every count: this file never adds marks, kept and added boxes into a number. While any suggestion
// waits for the person's check, the only number shown is how many wait.

/** "Find the rest" asks for at least this many marks: the finder was measured from 3 marks on the same photo
 * (answer_levels_targets.json, protocol). The server's own limits still apply and its refusal is shown as it is. */
export const MARKS_TO_FIND = 3;

export type Decision = { kind: 'kept' } | { kind: 'rejected' } | { kind: 'fixed'; box: Box };

export interface ReviewDraft {
  /** The suggestions this draft is about (their ids, in order): a new find replaces them. */
  about: string;
  decisions: Record<string, Decision>;
  added: Box[];
  /** Changed since the server's last answer. */
  dirty: boolean;
}

export interface PartDraft {
  marks: Box[];
  marksDirty: boolean;
  review: ReviewDraft;
}

export type PhotoDraft = Record<TowerPart, PartDraft>;

export type PhotoAction =
  | { type: 'sync'; work: PhotoWork }
  | { type: 'saved'; part: TowerPart; work: PartWork }
  | { type: 'mark/add'; part: TowerPart; box: Box }
  | { type: 'mark/delete'; part: TowerPart; index: number }
  | { type: 'mark/fix'; part: TowerPart; index: number; box: Box }
  | { type: 'review/keep'; part: TowerPart; id: string }
  | { type: 'review/reject'; part: TowerPart; id: string }
  | { type: 'review/fix'; part: TowerPart; id: string; box: Box }
  | { type: 'review/undo'; part: TowerPart; id: string }
  | { type: 'review/add'; part: TowerPart; box: Box }
  | { type: 'review/fix-added'; part: TowerPart; index: number; box: Box }
  | { type: 'review/delete-added'; part: TowerPart; index: number };

function suggestionsKey(suggestions: Suggestion[]): string {
  return suggestions.map((s) => s.id).join(' ');
}

/** The person's decisions as the server last saved them. */
export function draftFromServer(work: PartWork): ReviewDraft {
  const decisions: Record<string, Decision> = {};
  for (const id of work.reviewed.kept) decisions[id] = { kind: 'kept' };
  for (const id of work.reviewed.rejected) decisions[id] = { kind: 'rejected' };
  for (const { id, box } of work.reviewed.fixed) decisions[id] = { kind: 'fixed', box };
  return { about: suggestionsKey(work.suggestions), decisions, added: [...work.reviewed.added], dirty: false };
}

function partFromServer(work: PartWork): PartDraft {
  return { marks: [...work.marks], marksDirty: false, review: draftFromServer(work) };
}

/** An empty draft for a photo whose work has not arrived yet. */
export function emptyPhotoDraft(): PhotoDraft {
  const empty: PartDraft = { marks: [], marksDirty: false, review: { about: '', decisions: {}, added: [], dirty: false } };
  return Object.fromEntries(ALL_PARTS.map(({ id }) => [id, empty])) as PhotoDraft;
}

/** Takes the server's work where the person has nothing unsaved; a new set of suggestions always replaces the old
 * decisions, which were about other boxes. */
export function syncPart(draft: PartDraft | undefined, work: PartWork): PartDraft {
  if (!draft) return partFromServer(work);
  const marks = draft.marksDirty ? draft.marks : [...work.marks];
  const sameSuggestions = draft.review.about === suggestionsKey(work.suggestions);
  const review = draft.review.dirty && sameSuggestions ? draft.review : draftFromServer(work);
  return { marks, marksDirty: draft.marksDirty, review };
}

function withPart(state: PhotoDraft, part: TowerPart, change: (draft: PartDraft) => PartDraft): PhotoDraft {
  return { ...state, [part]: change(state[part]) };
}

function decide(draft: PartDraft, id: string, decision: Decision | null): PartDraft {
  const decisions = { ...draft.review.decisions };
  if (decision) decisions[id] = decision;
  else delete decisions[id];
  return { ...draft, review: { ...draft.review, decisions, dirty: true } };
}

function editMarks(draft: PartDraft, marks: Box[]): PartDraft {
  return { ...draft, marks, marksDirty: true };
}

function editAdded(draft: PartDraft, added: Box[]): PartDraft {
  return { ...draft, review: { ...draft.review, added, dirty: true } };
}

export function photoReducer(state: PhotoDraft, action: PhotoAction): PhotoDraft {
  switch (action.type) {
    case 'sync':
      return Object.fromEntries(
        ALL_PARTS.map(({ id }) => {
          const work = action.work.parts[id];
          return [id, work ? syncPart(state[id], work) : state[id]];
        }),
      ) as PhotoDraft;
    case 'saved':
      return withPart(state, action.part, () => partFromServer(action.work));
    case 'mark/add':
      return withPart(state, action.part, (d) => editMarks(d, [...d.marks, action.box]));
    case 'mark/delete':
      return withPart(state, action.part, (d) =>
        editMarks(
          d,
          d.marks.filter((_, i) => i !== action.index),
        ),
      );
    case 'mark/fix':
      return withPart(state, action.part, (d) =>
        editMarks(
          d,
          d.marks.map((box, i) => (i === action.index ? action.box : box)),
        ),
      );
    case 'review/keep':
      // Keeping a box the person already corrected keeps their correction.
      return withPart(state, action.part, (d) =>
        d.review.decisions[action.id]?.kind === 'fixed' ? d : decide(d, action.id, { kind: 'kept' }),
      );
    case 'review/reject':
      return withPart(state, action.part, (d) => decide(d, action.id, { kind: 'rejected' }));
    case 'review/fix':
      return withPart(state, action.part, (d) => decide(d, action.id, { kind: 'fixed', box: action.box }));
    case 'review/undo':
      return withPart(state, action.part, (d) => decide(d, action.id, null));
    case 'review/add':
      return withPart(state, action.part, (d) => editAdded(d, [...d.review.added, action.box]));
    case 'review/fix-added':
      return withPart(state, action.part, (d) =>
        editAdded(
          d,
          d.review.added.map((box, i) => (i === action.index ? action.box : box)),
        ),
      );
    case 'review/delete-added':
      return withPart(state, action.part, (d) =>
        editAdded(
          d,
          d.review.added.filter((_, i) => i !== action.index),
        ),
      );
  }
}

/** PUT .../marks for one part: its own marks only. Which look-alikes the finder gets is the server's rule: the other
 * parts' saved marks, for the parts measured with them (MarksPayload); sending them here would be refused for a part
 * measured without them. */
export function marksPayload(part: TowerPart, draft: PhotoDraft): MarksPayload {
  return { part, marks: draft[part].marks, lookalike_marks: [] };
}

/** The other parts whose marks changed since they were saved: they are saved before a find of `part`, so the server
 * has them when it picks that find's look-alikes. */
export function unsavedOtherParts(part: TowerPart, draft: PhotoDraft): TowerPart[] {
  return ALL_PARTS.map(({ id }) => id).filter((id) => id !== part && draft[id].marksDirty);
}

/** PUT .../review for one part: each decision about the suggestions on screen, in their order, and the boxes added. */
export function reviewPayload(part: TowerPart, review: ReviewDraft, suggestions: Suggestion[]): ReviewPayload {
  const out: ReviewPayload = { part, kept: [], rejected: [], fixed: [], added: [...review.added] };
  for (const { id } of suggestions) {
    const decision = review.decisions[id];
    if (decision?.kind === 'kept') out.kept.push(id);
    else if (decision?.kind === 'rejected') out.rejected.push(id);
    else if (decision?.kind === 'fixed') out.fixed.push({ id, box: decision.box });
  }
  return out;
}

/** The suggestions the person has not decided on yet, in order. */
export function undecided(review: ReviewDraft, suggestions: Suggestion[]): Suggestion[] {
  return suggestions.filter(({ id }) => !review.decisions[id]);
}

/** The next suggestion still to check after `afterId` in the suggestions' order (going round), never `afterId`. */
export function nextUndecided(review: ReviewDraft, suggestions: Suggestion[], afterId: string | null): Suggestion | null {
  const start = afterId === null ? -1 : suggestions.findIndex((s) => s.id === afterId);
  for (let step = 1; step <= suggestions.length; step++) {
    const s = suggestions[(start + step + suggestions.length) % suggestions.length];
    if (s.id !== afterId && !review.decisions[s.id]) return s;
  }
  return null;
}

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** The one line about a part's count. Only the server's counted_by_you is ever shown as a count, and never while a
 * suggestion waits for the person's check (on the server or in their unsaved draft): then it says how many wait. A
 * finder that found no more is not a count either: the server counts once the person saved their review of it. */
export function countLine(work: PartWork, review: ReviewDraft | null): string | null {
  if (review?.dirty) {
    const left = undecided(review, work.suggestions).length;
    return left > 0 ? `${plural(left, 'suggestion', 'suggestions')} waiting for your check` : 'All checked: save your review to count them';
  }
  if (work.waiting > 0) return `${plural(work.waiting, 'suggestion', 'suggestions')} waiting for your check`;
  if (work.counted_by_you !== null) return `${work.counted_by_you} counted by you`;
  if (jobState(work.job).done && work.suggestions.length === 0) return 'The finder found no more: save your review to count them';
  return null;
}

/** Whether the person's review can be saved: they changed it, or the finder has finished, the server has no count
 * yet and nothing in the draft waits. The server counts a part only once a review of its finder's suggestions is
 * saved, also when it found none (that review is where the missed ones are added), so that empty review is allowed. */
export function maySaveReview(work: PartWork, review: ReviewDraft): boolean {
  if (review.dirty) return true;
  return jobState(work.job).done && work.counted_by_you === null && undecided(review, work.suggestions).length === 0;
}

/** Where a find job is, in the words the screen shows; `active` jobs are asked about again until they end, and a
 * `done` one is reviewed. */
export function jobState(job: FinderJob | null): { active: boolean; done: boolean; line: string | null; failed: boolean } {
  switch (job?.status) {
    case undefined:
      return { active: false, done: false, line: null, failed: false };
    case 'done':
      return { active: false, done: true, line: null, failed: false };
    case 'queued':
      return { active: true, done: false, line: 'Waiting in line for the finder…', failed: false };
    case 'running':
      return { active: true, done: false, line: 'Finding the rest…', failed: false };
    case 'failed':
      return { active: false, done: false, line: 'The finder could not finish this photo. Try again.', failed: true };
    default:
      return { active: false, done: false, line: `The finder answered "${job?.status}".`, failed: true };
  }
}

/** True while any part of the photo has a find job in line or running. */
export function anyJobActive(work: PhotoWork | undefined): boolean {
  return !!work && ALL_PARTS.some(({ id }) => jobState(work.parts[id]?.job ?? null).active);
}

/** Which photo to open from the server's list of photos still to check (the PHOTOS_TO_CHECK_CARD answer: its items
 * are photos, ids their sha256, in the order to work on them). Goes down that list from `current` (going round
 * once), so stepping on from a photo the person left unfinished does not send them straight back to it. When no
 * other photo is listed, `note` says why in the server's words. This screen never decides itself which photo needs a
 * check. */
export function photoToOpen(
  list: TowerCardAnswer,
  photos: InspectionPhoto[],
  current: string | null,
): { index: number; note: null } | { index: null; note: string } {
  if (list.items.length === 0) return { index: null, note: list.what };
  const indexOf = new Map(photos.map((p, i) => [p.sha256, i]));
  const listed = list.items.filter((item) => indexOf.has(item.id));
  if (listed.length === 0) throw new Error('The photos to check are not among the photos of this inspection; reload the page.');
  const at = listed.findIndex((item) => item.id === current);
  const order = at < 0 ? listed : [...listed.slice(at + 1), ...listed.slice(0, at)];
  if (order.length > 0) return { index: indexOf.get(order[0].id) as number, note: null };
  return { index: null, note: `This is the only photo left to check: ${listed[at].detail}.` };
}
