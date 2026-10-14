import * as DialogPrimitive from '@radix-ui/react-dialog';
import { useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, Check, ChevronLeft, ChevronRight, LoaderCircle, ScanSearch, Trash2, Undo2, X } from 'lucide-react';
import { useCallback, useEffect, useReducer, useRef, useState } from 'react';
import { PANEL_FONT } from '@/components/CardAnswer';
import { BOX_STYLE, type BoxKind, type CanvasBox, type CanvasTool, PhotoCanvas } from '@/components/towers/PhotoCanvas';
import {
  photosToCheckNow,
  useFindTheRest,
  usePhotoQuestions,
  usePhotoWork,
  useSaveMarks,
  useSaveReview,
  useTowerCardAnswer,
  useTrace,
} from '@/hooks/useTowers';
import { type DeckItem, deckOf, litParts, PART_COLOR, scoreWords, suggestedLine, unanswered } from '@/lib/photoCheck';
import type { Box, Point } from '@/lib/photoView';
import {
  type InspectionPhoto,
  type PartLabel,
  type PhotoQuestion,
  TOWER_AUDIENCES,
  TOWER_PARTS,
  type TowerAudience,
  type TowerPart,
  type Traced,
} from '@/lib/towerContract';
import {
  countLine,
  emptyPhotoDraft,
  jobState,
  MARKS_TO_FIND,
  marksPayload,
  maySaveReview,
  nextUndecided,
  type PhotoAction,
  photoReducer,
  photoToOpen,
  reviewPayload,
  undecided,
  unsavedOtherParts,
} from '@/lib/towerReview';

// The photo screen of an inspection. It opens on the inspection's questions, each saying what this photo can tell;
// a question lights up its parts on the photo. "What is on the tower" is where the work is: each suggestion of the
// trained models is shown zoomed in and answered Yes or No (kept as it is answered), then parts the models missed
// are tapped and traced, then the photo's count is the server's. Parts the models do not find by themselves (maize
// with the example finder, bolts on a close-up) are marked first, then found and checked. The server owns every
// count and every word about the inspection; this screen never adds anything up.

const BUTTON =
  'inline-flex items-center justify-center gap-1.5 min-h-10 px-3.5 rounded-full text-[14px] font-semibold cursor-pointer disabled:opacity-50 disabled:cursor-default';
const PRIMARY = `${BUTTON} bg-[#D9A066] text-[#140E0B] hover:bg-[#E9B987]`;
const SECONDARY = `${BUTTON} bg-white/[0.07] text-[#F3EDE6] border border-white/10 hover:bg-white/[0.12]`;
const SMALL = 'inline-flex items-center gap-1 min-h-9 px-3 rounded-full text-[13px] font-bold shadow-lg cursor-pointer';
const BIG =
  'inline-flex items-center justify-center gap-2 min-h-12 rounded-[14px] text-[16px] font-bold cursor-pointer disabled:opacity-50';
const LINK = 'bg-transparent border-0 p-0 text-[13px] text-[#B8A99B] underline underline-offset-[3px] hover:text-[#F3EDE6] cursor-pointer';
const EYEBROW = 'text-[11px] font-bold uppercase tracking-[0.12em] text-[#D9A066]';
const ALERT = 'm-0 rounded-[12px] bg-[#E9B987]/10 border border-[#E9B987]/30 px-3 py-2 text-[14px] text-[#E9B987]';

/** How a question's status looks: the pill's colours by the server's status. */
const PILL: Record<string, string> = {
  needs_check: 'bg-[#D9A066] text-[#0B0908]',
  ready: 'bg-[#F3EDE6] text-[#0B0908]',
  partly: 'bg-[#F3EDE6]/80 text-[#0B0908]',
  working: 'border border-[#D9A066]/50 text-[#E9B987]',
  needs_flight: 'border border-[#8FD3FF]/40 bg-[#8FD3FF]/10 text-[#8FD3FF]',
  needs_camera: 'border border-[#FF8FB4]/40 bg-[#FF8FB4]/10 text-[#FF8FB4]',
  to_build: 'border border-white/15 text-[#B8A99B]',
  cant_tell: 'border border-white/15 text-[#B8A99B]',
};

function takenLine(photo: InspectionPhoto): string | null {
  if (!photo.taken_at) return null;
  const when = new Date(photo.taken_at);
  return `Taken ${Number.isNaN(when.getTime()) ? photo.taken_at : when.toLocaleString()}`;
}

function Pill({ status, label }: { status: string; label: string }) {
  if (!label) return null;
  return (
    <span className={`shrink-0 whitespace-nowrap rounded-full px-2 py-[3px] text-[11px] font-bold ${PILL[status] ?? PILL.cant_tell}`}>
      {label}
    </span>
  );
}

function Dot({ part }: { part: TowerPart }) {
  return <span className="inline-block size-2.5 shrink-0 rounded-[3px]" style={{ background: PART_COLOR[part] }} />;
}

/** The suggestion zoomed in, with its outline (or box), drawn from the photo itself. */
function SuggestionCrop({ url, item, useBox }: { url: string; item: DeckItem; useBox: boolean }) {
  const ref = useRef<HTMLCanvasElement>(null);
  const { suggestion, part } = item;
  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    const W = 640;
    const H = 480;
    canvas.width = W;
    canvas.height = H;
    const [x0, y0, x1, y1] = suggestion.box;
    let cw = (x1 - x0) * 1.9;
    let ch = (y1 - y0) * 1.9;
    if (cw / ch > W / H) ch = (cw * H) / W;
    else cw = (ch * W) / H;
    const cx = (x0 + x1) / 2 - cw / 2;
    const cy = (y0 + y1) / 2 - ch / 2;
    const k = W / cw;
    const img = new Image();
    img.onload = () => {
      ctx.fillStyle = '#000';
      ctx.fillRect(0, 0, W, H);
      ctx.drawImage(img, cx, cy, cw, ch, 0, 0, W, H);
      ctx.lineJoin = 'round';
      ctx.lineWidth = 4;
      ctx.strokeStyle = PART_COLOR[part];
      ctx.fillStyle = `${PART_COLOR[part]}22`;
      const shapes =
        !useBox && suggestion.outline?.length
          ? suggestion.outline
          : [
              [
                [x0, y0],
                [x1, y0],
                [x1, y1],
                [x0, y1],
              ],
            ];
      for (const poly of shapes) {
        ctx.beginPath();
        poly.forEach(([x, y], i) => (i ? ctx.lineTo((x - cx) * k, (y - cy) * k) : ctx.moveTo((x - cx) * k, (y - cy) * k)));
        ctx.closePath();
        ctx.fill();
        ctx.stroke();
      }
    };
    img.src = url;
  }, [url, suggestion, part, useBox]);
  return (
    <canvas
      ref={ref}
      aria-label={`The suggestion, zoomed in`}
      className="block aspect-[4/3] w-full rounded-[14px] border border-white/10 bg-black"
    />
  );
}

function ReaderSwitch({ audience, onChange }: { audience: TowerAudience; onChange: (a: TowerAudience) => void }) {
  return (
    <div className="inline-grid grid-cols-2 gap-[2px] rounded-[10px] bg-[#221813] p-[3px]" role="group" aria-label="Reading as">
      {TOWER_AUDIENCES.map(({ id, label }) => (
        <button
          key={id}
          type="button"
          aria-pressed={audience === id}
          onClick={() => onChange(id)}
          className={`rounded-[8px] px-2.5 py-1 text-[12.5px] cursor-pointer ${
            audience === id ? 'bg-[#3A2A21] font-bold text-[#F3EDE6]' : 'font-semibold text-[#B8A99B] hover:text-[#F3EDE6]'
          }`}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

type View = { kind: 'questions' } | { kind: 'question'; id: string } | { kind: 'check' } | { kind: 'part'; part: TowerPart };
type Step = 'check' | 'missed' | 'done';

function QuestionDetail({
  layerId,
  question,
  audience,
  markParts,
  onMark,
}: {
  layerId: string;
  question: PhotoQuestion;
  audience: TowerAudience;
  /** Parts a person can mark on this photo to answer it (bolts on a close-up). */
  markParts: PartLabel[];
  onMark: (part: TowerPart) => void;
}) {
  const answer = useTowerCardAnswer(layerId, question.id, audience);
  return (
    <div className="flex flex-col gap-3.5">
      <div>
        <Pill status={question.status} label={question.status_label} />
      </div>
      <h2 className="m-0 text-[21px] font-bold leading-tight text-balance">{question.question}</h2>
      <section className="flex flex-col gap-1">
        <h3 className="m-0 text-[11px] font-bold uppercase tracking-[0.1em] text-[#8C7D70]">On this photo</h3>
        <p className="m-0 text-[14.5px] leading-snug text-[#F3EDE6]">{question.on_photo}</p>
      </section>
      {answer.data && (
        <section className="flex flex-col gap-1">
          <h3 className="m-0 text-[11px] font-bold uppercase tracking-[0.1em] text-[#8C7D70]">Why it matters</h3>
          <p className="m-0 text-[14.5px] leading-snug text-[#F3EDE6]">{answer.data.why}</p>
        </section>
      )}
      {(question.needs || answer.data?.todo) && (
        <section className="flex flex-col gap-1 rounded-[12px] border border-[#8FD3FF]/25 bg-[#8FD3FF]/[0.07] px-3 py-2.5">
          <h3 className="m-0 text-[11px] font-bold uppercase tracking-[0.1em] text-[#8C7D70]">
            {question.needs ? 'What would answer it' : 'What to do'}
          </h3>
          <p className="m-0 text-[14.5px] leading-snug text-[#F3EDE6]">{question.needs ?? answer.data?.todo}</p>
        </section>
      )}
      {answer.data && answer.data.how_sure.because.length > 0 && (
        <section className="flex flex-col gap-1 border-l-2 border-[#4A3326] pl-2.5">
          <h3 className="m-0 text-[11px] font-bold uppercase tracking-[0.1em] text-[#8C7D70]">How sure · {answer.data.how_sure.label}</h3>
          <ul className="m-0 flex list-none flex-col gap-1 p-0 text-[12.5px] leading-snug text-[#B8A99B]">
            {answer.data.how_sure.because.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </section>
      )}
      {markParts.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {markParts.map(({ id, label }) => (
            <button key={id} type="button" className={SECONDARY} onClick={() => onMark(id)}>
              <Dot part={id} /> Mark a {label.toLowerCase()} on this photo
            </button>
          ))}
        </div>
      )}
      {answer.error && answer.error.status !== 404 && <p className={ALERT}>{answer.error.message}</p>}
    </div>
  );
}

function PhotoWorkspace({
  layerId,
  photo,
  parts,
  scanning,
  audience,
  onAudience,
  onDirtyChange,
  onNextPhoto,
  nextBusy,
}: {
  layerId: string;
  photo: InspectionPhoto;
  parts: PartLabel[];
  scanning: boolean;
  audience: TowerAudience;
  onAudience: (a: TowerAudience) => void;
  onDirtyChange: (dirty: boolean) => void;
  onNextPhoto: () => void;
  nextBusy: boolean;
}) {
  const work = usePhotoWork(layerId, photo.sha256, scanning);
  const questions = usePhotoQuestions(layerId, photo.sha256, audience);
  const saveMarks = useSaveMarks(layerId, photo.sha256);
  const find = useFindTheRest(layerId, photo.sha256);
  const saveReview = useSaveReview(layerId, photo.sha256);
  const trace = useTrace(layerId, photo.sha256);
  const [draft, dispatch] = useReducer(photoReducer, undefined, emptyPhotoDraft);
  const scanned = parts.filter((p) => p.scanned);
  const marked = parts.filter((p) => !p.scanned);
  const hasQuestions = !!questions.data;
  const [view, setView] = useState<View>({ kind: 'questions' });
  const [step, setStep] = useState<Step | null>(null);
  const [current, setCurrent] = useState<string | null>(null);
  const [useBox, setUseBox] = useState<Record<string, boolean>>({});
  const [selected, setSelected] = useState<string | null>(null);
  const [focus, setFocus] = useState<{ box: Box; nonce: number } | null>(null);
  const [tool, setTool] = useState<'draw' | 'move'>('draw');
  const [addKind, setAddKind] = useState<TowerPart | null>(scanned[0]?.id ?? null);
  const [traced, setTraced] = useState<(Traced & { part: TowerPart }) | null>(null);
  const [drawInstead, setDrawInstead] = useState(false);
  const [addedOutlines, setAddedOutlines] = useState<Record<string, number[][][]>>({});
  const [history, setHistory] = useState<DeckItem[]>([]);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [findError, setFindError] = useState<string | null>(null);
  const [remarking, setRemarking] = useState<Partial<Record<TowerPart, boolean>>>({});

  useEffect(() => {
    if (work.data) dispatch({ type: 'sync', work: work.data });
  }, [work.data]);

  // Without questions (an industry with no cards) the photo opens on the work itself.
  const opened = useRef(false);
  useEffect(() => {
    if (opened.current || questions.isPending) return;
    opened.current = true;
    if (!questions.data) setView(scanned.length ? { kind: 'check' } : marked[0] ? { kind: 'part', part: marked[0].id } : { kind: 'check' });
  }, [questions.isPending, questions.data, scanned.length, marked]);

  const deck = deckOf(work.data, scanned);
  const open = unanswered(deck, draft);
  const foundParts = scanned.filter(({ id }) => jobState(work.data?.parts[id]?.job ?? null).done);
  const allCounted = foundParts.length > 0 && foundParts.every(({ id }) => work.data?.parts[id]?.counted_by_you !== null);
  // Parts the scan could not look at on this photo (no camera angle, unreadable): a person marks one and finds the rest.
  const notScanned = scanned.filter(({ id }) => {
    const w = work.data?.parts[id];
    return !!w && !w.job && w.marks.length === 0 && !scanning;
  });
  const scanBusy = scanned.some(({ id }) => {
    const w = work.data?.parts[id];
    return (w && jobState(w.job).active) || (scanning && w && !w.job && w.marks.length === 0);
  });
  const shownStep: Step = step ?? (open.length > 0 ? 'check' : allCounted && !scanBusy ? 'done' : 'missed');
  const currentItem =
    (current && deck.find((d) => d.key === current && !draft[d.part].review.decisions[d.suggestion.id])) || open[0] || null;

  // --- Each answer is kept as it is given: one save per part at a time, the newest answers sent next. ---
  const versions = useRef<Partial<Record<TowerPart, number>>>({});
  const inFlight = useRef<Partial<Record<TowerPart, boolean>>>({});
  const [tick, setTick] = useState(0);
  const act = useCallback((action: PhotoAction) => {
    if ('part' in action) versions.current[action.part] = (versions.current[action.part] ?? 0) + 1;
    dispatch(action);
  }, []);
  // biome-ignore lint/correctness/useExhaustiveDependencies: tick asks again once a save has ended
  useEffect(() => {
    for (const { id } of scanned) {
      const partWork = work.data?.parts[id];
      const review = draft[id].review;
      if (!partWork || !review.dirty || inFlight.current[id] || !jobState(partWork.job).done) continue;
      inFlight.current[id] = true;
      const sent = versions.current[id] ?? 0;
      saveReview
        .mutateAsync(reviewPayload(id, review, partWork.suggestions))
        .then((saved) => {
          setSaveError(null);
          if ((versions.current[id] ?? 0) === sent) dispatch({ type: 'saved', part: id, work: saved });
        })
        .catch((error: Error) => setSaveError(error.message))
        .finally(() => {
          inFlight.current[id] = false;
          setTick((t) => t + 1);
        });
    }
  }, [draft, work.data, tick]);

  const markedDirty = marked.some(({ id }) => draft[id].marksDirty || draft[id].review.dirty);
  const scannedDirty = scanned.some(({ id }) => draft[id].review.dirty);
  useEffect(() => onDirtyChange(markedDirty || scannedDirty), [markedDirty, scannedDirty, onDirtyChange]);

  const answer = useCallback(
    (kind: 'keep' | 'reject') => {
      const item = currentItem;
      if (!item) return;
      act({ type: kind === 'keep' ? 'review/keep' : 'review/reject', part: item.part, id: item.suggestion.id });
      setHistory((h) => [...h, item]);
      const next = deck.find(
        (d) => d.key !== item.key && deck.indexOf(d) > deck.indexOf(item) && !draft[d.part].review.decisions[d.suggestion.id],
      );
      const after = next ?? open.find((d) => d.key !== item.key) ?? null;
      setCurrent(after?.key ?? null);
      setSelected(null);
      if (after) setFocus({ box: after.suggestion.box, nonce: Date.now() });
      else setStep(null);
    },
    [currentItem, act, deck, draft, open],
  );
  const undo = useCallback(() => {
    const last = history[history.length - 1];
    if (!last) return;
    setHistory((h) => h.slice(0, -1));
    act({ type: 'review/undo', part: last.part, id: last.suggestion.id });
    setCurrent(last.key);
    setStep('check');
  }, [history, act]);

  // Y keeps, N rejects, U takes back the last answer; Delete removes a box you added.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.metaKey || e.ctrlKey || e.altKey) return;
      const k = e.key.toLowerCase();
      if (view.kind === 'check' && shownStep === 'check' && k === 'y') answer('keep');
      else if (view.kind === 'check' && shownStep === 'check' && k === 'n') answer('reject');
      else if (view.kind === 'check' && k === 'u') undo();
      else return;
      e.preventDefault();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [view.kind, shownStep, answer, undo]);

  const finishPhoto = async () => {
    // A part the models found nothing of, or every suggestion answered, is counted once its review is saved.
    for (const { id } of scanned) {
      const partWork = work.data?.parts[id];
      if (!partWork || draft[id].review.dirty || !maySaveReview(partWork, draft[id].review)) continue;
      try {
        const saved = await saveReview.mutateAsync(reviewPayload(id, draft[id].review, partWork.suggestions));
        dispatch({ type: 'saved', part: id, work: saved });
      } catch (error) {
        setSaveError(error instanceof Error ? error.message : String(error));
        return;
      }
    }
    setStep('done');
  };

  const onTap = (point: Point) => {
    if (!addKind) return;
    setTraced(null);
    trace.mutate({ x: point[0], y: point[1] }, { onSuccess: (t) => setTraced({ ...t, part: addKind }) });
  };
  const keepTraced = () => {
    if (!traced) return;
    const index = draft[traced.part].review.added.length;
    setAddedOutlines((o) => ({ ...o, [`${traced.part}:${index}`]: traced.polygons }));
    act({ type: 'review/add', part: traced.part, box: traced.box });
    setTraced(null);
  };

  // --- What the photo shows ---
  const question = view.kind === 'question' ? questions.data?.questions.find((q) => q.id === view.id) : undefined;
  const lit =
    view.kind === 'check'
      ? scanned.map((p) => p.id)
      : litParts(question?.lights ?? (view.kind === 'questions' ? 'equipment' : null), parts);
  const boxes: CanvasBox[] = [];
  const partView = view.kind === 'part' ? view.part : null;
  const partWork = partView ? (work.data?.parts[partView] ?? null) : null;
  const partDraft = partView ? draft[partView] : null;
  const partJob = jobState(partWork?.job ?? null);
  const partReviewing = !!partWork && partJob.done && !(partView && remarking[partView]);
  if (partView && partDraft) {
    if (!partReviewing) {
      partDraft.marks.forEach((box, i) =>
        boxes.push({ key: `m:${i}`, box, kind: 'mark', label: String(i + 1), selectable: true, editable: !partJob.active }),
      );
    } else if (partWork) {
      partDraft.marks.forEach((box, i) =>
        boxes.push({ key: `m:${i}`, box, kind: 'mark', label: String(i + 1), selectable: false, editable: false }),
      );
      for (const s of partWork.suggestions) {
        const decision = partDraft.review.decisions[s.id];
        boxes.push({
          key: `s:${s.id}`,
          box: decision?.kind === 'fixed' ? decision.box : s.box,
          kind: decision ? decision.kind : 'suggestion',
          selectable: true,
          editable: decision?.kind !== 'rejected',
        });
      }
      partDraft.review.added.forEach((box, i) => boxes.push({ key: `a:${i}`, box, kind: 'added', selectable: true, editable: true }));
    }
  } else {
    for (const id of lit) {
      const w = work.data?.parts[id];
      if (!w) continue;
      for (const s of w.suggestions) {
        const decision = draft[id].review.decisions[s.id];
        if (view.kind !== 'check' && decision?.kind === 'rejected') continue;
        const key = `${id}:${s.id}`;
        const kind: BoxKind = decision ? decision.kind : 'suggestion';
        boxes.push({
          key,
          box: decision?.kind === 'fixed' ? decision.box : s.box,
          kind,
          color: PART_COLOR[id],
          outline: decision?.kind === 'fixed' || useBox[key] ? undefined : s.outline,
          current: view.kind === 'check' && shownStep === 'check' && currentItem?.key === key,
          lit: decision?.kind !== 'rejected',
          selectable: view.kind === 'check',
          editable: view.kind === 'check' && decision?.kind !== 'rejected',
        });
      }
      draft[id].review.added.forEach((box, i) =>
        boxes.push({
          key: `a:${id}:${i}`,
          box,
          kind: 'added',
          color: PART_COLOR[id],
          outline: addedOutlines[`${id}:${i}`],
          lit: true,
          selectable: view.kind === 'check',
          editable: view.kind === 'check',
        }),
      );
    }
    if (traced)
      boxes.push({
        key: 'traced',
        box: traced.box,
        kind: 'suggestion',
        color: '#F3EDE6',
        outline: traced.polygons,
        current: true,
        lit: true,
        selectable: false,
        editable: false,
      });
  }

  const onSelect = (key: string | null) => {
    setSelected(key);
    if (view.kind === 'check' && key && !key.startsWith('a:')) {
      const item = deck.find((d) => d.key === key);
      if (item && !draft[item.part].review.decisions[item.suggestion.id]) {
        setCurrent(item.key);
        setStep('check');
      }
    } else if (view.kind !== 'check' && view.kind !== 'part' && key) {
      setView({ kind: 'check' });
    }
  };
  const onDraw = (box: Box) => {
    if (partView) {
      if (partReviewing) act({ type: 'review/add', part: partView, box });
      else if (!partJob.active) act({ type: 'mark/add', part: partView, box });
    } else if (view.kind === 'check' && addKind && drawInstead) {
      act({ type: 'review/add', part: addKind, box });
      setDrawInstead(false);
    }
  };
  const onChange = (key: string, box: Box) => {
    if (partView) {
      const [kind, rest] = [key.slice(0, 1), key.slice(2)];
      if (kind === 'm') act({ type: 'mark/fix', part: partView, index: Number(rest), box });
      else if (kind === 's') act({ type: 'review/fix', part: partView, id: rest, box });
      else if (kind === 'a') act({ type: 'review/fix-added', part: partView, index: Number(rest), box });
      return;
    }
    if (key.startsWith('a:')) {
      const [, part, index] = key.split(':');
      act({ type: 'review/fix-added', part: part as TowerPart, index: Number(index), box });
      return;
    }
    const [part, id] = [key.slice(0, key.indexOf(':')) as TowerPart, key.slice(key.indexOf(':') + 1)];
    act({ type: 'review/fix', part, id, box });
  };

  const removeSelected = () => {
    if (!selected) return;
    if (partView) {
      const [kind, rest] = [selected.slice(0, 1), selected.slice(2)];
      if (kind === 'm' && !partReviewing) act({ type: 'mark/delete', part: partView, index: Number(rest) });
      else if (kind === 'a') act({ type: 'review/delete-added', part: partView, index: Number(rest) });
    } else if (selected.startsWith('a:')) {
      const [, part, index] = selected.split(':');
      act({ type: 'review/delete-added', part: part as TowerPart, index: Number(index) });
    }
    setSelected(null);
  };

  let controls: React.ReactNode = null;
  const selectedSuggestion = partView && selected?.startsWith('s:') ? selected.slice(2) : null;
  // Keeping or rejecting a suggestion of a marked part goes on to the next one still to check.
  const decidePart = (id: string, kind: 'keep' | 'reject') => {
    if (!partView || !partWork || !partDraft) return;
    act({ type: kind === 'keep' ? 'review/keep' : 'review/reject', part: partView, id });
    const decision = kind === 'keep' ? ({ kind: 'kept' } as const) : ({ kind: 'rejected' } as const);
    const decided = { ...partDraft.review, decisions: { ...partDraft.review.decisions, [id]: decision } };
    const next = nextUndecided(decided, partWork.suggestions, id);
    setSelected(next ? `s:${next.id}` : null);
    if (next) setFocus({ box: next.box, nonce: Date.now() });
  };
  if (partView && selectedSuggestion && partDraft) {
    const decision = partDraft.review.decisions[selectedSuggestion];
    controls = decision ? (
      <button
        type="button"
        onClick={() => act({ type: 'review/undo', part: partView, id: selectedSuggestion })}
        className={`${SMALL} bg-[#F3EDE6] text-[#140E0B]`}
      >
        <Undo2 className="size-4" /> Undo
      </button>
    ) : (
      <>
        <button type="button" onClick={() => decidePart(selectedSuggestion, 'keep')} className={`${SMALL} bg-[#D9A066] text-[#0B0908]`}>
          <Check className="size-4" strokeWidth={3} /> Keep
        </button>
        <button type="button" onClick={() => decidePart(selectedSuggestion, 'reject')} className={`${SMALL} bg-[#FF4D1A] text-white`}>
          <X className="size-4" strokeWidth={3} /> Reject
        </button>
      </>
    );
  } else if (selected && (selected.startsWith('a:') || (partView && selected.startsWith('m:') && !partReviewing))) {
    controls = (
      <button type="button" onClick={removeSelected} className={`${SMALL} bg-[#F3EDE6] text-[#140E0B]`}>
        <Trash2 className="size-4" /> Remove
      </button>
    );
  }

  const canvasTool: CanvasTool = partView
    ? tool
    : view.kind === 'check' && shownStep === 'missed'
      ? drawInstead
        ? 'draw'
        : 'tap'
      : 'move';

  // --- The line on the photo ---
  const suggested = suggestedLine(work.data, scanned);
  let sentence: React.ReactNode = null;
  if (view.kind === 'questions') sentence = questions.data?.summary ?? null;
  else if (view.kind === 'question' && question) sentence = question.on_photo;
  else if (view.kind === 'check' && shownStep !== 'done' && suggested.some)
    sentence = (
      <>
        The model suggests <b>{suggested.some}</b> here
        {suggested.none.length > 0 && <>, and is not sure of any {suggested.none.join(' or ')}</>}.{' '}
        <span className="text-[#B8A99B]">Nothing is counted until you check.</span>
      </>
    );

  const findTheRest = async () => {
    if (!partView) return;
    setFindError(null);
    setSelected(null);
    try {
      for (const other of unsavedOtherParts(partView, draft)) {
        const savedOther = await saveMarks.mutateAsync(marksPayload(other, draft));
        dispatch({ type: 'saved', part: other, work: savedOther });
      }
      const saved = await saveMarks.mutateAsync(marksPayload(partView, draft));
      dispatch({ type: 'saved', part: partView, work: saved });
      await find.mutateAsync({ part: partView });
      setRemarking((r) => ({ ...r, [partView]: false }));
    } catch (error) {
      setFindError(error instanceof Error ? error.message : String(error));
    }
  };
  const savePartReview = () => {
    if (!partView || !partWork || !partDraft) return;
    saveReview.mutate(reviewPayload(partView, partDraft.review, partWork.suggestions), {
      onSuccess: (saved) => dispatch({ type: 'saved', part: partView, work: saved }),
    });
  };

  const back = (
    <button
      type="button"
      className="inline-flex items-center gap-1.5 self-start bg-transparent p-0 text-[13px] text-[#B8A99B] hover:text-[#F3EDE6] cursor-pointer border-0"
      onClick={() => {
        setView({ kind: 'questions' });
        setSelected(null);
        setTraced(null);
      }}
    >
      <ArrowLeft className="size-4" /> All questions
    </button>
  );
  const label = (part: TowerPart) => parts.find((p) => p.id === part) as PartLabel;

  let panel: React.ReactNode;
  if (work.isPending) {
    panel = <p className="m-0 text-[15px] text-[#D8CCBF]">Loading this photo…</p>;
  } else if (view.kind === 'questions' && questions.data) {
    panel = (
      <div className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className={EYEBROW}>This photo · {questions.data.questions.length} questions</span>
          <ReaderSwitch audience={audience} onChange={onAudience} />
        </div>
        <ul className="m-0 flex list-none flex-col gap-2 p-0">
          {questions.data.questions.map((q) => (
            <li key={q.id}>
              <button
                type="button"
                onClick={() =>
                  setView(
                    q.id === 'equipment' || (q.lights === 'defects' && scanned.some((p) => litParts('defects', [p]).length))
                      ? { kind: 'check' }
                      : { kind: 'question', id: q.id },
                  )
                }
                className="grid w-full grid-cols-[minmax(0,1fr)_auto] items-center gap-x-2.5 gap-y-1 rounded-[14px] border border-white/[0.06] bg-[#221813] px-3.5 py-3 text-left hover:border-[#D9A066]/45 cursor-pointer"
              >
                <span className="text-[14.5px] font-semibold leading-snug text-[#F3EDE6]">{q.question}</span>
                <Pill status={q.status} label={q.status_label} />
                <span className="col-span-2 text-[12.5px] leading-snug text-[#B8A99B]">{q.on_photo}</span>
              </button>
            </li>
          ))}
        </ul>
      </div>
    );
  } else if (view.kind === 'question' && question) {
    panel = (
      <div className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          {back}
          <ReaderSwitch audience={audience} onChange={onAudience} />
        </div>
        <QuestionDetail
          layerId={layerId}
          question={question}
          audience={audience}
          markParts={question.lights === 'defects' ? marked.filter((p) => litParts('defects', [p]).length) : []}
          onMark={(part) => setView({ kind: 'part', part })}
        />
      </div>
    );
  } else if (view.kind === 'check') {
    const answered = deck.length - open.length;
    if (deck.length === 0 && foundParts.length === 0 && notScanned.length > 0 && !scanBusy) {
      panel = (
        <div className="flex flex-col gap-3">
          {hasQuestions && back}
          <h2 className="m-0 text-[21px] font-bold leading-tight">This photo was not scanned</h2>
          <p className="m-0 text-[14px] leading-snug text-[#B8A99B]">
            This photo was not scanned for {notScanned.map((p) => p.plural).join(' or ')} (it has no camera angle, or could not be read).
            Draw a box around one you see, then let the model find the rest.
          </p>
          <div className="flex flex-wrap gap-2">
            {notScanned.map(({ id, label: name }) => (
              <button key={id} type="button" className={SECONDARY} onClick={() => setView({ kind: 'part', part: id })}>
                <Dot part={id} /> Mark a {name.toLowerCase()}
              </button>
            ))}
          </div>
        </div>
      );
    } else if (scanBusy && deck.length === 0) {
      panel = (
        <div className="flex flex-col gap-3">
          {hasQuestions && back}
          <p role="status" className="m-0 flex items-center gap-2 text-[14px] leading-snug text-[#D8CCBF]">
            <LoaderCircle className="size-4 shrink-0 animate-spin text-[#D9A066]" />
            The models are looking at this photo. Their suggestions show here as soon as they are done.
          </p>
        </div>
      );
    } else if (shownStep === 'check' && currentItem) {
      const item = currentItem;
      const key = item.key;
      panel = (
        <div className="flex flex-col gap-3">
          <div className="flex items-center justify-between gap-2">
            {hasQuestions ? back : <span />}
            {history.length > 0 && (
              <button type="button" className={LINK} onClick={undo}>
                Undo last
              </button>
            )}
          </div>
          <span className={EYEBROW}>
            Check {answered + 1} of {deck.length}
          </span>
          <SuggestionCrop url={photo.url} item={item} useBox={!!useBox[key]} />
          <h2 className="m-0 text-[21px] font-bold leading-tight">Is this a {label(item.part).label.toLowerCase()}?</h2>
          <span className="inline-flex items-center gap-2 text-[12.5px] text-[#B8A99B]">
            <span className="inline-block h-1.5 w-16 overflow-hidden rounded-full bg-[#221813]">
              <span className="block h-full bg-[#D9A066]" style={{ width: `${Math.round(item.suggestion.score * 100)}%` }} />
            </span>
            {scoreWords(item.suggestion.score)}
          </span>
          <div className="grid grid-cols-2 gap-2">
            <button
              type="button"
              className={`${BIG} border border-white/15 bg-white/[0.06] text-[#F3EDE6] hover:bg-[#FF4D1A]/20`}
              onClick={() => answer('reject')}
            >
              No <kbd className="rounded border border-current px-1 font-mono text-[11px] opacity-60">N</kbd>
            </button>
            <button type="button" className={`${BIG} bg-[#D9A066] text-[#0B0908] hover:bg-[#E9B987]`} onClick={() => answer('keep')}>
              Yes <kbd className="rounded border border-current px-1 font-mono text-[11px] opacity-60">Y</kbd>
            </button>
          </div>
          <div className="flex flex-wrap items-center justify-between gap-2">
            {item.suggestion.outline?.length ? (
              <button type="button" className={LINK} onClick={() => setUseBox((u) => ({ ...u, [key]: !u[key] }))}>
                {useBox[key] ? 'Show the traced outline' : 'Outline wrong? Show the box'}
              </button>
            ) : (
              <span className="text-[12px] text-[#8C7D70]">Tap the box on the photo to fix its corners</span>
            )}
            <span className="text-[12px] text-[#8C7D70]">Each answer is kept as you give it</span>
          </div>
          <div className="flex flex-wrap gap-[5px]" role="group" aria-label="Every suggestion on this photo">
            {deck.map((d, i) => {
              const decision = draft[d.part].review.decisions[d.suggestion.id];
              return (
                <button
                  key={d.key}
                  type="button"
                  aria-label={`${label(d.part).label} ${i + 1}${decision ? (decision.kind === 'rejected' ? ': no' : ': yes') : ''}`}
                  onClick={() => {
                    if (!decision) {
                      setCurrent(d.key);
                      setFocus({ box: d.suggestion.box, nonce: Date.now() });
                    }
                  }}
                  className={`grid size-[18px] place-items-center rounded-[6px] border-2 p-0 text-[10px] font-extrabold cursor-pointer ${
                    d.key === item.key ? 'outline outline-2 outline-offset-2 outline-[#F3EDE6]' : ''
                  } ${decision?.kind === 'rejected' ? 'opacity-35 text-[#B8A99B]' : 'text-[#0B0908]'}`}
                  style={{
                    borderColor: PART_COLOR[d.part],
                    background: decision && decision.kind !== 'rejected' ? PART_COLOR[d.part] : 'transparent',
                  }}
                >
                  {decision ? (decision.kind === 'rejected' ? '✕' : '✓') : ''}
                </button>
              );
            })}
          </div>
          {saveError && (
            <p role="alert" className={ALERT}>
              {saveError}
            </p>
          )}
        </div>
      );
    } else if (shownStep !== 'done') {
      panel = (
        <div className="flex flex-col gap-3">
          {hasQuestions && back}
          <span className={EYEBROW}>Anything missed?</span>
          <h2 className="m-0 text-[21px] font-bold leading-tight">
            {drawInstead ? 'Draw a box around it' : 'Tap a part the model missed'}
          </h2>
          <p className="m-0 text-[14px] leading-snug text-[#B8A99B]">
            {drawInstead
              ? 'Drag a box around the part on the photo.'
              : 'Pick what it is, then tap it on the photo: its outline is traced for you to see before you keep it.'}
          </p>
          <div className="flex flex-wrap gap-1.5" role="group" aria-label="What you are adding">
            {scanned.map(({ id, label: name }) => (
              <button
                key={id}
                type="button"
                aria-pressed={addKind === id}
                onClick={() => {
                  setAddKind(id);
                  setTraced(null);
                }}
                className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-[13px] font-semibold cursor-pointer ${
                  addKind === id ? 'border-[#F3EDE6] bg-[#F3EDE6] text-[#0B0908]' : 'border-white/15 text-[#F3EDE6]'
                }`}
              >
                <Dot part={id} /> {name}
              </button>
            ))}
          </div>
          {trace.isPending && (
            <p role="status" className="m-0 flex items-center gap-2 text-[14px] text-[#D8CCBF]">
              <LoaderCircle className="size-4 animate-spin text-[#D9A066]" /> Tracing…
            </p>
          )}
          {trace.error && (
            <p role="alert" className={ALERT}>
              {trace.error.message}
            </p>
          )}
          {traced && (
            <div className="flex flex-col gap-2 rounded-[12px] border border-white/10 bg-[#221813] p-3">
              <p className="m-0 text-[14px] text-[#F3EDE6]">Is the white outline the {label(traced.part).label.toLowerCase()}?</p>
              <div className="flex flex-wrap gap-2">
                <button type="button" className={PRIMARY} onClick={keepTraced}>
                  <Check className="size-4" /> Keep it
                </button>
                <button
                  type="button"
                  className={SECONDARY}
                  onClick={() => {
                    setTraced(null);
                    setDrawInstead(true);
                  }}
                >
                  Wrong shape: draw a box
                </button>
              </div>
            </div>
          )}
          {!drawInstead && !traced && (
            <button type="button" className={LINK} onClick={() => setDrawInstead(true)}>
              Draw a box instead
            </button>
          )}
          {scanned.some(({ id }) => draft[id].review.added.length > 0) && (
            <p className="m-0 text-[13px] text-[#B8A99B]">
              Added by you:{' '}
              {scanned
                .filter(({ id }) => draft[id].review.added.length > 0)
                .map(({ id, label: name }) => `${draft[id].review.added.length} ${name.toLowerCase()}`)
                .join(', ')}
              . Tap one on the photo to remove it.
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            <button type="button" className={PRIMARY} disabled={saveReview.isPending} onClick={finishPhoto}>
              {saveReview.isPending ? <LoaderCircle className="size-4 animate-spin" /> : <Check className="size-4" />}
              {scanned.some(({ id }) => draft[id].review.added.length > 0) ? 'Done with this photo' : 'Nothing missed: done'}
            </button>
            {deck.length > 0 && (
              <button
                type="button"
                className={SECONDARY}
                onClick={() => {
                  setStep('check');
                  setCurrent(deck[0]?.key ?? null);
                }}
              >
                Back to the checks
              </button>
            )}
          </div>
          {saveError && (
            <p role="alert" className={ALERT}>
              {saveError}
            </p>
          )}
        </div>
      );
    } else {
      panel = (
        <div className="flex flex-col gap-3">
          {hasQuestions && back}
          <span className={EYEBROW}>This photo is checked</span>
          <h2 className="m-0 text-[21px] font-bold leading-tight">Counted by you on this photo</h2>
          <div className="flex flex-col gap-2">
            {scanned.map(({ id, plural }) => {
              const w = work.data?.parts[id];
              return (
                <div key={id} className="flex items-center justify-between rounded-[12px] bg-[#221813] px-3 py-2.5 text-[15px]">
                  <span className="inline-flex items-center gap-2 capitalize">
                    <Dot part={id} /> {plural}
                  </span>
                  <b className="tabular-nums text-[18px]">{w?.counted_by_you ?? '–'}</b>
                </div>
              );
            })}
          </div>
          <p className="m-0 text-[12.5px] leading-snug text-[#8C7D70]">
            Photos overlap, so the tower's count is the most seen on one photo, not a sum. Comparing with the tenant list (what each
            contract allows) is coming.
          </p>
          <div className="flex flex-wrap gap-2">
            <button type="button" className={PRIMARY} disabled={nextBusy} onClick={onNextPhoto}>
              {nextBusy && <LoaderCircle className="size-4 animate-spin" />}
              Go to the next photo
            </button>
            <button
              type="button"
              className={SECONDARY}
              onClick={() => {
                setStep('check');
                setCurrent(deck[0]?.key ?? null);
              }}
            >
              Look again
            </button>
          </div>
        </div>
      );
    }
  } else if (partView && partWork && partDraft) {
    const partLabel = label(partView);
    const marksNeeded = partLabel.scanned ? 1 : MARKS_TO_FIND;
    panel = (
      <div className="flex flex-col gap-3">
        {hasQuestions && back}
        {marked.length > 1 && (
          <div className="flex flex-wrap gap-1.5" role="radiogroup" aria-label="Part to mark">
            {marked.map(({ id, label: name }) => (
              <button
                key={id}
                type="button"
                role="radio"
                aria-checked={partView === id}
                onClick={() => setView({ kind: 'part', part: id })}
                className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-[13px] font-semibold cursor-pointer ${
                  partView === id ? 'border-[#F3EDE6] bg-[#F3EDE6] text-[#0B0908]' : 'border-white/15 text-[#F3EDE6]'
                }`}
              >
                {name}
              </button>
            ))}
          </div>
        )}
        {!partReviewing ? (
          <>
            <h2 className="m-0 text-[21px] font-bold leading-tight">Mark {partLabel.plural}</h2>
            <p className="m-0 text-[14px] leading-snug text-[#B8A99B]">
              Draw a box around at least {marksNeeded} {marksNeeded === 1 ? partLabel.label.toLowerCase() : partLabel.plural}, then let the
              finder suggest the rest.
            </p>
            <span className="text-[13px] tabular-nums text-[#B8A99B]">
              {partDraft.marks.length} marked
              {partDraft.marks.length < marksNeeded && ` · ${marksNeeded - partDraft.marks.length} more to find the rest`}
            </span>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                className={PRIMARY}
                disabled={partDraft.marks.length < marksNeeded || partJob.active || saveMarks.isPending || find.isPending}
                onClick={findTheRest}
              >
                {saveMarks.isPending || find.isPending ? (
                  <LoaderCircle className="size-4 animate-spin" />
                ) : (
                  <ScanSearch className="size-4" />
                )}
                Find the rest
              </button>
              {partJob.done && remarking[partView] && (
                <button type="button" className={SECONDARY} onClick={() => setRemarking((r) => ({ ...r, [partView]: false }))}>
                  Back to the review
                </button>
              )}
            </div>
            {partJob.line && (
              <p
                role={partJob.failed ? 'alert' : 'status'}
                className={`m-0 text-[14px] ${partJob.failed ? 'text-[#E9B987]' : 'text-[#D8CCBF]'}`}
              >
                {partJob.line}
              </p>
            )}
            {findError && (
              <p role="alert" className={ALERT}>
                {findError}
              </p>
            )}
          </>
        ) : (
          <>
            <p role="status" className="m-0 text-[17px] font-bold tabular-nums text-[#F3EDE6]">
              {countLine(partWork, partDraft.review)}
            </p>
            <p className="m-0 text-[14px] leading-snug text-[#B8A99B]">
              {partWork.suggestions.length > 0
                ? `Tap a box to keep or reject it, drag its corners to fix it, and draw a box around any ${partLabel.label.toLowerCase()} the finder missed. Nothing is counted until you save.`
                : `The finder found no more ${partLabel.plural}. Draw a box around any it missed, then save: nothing is counted until you save.`}
            </p>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                className={PRIMARY}
                disabled={!maySaveReview(partWork, partDraft.review) || saveReview.isPending}
                onClick={savePartReview}
              >
                {saveReview.isPending ? <LoaderCircle className="size-4 animate-spin" /> : <Check className="size-4" />}
                Save review
              </button>
              {undecided(partDraft.review, partWork.suggestions).length > 0 && (
                <button
                  type="button"
                  className={SECONDARY}
                  onClick={() => {
                    const next = nextUndecided(partDraft.review, partWork.suggestions, selectedSuggestion);
                    setSelected(next ? `s:${next.id}` : null);
                    if (next) setFocus({ box: next.box, nonce: Date.now() });
                  }}
                >
                  Next to check
                </button>
              )}
              <button
                type="button"
                className={SECONDARY}
                onClick={() => {
                  setRemarking((r) => ({ ...r, [partView]: true }));
                  setSelected(null);
                }}
              >
                Change marks
              </button>
            </div>
            {saveReview.error && (
              <p role="alert" className={ALERT}>
                {saveReview.error.message}
              </p>
            )}
          </>
        )}
      </div>
    );
  } else {
    panel = work.error ? (
      <p role="alert" className={ALERT}>
        {work.error.message}
      </p>
    ) : null;
  }

  return (
    <div className="flex flex-1 min-h-0 flex-col lg:flex-row">
      <div className="relative min-h-[48vh] flex-1">
        <PhotoCanvas
          url={photo.url}
          width={photo.width}
          height={photo.height}
          alt={`Tower photo ${photo.sha256.slice(0, 8)}`}
          boxes={boxes}
          selected={selected}
          tool={canvasTool}
          disabled={!work.data}
          focus={focus}
          onSelect={onSelect}
          onDraw={onDraw}
          onChange={onChange}
          onTap={onTap}
          onToolChange={partView ? setTool : undefined}
          spotlight={!partView && lit.length > 0 && boxes.length > 0}
          selectedControls={controls}
        />
        {sentence && (
          <p
            role="status"
            className="pointer-events-none absolute left-3 top-3 m-0 max-w-[min(64ch,calc(100%-24px))] rounded-[14px] border border-white/10 bg-[#17110E]/80 px-3.5 py-2.5 text-[14px] leading-snug text-[#F3EDE6] backdrop-blur-md"
          >
            {sentence}
          </p>
        )}
      </div>
      <aside className="flex shrink-0 flex-col gap-4 overflow-y-auto border-t border-white/10 p-4 lg:w-[390px] lg:border-t-0 lg:border-l max-lg:max-h-[52vh]">
        {questions.error && questions.error.status !== 404 && <p className={ALERT}>{questions.error.message}</p>}
        {panel}
        {partView && (
          <div className="flex flex-wrap gap-x-3 gap-y-1.5 text-[12px] text-[#D8CCBF]">
            {(partReviewing ? (['mark', 'suggestion', 'kept', 'added', 'rejected'] as BoxKind[]) : (['mark'] as BoxKind[])).map((kind) => (
              <span key={kind} className="inline-flex items-center gap-1.5">
                <span
                  className="inline-block h-3 w-4 rounded-[3px]"
                  style={{ border: `2px ${BOX_STYLE[kind].dash ? 'dashed' : 'solid'} ${BOX_STYLE[kind].stroke}` }}
                />
                {
                  {
                    mark: 'Your marks',
                    suggestion: 'To check',
                    kept: 'Kept',
                    added: 'Added by you',
                    rejected: 'Rejected',
                    fixed: 'Fixed',
                    lookalike: 'Other parts',
                  }[kind]
                }
              </span>
            ))}
          </div>
        )}
      </aside>
    </div>
  );
}

/** The photo screen over the whole window, for one photo at a time of an inspection. */
export function PhotoMarking({
  layerId,
  layerName,
  photos,
  parts = TOWER_PARTS,
  scanning = false,
  startIndex,
  note,
  onClose,
}: {
  layerId: string;
  layerName: string;
  photos: InspectionPhoto[];
  /** The parts this inspection marks (the server's list); the tower parts when it gives none. */
  parts?: PartLabel[];
  /** The trained models are scanning the inspection's photos now. */
  scanning?: boolean;
  startIndex: number;
  /** A line shown when the screen opens (the server's words when no photo is left to check). */
  note?: string | null;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [index, setIndex] = useState(startIndex);
  const [dirty, setDirty] = useState(false);
  const [searching, setSearching] = useState(false);
  const [message, setMessage] = useState<string | null>(note ?? null);
  const [audience, setAudience] = useState<TowerAudience>('owner');
  const photo = photos[index];
  const towerWork = parts.some((p) => TOWER_PARTS.some((t) => t.id === p.id));

  const mayLeave = () => !dirty || window.confirm('Leave this photo? Your unsaved marks or checks on it will be lost.');
  const go = (next: number) => {
    if (next === index || !mayLeave()) return;
    setIndex(next);
    setDirty(false);
    setMessage(null);
  };
  const close = () => {
    if (mayLeave()) onClose();
  };
  const nextToCheck = async () => {
    if (!towerWork) {
      go((index + 1) % photos.length);
      return;
    }
    setSearching(true);
    setMessage(null);
    try {
      const next = photoToOpen(await photosToCheckNow(queryClient, layerId), photos, photo?.sha256 ?? null);
      if (next.index === null) setMessage(next.note);
      else go(next.index);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setSearching(false);
    }
  };
  const taken = photo ? takenLine(photo) : null;

  return (
    <DialogPrimitive.Root open onOpenChange={(open) => !open && close()}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Content
          aria-describedby={undefined}
          className="fixed inset-0 z-[60] flex flex-col bg-[#0B0908] text-[#F3EDE6] outline-none"
          style={{ fontFamily: PANEL_FONT }}
          onPointerDownOutside={(e) => e.preventDefault()}
        >
          <header className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-white/10 px-3 py-2.5 sm:px-4">
            <button
              type="button"
              aria-label="Close the photo screen"
              onClick={close}
              className="flex size-10 items-center justify-center rounded-full text-[#B8A99B] hover:bg-white/10 hover:text-[#F3EDE6] cursor-pointer"
            >
              <X className="size-5" />
            </button>
            <div className="flex min-w-0 flex-1 flex-col">
              <DialogPrimitive.Title className="m-0 truncate text-[17px] font-bold">{layerName}</DialogPrimitive.Title>
              <span className="truncate text-[13px] text-[#B8A99B] tabular-nums">
                Photo {index + 1} of {photos.length}
                {taken && ` · ${taken}`}
              </span>
            </div>
            <div className="flex items-center gap-1.5">
              <button
                type="button"
                aria-label="Previous photo"
                disabled={index === 0}
                onClick={() => go(index - 1)}
                className="flex size-10 items-center justify-center rounded-full bg-white/[0.06] hover:bg-white/[0.12] disabled:opacity-40 cursor-pointer disabled:cursor-default"
              >
                <ChevronLeft className="size-5" />
              </button>
              <button
                type="button"
                aria-label="Next photo"
                disabled={index >= photos.length - 1}
                onClick={() => go(index + 1)}
                className="flex size-10 items-center justify-center rounded-full bg-white/[0.06] hover:bg-white/[0.12] disabled:opacity-40 cursor-pointer disabled:cursor-default"
              >
                <ChevronRight className="size-5" />
              </button>
              <button type="button" className={SECONDARY} disabled={searching} onClick={nextToCheck}>
                {searching && <LoaderCircle className="size-4 animate-spin" />}
                Next photo to check
              </button>
            </div>
            {message && (
              <p role="status" className="m-0 w-full text-[13px] text-[#E9B987]">
                {message}
              </p>
            )}
          </header>
          {photo ? (
            <PhotoWorkspace
              key={photo.sha256}
              layerId={layerId}
              photo={photo}
              parts={parts}
              scanning={scanning}
              audience={audience}
              onAudience={setAudience}
              onDirtyChange={setDirty}
              onNextPhoto={nextToCheck}
              nextBusy={searching}
            />
          ) : (
            <p className="m-0 p-6 text-[15px] text-[#D8CCBF]">This inspection has no photos.</p>
          )}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
