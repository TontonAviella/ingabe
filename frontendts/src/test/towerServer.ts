import type { Box } from '@/lib/photoView';
import type { InspectionPhoto, PartReview, PartWork, PhotoWork, Suggestion, TowerCardAnswer, TowerPart } from '@/lib/towerContract';

// A fake of the tower API for component and logic tests. Its rules are copied from the real code on the backend
// branch (feat/towers-api), never from what the screens expect:
// - src/services/tower_inspection.py: set_marks (look-alikes refused for a part measured without them, as
//   example_finder.check_lookalikes; changed marks drop the part's job and review), lookalike_marks (the other parts'
//   saved marks, for the parts measured with them), start_find (a new job drops the review), part_work and counted
//   (marks + kept + fixed + added, only once a review is saved with nothing waiting, also when the finder found
//   none), set_review (409 before the finder is done; ids must be this job's suggestions) and photos_to_check;
// - src/services/tower_cards.py: the photos_to_check answer (items, their detail words, the empty list's words);
// - src/services/answer_levels_targets.json: each part's noun and whether look-alikes were measured to help.
// The answer level is taken to allow a count after checking (FULL_REVIEW, every tower part's), as it does whenever
// a find ran: the find refuses marks the size gate fails, and changed marks drop the job.

const PARTS: TowerPart[] = ['tower_dish', 'tower_antenna', 'tower_radio_unit'];
const MEASURED: Record<TowerPart, { noun: string; lookalikes: boolean }> = {
  tower_dish: { noun: 'dishes', lookalikes: false },
  tower_antenna: { noun: 'antennas', lookalikes: true },
  tower_radio_unit: { noun: 'radio units', lookalikes: true },
  maize_seedling: { noun: 'maize plants', lookalikes: false },
};
const MAX_LOOKALIKE_MARKS = 20;

/** A photo of the inspection as the server knows it: the contract's fields and its file name. */
export type FakePhoto = InspectionPhoto & { filename: string };

interface PartRecord {
  marks: Box[];
  ownLookalikes: Box[];
  jobId: string | null;
  review: PartReview;
  reviewSaved: boolean;
}

interface FakeJob {
  status: 'queued' | 'running' | 'done' | 'failed';
  finds: { box: Box; score: number }[];
}

type Answer = { status: number; body: unknown };

const emptyReview = (): PartReview => ({ kept: [], rejected: [], fixed: [], added: [] });
const emptyPart = (): PartRecord => ({ marks: [], ownLookalikes: [], jobId: null, review: emptyReview(), reviewSaved: false });
const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
const n = (count: number, one: string) => `${count} ${count === 1 ? one : `${one}s`}`;
const names = (list: string[]) => (list.length === 1 ? list[0] : `${list.slice(0, -1).join(', ')} and ${list[list.length - 1]}`);

/** The answer of tower_cards.answer_card, in its full shape (drone_cards' answer shape). */
function cardAnswer(id: string, title: string, fields: { what: string; why: string; todo: string; items: TowerCardAnswer['items'] }) {
  return {
    id,
    title,
    status: 'ready',
    preview: '',
    question: title,
    status_label: 'Answer ready',
    ...fields,
    how_sure: { level: 'high', label: 'High', bars: 3, because: ['From the work saved on each photo of this inspection'], surer: null },
    facts: [],
    terms: [],
    downloads: [],
    progress: null,
    overlay: null,
    choices: null,
    upload: null,
    field_check: null,
    ask_sage: [],
  } as TowerCardAnswer;
}

export function towerServer(layerId: string, photos: FakePhoto[]) {
  const records = new Map<string, Record<TowerPart, PartRecord>>();
  const jobs = new Map<string, FakeJob>();
  const jobOfInputs = new Map<string, string>();
  const base = `/api/towers/layer/${layerId}`;

  const recordOf = (sha: string) =>
    records.get(sha) ?? (Object.fromEntries(PARTS.map((p) => [p, emptyPart()])) as Record<TowerPart, PartRecord>);

  function lookalikeMarks(part: TowerPart, record: Record<TowerPart, PartRecord>): Box[] {
    if (!MEASURED[part].lookalikes) return [];
    const unique: Box[] = [];
    for (const box of [...PARTS.filter((p) => p !== part).flatMap((p) => record[p].marks), ...record[part].ownLookalikes]) {
      if (!unique.some((b) => same(b, box))) unique.push(box);
    }
    return unique.slice(0, MAX_LOOKALIKE_MARKS);
  }

  function partWork(sha: string, part: TowerPart): PartWork & { reviewSaved: boolean } {
    const record = recordOf(sha);
    const own = record[part];
    const job = own.jobId === null ? null : (jobs.get(own.jobId) ?? null);
    const done = job?.status === 'done';
    const suggestions: Suggestion[] = done
      ? job.finds.map((f, i) => ({ id: `${(own.jobId as string).slice(0, 12)}-${i + 1}`, box: f.box, score: f.score }))
      : [];
    const reviewed = new Set([...own.review.kept, ...own.review.rejected, ...own.review.fixed.map((f) => f.id)]);
    const waiting = suggestions.filter((s) => !reviewed.has(s.id)).length;
    const counted =
      done && waiting === 0 && own.reviewSaved && own.marks.length > 0
        ? own.marks.length + own.review.kept.length + own.review.fixed.length + own.review.added.length
        : null;
    return {
      marks: own.marks,
      lookalike_marks: lookalikeMarks(part, record),
      job: job === null ? null : { job_id: own.jobId as string, status: job.status },
      suggestions,
      reviewed: own.review,
      counted_by_you: counted,
      waiting,
      reviewSaved: own.reviewSaved,
    };
  }

  function contractWork(sha: string, part: TowerPart): PartWork {
    const { reviewSaved: _, ...work } = partWork(sha, part);
    return work;
  }

  function work(sha: string): PhotoWork {
    return { parts: Object.fromEntries(PARTS.map((p) => [p, contractWork(sha, p)])) as PhotoWork['parts'] };
  }

  function setMarks(sha: string, body: { part: TowerPart; marks: Box[]; lookalike_marks?: Box[] }): Answer {
    const looks = body.lookalike_marks ?? [];
    if (looks.length > 0 && !MEASURED[body.part].lookalikes) {
      return {
        status: 422,
        body: { detail: `look-alike marks are not used for ${MEASURED[body.part].noun}: they were measured not to help, or not tried` },
      };
    }
    const record = recordOf(sha);
    const current = record[body.part];
    if (!same([current.marks, current.ownLookalikes], [body.marks, looks])) {
      records.set(sha, { ...record, [body.part]: { ...emptyPart(), marks: body.marks, ownLookalikes: looks } });
    }
    return { status: 200, body: contractWork(sha, body.part) };
  }

  function startFind(sha: string, body: { part: TowerPart }): Answer {
    const record = recordOf(sha);
    const own = record[body.part];
    if (own.marks.length === 0) return { status: 422, body: { detail: 'give 1 to 20 marks' } };
    const inputs = JSON.stringify([sha, body.part, own.marks, lookalikeMarks(body.part, record)]);
    let jobId = jobOfInputs.get(inputs);
    if (jobId === undefined) {
      jobId = `${String(jobs.size + 1).padStart(12, '0')}${'f'.repeat(52)}`;
      jobs.set(jobId, { status: 'queued', finds: [] });
      jobOfInputs.set(inputs, jobId);
    }
    if (own.jobId !== jobId)
      records.set(sha, { ...record, [body.part]: { ...emptyPart(), marks: own.marks, ownLookalikes: own.ownLookalikes, jobId } });
    return { status: 200, body: { job_id: jobId, status: (jobs.get(jobId) as FakeJob).status } };
  }

  function setReview(sha: string, body: { part: TowerPart } & PartReview): Answer {
    const given = [...body.kept, ...body.rejected, ...body.fixed.map((f) => f.id)];
    const twice = given.find((id, i) => given.indexOf(id) !== i);
    if (twice)
      return { status: 422, body: { detail: `each suggestion is kept, rejected or fixed once; ${twice} is given more than once` } };
    const current = partWork(sha, body.part);
    if (current.job?.status !== 'done') {
      return {
        status: 409,
        body: { detail: `the finder has not found ${MEASURED[body.part].noun} on this photo yet; review its suggestions once it has` },
      };
    }
    const unknown = given.find((id) => !current.suggestions.some((s) => s.id === id));
    if (unknown) {
      return {
        status: 422,
        body: {
          detail: `${unknown} is not one of this photo's suggestions for ${MEASURED[body.part].noun} (the suggestions change when the marks do)`,
        },
      };
    }
    const record = recordOf(sha);
    const review = { kept: body.kept, rejected: body.rejected, fixed: body.fixed, added: body.added };
    records.set(sha, { ...record, [body.part]: { ...record[body.part], review, reviewSaved: true } });
    return { status: 200, body: contractWork(sha, body.part) };
  }

  /** tower_inspection.photos_to_check and tower_cards' items for it. */
  function photosToCheck(): TowerCardAnswer {
    const flight = [...photos].sort((a, b) => {
      const key = (p: FakePhoto) => [p.taken_at === null ? 1 : 0, (p.taken_at ?? '').slice(0, 19), p.filename].join('\u0000');
      return key(a) < key(b) ? -1 : key(a) > key(b) ? 1 : 0;
    });
    const order = ['waiting', 'confirm', 'not_found', 'finding', 'unmarked'];
    const listed: { photo: FakePhoto; reason: string; waiting: number; parts: TowerPart[] }[] = [];
    for (const photo of flight) {
      const works = PARTS.map((p) => [p, partWork(photo.sha256, p)] as const);
      if (!records.has(photo.sha256) || !works.some(([, w]) => w.marks.length > 0)) {
        listed.push({ photo, reason: 'unmarked', waiting: 0, parts: [] });
        continue;
      }
      const waiting = works.reduce((sum, [, w]) => sum + w.waiting, 0);
      const tests: [string, (w: PartWork & { reviewSaved: boolean }) => boolean][] = [
        ['waiting', (w) => w.waiting > 0],
        ['confirm', (w) => w.job?.status === 'done' && !w.reviewSaved],
        ['not_found', (w) => w.marks.length > 0 && (w.job === null || w.job.status === 'failed')],
        ['finding', (w) => w.job?.status === 'queued' || w.job?.status === 'running'],
      ];
      for (const [reason, test] of tests) {
        const hit = works.filter(([, w]) => test(w)).map(([p]) => p);
        if (hit.length > 0) {
          listed.push({ photo, reason, waiting, parts: hit });
          break;
        }
      }
    }
    listed.sort((a, b) => order.indexOf(a.reason) - order.indexOf(b.reason) || b.waiting - a.waiting);
    const words = (c: (typeof listed)[number]) => {
      const parts = names(c.parts.map((p) => MEASURED[p].noun));
      return {
        waiting: `${n(c.waiting, 'suggestion')} waiting for you`,
        confirm: `the finder found no more ${parts}: add any it missed, then save`,
        not_found: `${parts} marked; press Find`,
        finding: `the finder is working on ${parts}`,
        unmarked: 'nothing marked yet',
      }[c.reason] as string;
    };
    const of = (reason: string) => listed.filter((c) => c.reason === reason);
    const waitingAll = of('waiting').reduce((sum, c) => sum + c.waiting, 0);
    const summary = [
      of('waiting').length ? `${n(of('waiting').length, 'photo')} with ${n(waitingAll, 'suggestion')} waiting` : '',
      of('confirm').length ? `${n(of('confirm').length, 'photo')} where the finder found no more, to confirm` : '',
      of('not_found').length ? `${n(of('not_found').length, 'photo')} marked but not found yet` : '',
      of('finding').length ? `${n(of('finding').length, 'photo')} the finder is working on` : '',
      of('unmarked').length ? `${n(of('unmarked').length, 'photo')} with nothing marked` : '',
    ].filter(Boolean);
    const what =
      listed.length === 0
        ? 'Every photo is checked: each photo with marks has its count.'
        : `${n(listed.length, 'photo')} of ${photos.length} still need checking: ${names(summary)}. Listed in the order to work on them: nearly done first, then in the order they were taken.`;
    return cardAnswer('photos_to_check', 'Which photos still need checking?', {
      what,
      why: "A photo's count exists only once every suggestion on it is checked; until then it adds nothing to the equipment answer.",
      todo: listed.length === 0 ? 'Nothing to check.' : `Start with ${listed[0].photo.filename}: ${words(listed[0])}.`,
      items: listed.slice(0, 20).map((c) => ({
        id: c.photo.sha256,
        title: c.photo.filename,
        detail: words(c),
        picture: `/api/layer/${layerId}/photos/${c.photo.sha256}`,
        lon: c.photo.lon,
        lat: c.photo.lat,
      })),
    });
  }

  /** The finder's worker ending the job of a part on a photo: its finds, or a failure. */
  function finish(sha: string, part: TowerPart, finds: { box: Box; score: number }[] | 'failed') {
    const jobId = recordOf(sha)[part].jobId;
    if (jobId === null) throw new Error(`no find was started for ${part} on ${sha}`);
    jobs.set(jobId, finds === 'failed' ? { status: 'failed', finds: [] } : { status: 'done', finds });
  }

  /** The routes, keyed "METHOD url", for the tests' fetch mock. */
  function routes(): Record<string, (body: unknown) => Answer> {
    const out: Record<string, (body: unknown) => Answer> = {
      [`GET ${base}/photos`]: () => ({
        status: 200,
        body: { photos: photos.map(({ filename: _, ...photo }) => photo) },
      }),
    };
    // The technician's answer (the screens ask for that one; the owner's words differ, its items do not).
    out[`GET ${base}/cards/photos_to_check?audience=technician`] = () => ({ status: 200, body: photosToCheck() });
    for (const { sha256 } of photos) {
      const photo = `${base}/photos/${sha256}`;
      out[`GET ${photo}/work`] = () => ({ status: 200, body: work(sha256) });
      out[`PUT ${photo}/marks`] = (body) => setMarks(sha256, body as Parameters<typeof setMarks>[1]);
      out[`POST ${photo}/find`] = (body) => startFind(sha256, body as { part: TowerPart });
      out[`PUT ${photo}/review`] = (body) => setReview(sha256, body as Parameters<typeof setReview>[1]);
    }
    return out;
  }

  return { work, setMarks, startFind, setReview, photosToCheck, finish, routes };
}
