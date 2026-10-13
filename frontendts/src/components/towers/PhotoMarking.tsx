import * as DialogPrimitive from '@radix-ui/react-dialog';
import { useQueryClient } from '@tanstack/react-query';
import { Check, ChevronLeft, ChevronRight, Hand, LoaderCircle, PenLine, ScanSearch, Trash2, Undo2, X } from 'lucide-react';
import { useCallback, useEffect, useReducer, useRef, useState } from 'react';
import { PANEL_FONT } from '@/components/CardAnswer';
import { BOX_STYLE, type BoxKind, type CanvasBox, PhotoCanvas } from '@/components/towers/PhotoCanvas';
import { photosToCheckNow, useFindTheRest, usePhotoWork, useSaveMarks, useSaveReview } from '@/hooks/useTowers';
import type { Box } from '@/lib/photoView';
import { type InspectionPhoto, type PartLabel, TOWER_PARTS, type TowerPart } from '@/lib/towerContract';
import {
  countLine,
  emptyPhotoDraft,
  jobState,
  MARKS_TO_FIND,
  marksPayload,
  maySaveReview,
  nextUndecided,
  photoReducer,
  photoToOpen,
  reviewPayload,
  undecided,
  unsavedOtherParts,
} from '@/lib/towerReview';

// The marking screen for one photo of a tower inspection: mark a few of one part, let the finder suggest the rest,
// then keep, reject, fix or add boxes and save the check. The server owns every count; this screen never shows a
// total of its own, and while suggestions wait it only says how many wait.

const BUTTON =
  'inline-flex items-center justify-center gap-1.5 min-h-10 px-3.5 rounded-full text-[14px] font-semibold cursor-pointer disabled:opacity-50 disabled:cursor-default';
const PRIMARY = `${BUTTON} bg-[#D9A066] text-[#140E0B] hover:bg-[#E9B987]`;
const SECONDARY = `${BUTTON} bg-white/[0.07] text-[#F3EDE6] border border-white/10 hover:bg-white/[0.12]`;
const SMALL = 'inline-flex items-center gap-1 min-h-9 px-3 rounded-full text-[13px] font-bold shadow-lg cursor-pointer';

function takenLine(photo: InspectionPhoto): string | null {
  const parts: string[] = [];
  if (photo.taken_at) {
    const when = new Date(photo.taken_at);
    parts.push(`Taken ${Number.isNaN(when.getTime()) ? photo.taken_at : when.toLocaleString()}`);
  }
  if (photo.relative_altitude_m !== null) parts.push(`${photo.relative_altitude_m.toFixed(1)} m above take-off`);
  return parts.length ? parts.join(' · ') : null;
}

function Swatch({ kind, label }: { kind: BoxKind; label: string }) {
  const style = BOX_STYLE[kind];
  return (
    <span className="inline-flex items-center gap-1.5 text-[12px] text-[#D8CCBF]">
      <span
        className="inline-block h-3 w-4 rounded-[3px]"
        style={{
          border: `2px ${style.dash ? 'dashed' : 'solid'} ${style.stroke}`,
          background: style.fill === 'none' ? 'transparent' : style.fill,
        }}
      />
      {label}
    </span>
  );
}

function PhotoWorkspace({
  layerId,
  photo,
  parts,
  scanning,
  onDirtyChange,
}: {
  layerId: string;
  photo: InspectionPhoto;
  parts: PartLabel[];
  scanning: boolean;
  onDirtyChange: (dirty: boolean) => void;
}) {
  const work = usePhotoWork(layerId, photo.sha256, scanning);
  const saveMarks = useSaveMarks(layerId, photo.sha256);
  const find = useFindTheRest(layerId, photo.sha256);
  const saveReview = useSaveReview(layerId, photo.sha256);
  const [draft, dispatch] = useReducer(photoReducer, undefined, emptyPhotoDraft);
  const [part, setPart] = useState<TowerPart>(parts[0]?.id ?? 'tower_dish');
  const [tool, setTool] = useState<'draw' | 'move'>('draw');
  const [selected, setSelected] = useState<string | null>(null);
  const [focus, setFocus] = useState<{ box: Box; nonce: number } | null>(null);
  // Parts the person went back to marking after a find, until their next find.
  const [remarking, setRemarking] = useState<Partial<Record<TowerPart, boolean>>>({});
  const [findError, setFindError] = useState<string | null>(null);

  useEffect(() => {
    if (work.data) dispatch({ type: 'sync', work: work.data });
  }, [work.data]);

  const dirty = parts.some(({ id }) => draft[id].marksDirty || draft[id].review.dirty);
  useEffect(() => onDirtyChange(dirty), [dirty, onDirtyChange]);

  const partWork = work.data?.parts[part] ?? null;
  const partDraft = draft[part];
  const label = (parts.find((p) => p.id === part) ?? parts[0]) as PartLabel;
  const job = jobState(partWork?.job ?? null);
  // Once a find has ended well the person reviews it, also when it found no more (that review is where they add any
  // it missed, and nothing is counted without it); while one runs or after it failed, the marks show.
  const reviewing = !!partWork && job.done && !remarking[part];
  // A part the trained models find by themselves: nobody draws examples first, the photo opens on what they found.
  const scanned = !!label.scanned;
  const waitingForScan = scanned && !!partWork && !partWork.job && partWork.marks.length === 0;
  // The scan is on its way to this photo, or already looking at it.
  const scanBusy = scanned && ((waitingForScan && scanning) || job.active);
  // A scanned part falls back to marks only where the scan could not look (no camera angle, unreadable photo).
  const marksNeeded = scanned ? 1 : MARKS_TO_FIND;

  // The photo opens on the first part with suggestions waiting, when the one shown has none.
  const opened = useRef(false);
  useEffect(() => {
    if (opened.current || !work.data) return;
    opened.current = true;
    if ((work.data.parts[part]?.waiting ?? 0) > 0) return;
    const first = parts.find(({ id }) => (work.data?.parts[id]?.waiting ?? 0) > 0);
    if (first) setPart(first.id);
  }, [work.data, part, parts]);
  const left = partWork ? undecided(partDraft.review, partWork.suggestions) : [];
  const busy = saveMarks.isPending || find.isPending;

  const pickPart = (id: TowerPart) => {
    setPart(id);
    setSelected(null);
    setFindError(null);
    saveReview.reset();
  };

  const findTheRest = async () => {
    setFindError(null);
    setSelected(null);
    try {
      // The other parts' changed marks first: the server gives them to this find as look-alikes where it was
      // measured with them.
      for (const other of unsavedOtherParts(part, draft)) {
        const savedOther = await saveMarks.mutateAsync(marksPayload(other, draft));
        dispatch({ type: 'saved', part: other, work: savedOther });
      }
      const saved = await saveMarks.mutateAsync(marksPayload(part, draft));
      dispatch({ type: 'saved', part, work: saved });
      await find.mutateAsync({ part });
      setRemarking((r) => ({ ...r, [part]: false }));
    } catch (error) {
      setFindError(error instanceof Error ? error.message : String(error));
    }
  };

  const saveCheck = () => {
    if (!partWork) return;
    saveReview.mutate(reviewPayload(part, partDraft.review, partWork.suggestions), {
      onSuccess: (saved) => dispatch({ type: 'saved', part, work: saved }),
    });
  };

  const goToNext = useCallback(
    (afterId: string | null) => {
      if (!partWork) return;
      const next = nextUndecided(partDraft.review, partWork.suggestions, afterId);
      setSelected(next ? `s:${next.id}` : null);
      if (next) setFocus({ box: next.box, nonce: Date.now() });
    },
    [partWork, partDraft.review],
  );

  const decide = useCallback(
    (id: string, kind: 'keep' | 'reject' | 'undo') => {
      dispatch({ type: `review/${kind}`, part, id });
      if (kind === 'undo') return;
      goToNext(id);
    },
    [part, goToNext],
  );

  const removeSelected = useCallback(() => {
    if (!selected) return;
    const [kind, rest] = [selected.slice(0, 1), selected.slice(2)];
    if (kind === 'm' && !reviewing) dispatch({ type: 'mark/delete', part, index: Number(rest) });
    else if (kind === 'a') dispatch({ type: 'review/delete-added', part, index: Number(rest) });
    else return;
    setSelected(null);
  }, [selected, reviewing, part]);

  // Keys: Delete removes the selected mark or added box; K keeps and R rejects the selected suggestion; N goes on.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLSelectElement || e.metaKey || e.ctrlKey || e.altKey) return;
      const suggestion = selected?.startsWith('s:') ? selected.slice(2) : null;
      if (e.key === 'Delete' || e.key === 'Backspace') removeSelected();
      else if (reviewing && suggestion && e.key.toLowerCase() === 'k') decide(suggestion, 'keep');
      else if (reviewing && suggestion && e.key.toLowerCase() === 'r') decide(suggestion, 'reject');
      else if (reviewing && e.key.toLowerCase() === 'n') goToNext(suggestion);
      else return;
      e.preventDefault();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [selected, reviewing, removeSelected, decide, goToNext]);

  const boxes: CanvasBox[] = [];
  if (!reviewing) {
    for (const { id } of parts) {
      if (id === part) continue;
      draft[id].marks.forEach((box, i) => boxes.push({ key: `l:${id}:${i}`, box, kind: 'lookalike', selectable: false, editable: false }));
    }
    partDraft.marks.forEach((box, i) =>
      boxes.push({ key: `m:${i}`, box, kind: 'mark', label: String(i + 1), selectable: true, editable: !job.active }),
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

  const onDraw = (box: Box) => {
    if (reviewing) dispatch({ type: 'review/add', part, box });
    else if (!job.active) dispatch({ type: 'mark/add', part, box });
  };

  const onChange = (key: string, box: Box) => {
    const [kind, rest] = [key.slice(0, 1), key.slice(2)];
    if (kind === 'm') dispatch({ type: 'mark/fix', part, index: Number(rest), box });
    else if (kind === 's') dispatch({ type: 'review/fix', part, id: rest, box });
    else if (kind === 'a') dispatch({ type: 'review/fix-added', part, index: Number(rest), box });
  };

  const selectedSuggestion = selected?.startsWith('s:') ? selected.slice(2) : null;
  const selectedDecision = selectedSuggestion ? partDraft.review.decisions[selectedSuggestion] : undefined;
  let controls: React.ReactNode = null;
  if (selectedSuggestion) {
    controls = selectedDecision ? (
      <button type="button" onClick={() => decide(selectedSuggestion, 'undo')} className={`${SMALL} bg-[#F3EDE6] text-[#140E0B]`}>
        <Undo2 className="size-4" /> Undo
      </button>
    ) : (
      <>
        <button type="button" onClick={() => decide(selectedSuggestion, 'keep')} className={`${SMALL} bg-[#5BC0EB] text-[#0B0908]`}>
          <Check className="size-4" strokeWidth={3} /> Keep
        </button>
        <button type="button" onClick={() => decide(selectedSuggestion, 'reject')} className={`${SMALL} bg-[#FF4D1A] text-white`}>
          <X className="size-4" strokeWidth={3} /> Reject
        </button>
      </>
    );
  } else if (selected?.startsWith('m:') && !reviewing && !job.active) {
    controls = (
      <button type="button" onClick={removeSelected} className={`${SMALL} bg-[#F3EDE6] text-[#140E0B]`}>
        <Trash2 className="size-4" /> Delete mark
      </button>
    );
  } else if (selected?.startsWith('a:')) {
    controls = (
      <button type="button" onClick={removeSelected} className={`${SMALL} bg-[#F3EDE6] text-[#140E0B]`}>
        <Trash2 className="size-4" /> Remove
      </button>
    );
  }

  return (
    <div className="flex flex-1 min-h-0 flex-col lg:flex-row">
      <div className="relative min-h-[48vh] flex-1">
        <PhotoCanvas
          url={photo.url}
          width={photo.width}
          height={photo.height}
          alt={`Tower photo ${photo.sha256.slice(0, 8)}: draw boxes around each ${label.label.toLowerCase()}`}
          boxes={boxes}
          selected={selected}
          tool={tool}
          disabled={!work.data}
          focus={focus}
          onSelect={setSelected}
          onDraw={onDraw}
          onChange={onChange}
          selectedControls={controls}
        />
      </div>

      <aside className="flex shrink-0 flex-col gap-4 overflow-y-auto border-t border-white/10 p-4 lg:w-[380px] lg:border-t-0 lg:border-l max-lg:max-h-[46vh]">
        {work.isPending && <p className="m-0 text-[15px] text-[#D8CCBF]">Loading this photo's marks…</p>}
        {work.error && (
          <p role="alert" className="m-0 text-[15px] text-[#E9B987]">
            {work.error.message}
          </p>
        )}

        <div className="flex flex-col gap-1.5" role="radiogroup" aria-label="Part to mark">
          {parts.map(({ id, label: name }) => {
            const partWorkLine = work.data?.parts[id];
            const line = partWorkLine ? countLine(partWorkLine, draft[id].review) : null;
            const partJob = jobState(work.data?.parts[id]?.job ?? null);
            return (
              <button
                key={id}
                type="button"
                role="radio"
                aria-checked={part === id}
                onClick={() => pickPart(id)}
                className={`flex items-center justify-between gap-3 min-h-12 rounded-[14px] px-3.5 py-2 text-left border cursor-pointer ${
                  part === id ? 'bg-[#3A2A21] border-[#D9A066]/60' : 'bg-white/[0.04] border-white/[0.07] hover:bg-white/[0.07]'
                }`}
              >
                <span className="flex flex-col">
                  <span className="text-[15px] font-semibold text-[#F3EDE6]">{name}</span>
                  {(partJob.active ? partJob.line : line) && (
                    <span className="text-[12px] text-[#B8A99B] tabular-nums">{partJob.active ? partJob.line : line}</span>
                  )}
                </span>
                {partJob.active && <LoaderCircle className="size-4 shrink-0 animate-spin text-[#D9A066]" />}
              </button>
            );
          })}
        </div>

        <div className="grid grid-cols-2 gap-[2px] rounded-[12px] bg-[#221813] p-[3px]" role="group" aria-label="Tool">
          {(
            [
              ['draw', 'Draw', PenLine],
              ['move', 'Move', Hand],
            ] as const
          ).map(([id, name, Icon]) => (
            <button
              key={id}
              type="button"
              aria-pressed={tool === id}
              onClick={() => setTool(id)}
              className={`inline-flex items-center justify-center gap-1.5 min-h-9 rounded-[9px] text-[13px] cursor-pointer ${
                tool === id ? 'bg-[#3A2A21] text-[#F3EDE6] font-bold' : 'text-[#B8A99B] font-semibold hover:text-[#F3EDE6]'
              }`}
            >
              <Icon className="size-4" /> {name}
            </button>
          ))}
        </div>

        {work.data && scanBusy && (
          <p role="status" className="m-0 flex items-center gap-2 text-[14px] leading-snug text-[#D8CCBF]">
            <LoaderCircle className="size-4 shrink-0 animate-spin text-[#D9A066]" />
            {`Looking for ${label.plural} on this photo… They show here as soon as the scan reaches it; you only check them.`}
          </p>
        )}

        {work.data && !reviewing && !scanBusy && (
          <div className="flex flex-col gap-2.5">
            <p className="m-0 text-[14px] leading-snug text-[#D8CCBF]">
              {scanned
                ? `This photo was not scanned for ${label.plural} (it has no camera angle, or could not be read). Draw a box around one you see, then let the model find the rest.`
                : `Draw a box around at least ${MARKS_TO_FIND} ${label.plural}, then let the finder suggest the rest.`}
              {parts.length > 1 && !scanned && (
                <>
                  {' '}
                  You can mark the other parts too: they are saved when you find the rest, and the finder is given them as look-alikes
                  (things that are not a {label.label.toLowerCase()}) where that was measured to help.
                </>
              )}
            </p>
            <span className="text-[13px] text-[#B8A99B] tabular-nums">
              {partDraft.marks.length} marked
              {partDraft.marks.length < marksNeeded && ` · ${marksNeeded - partDraft.marks.length} more to find the rest`}
            </span>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                className={PRIMARY}
                disabled={partDraft.marks.length < marksNeeded || job.active || busy}
                onClick={findTheRest}
              >
                {busy ? <LoaderCircle className="size-4 animate-spin" /> : <ScanSearch className="size-4" />}
                Find the rest
              </button>
              {job.done && remarking[part] && (
                <button type="button" className={SECONDARY} onClick={() => setRemarking((r) => ({ ...r, [part]: false }))}>
                  Back to the review
                </button>
              )}
            </div>
            {job.line && (
              <p role={job.failed ? 'alert' : 'status'} className={`m-0 text-[14px] ${job.failed ? 'text-[#E9B987]' : 'text-[#D8CCBF]'}`}>
                {job.line}
              </p>
            )}
            {findError && (
              <p
                role="alert"
                className="m-0 rounded-[12px] bg-[#E9B987]/10 border border-[#E9B987]/30 px-3 py-2 text-[14px] text-[#E9B987]"
              >
                {findError}
              </p>
            )}
          </div>
        )}

        {work.data && reviewing && partWork && (
          <div className="flex flex-col gap-2.5">
            <p role="status" className="m-0 text-[17px] font-bold text-[#F3EDE6] tabular-nums">
              {countLine(partWork, partDraft.review)}
            </p>
            <p className="m-0 text-[14px] leading-snug text-[#D8CCBF]">
              {scanned
                ? partWork.suggestions.length > 0
                  ? `The model found ${partWork.suggestions.length} ${partWork.suggestions.length === 1 ? label.label.toLowerCase() : label.plural} on this photo. Tap a blue box to keep or reject it, drag its corners to fix it, and draw a box around any it missed. Nothing is counted until you save.`
                  : `The model found no ${label.plural} on this photo. Draw a box around any it missed, then save your review: nothing is counted until you save.`
                : partWork.suggestions.length > 0
                  ? `Tap a blue box to keep or reject it, drag its corners to fix it, and draw a box around any ${label.label.toLowerCase()} the finder missed. Nothing is counted until you save.`
                  : `The finder found no more ${label.plural}. Draw a box around any it missed, then save your review: nothing is counted until you save.`}
            </p>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                className={PRIMARY}
                disabled={!maySaveReview(partWork, partDraft.review) || saveReview.isPending}
                onClick={saveCheck}
              >
                {saveReview.isPending ? <LoaderCircle className="size-4 animate-spin" /> : <Check className="size-4" />}
                Save review
              </button>
              {left.length > 0 && (
                <button type="button" className={SECONDARY} onClick={() => goToNext(selectedSuggestion)}>
                  Next to check
                </button>
              )}
              {!scanned && (
                <button
                  type="button"
                  className={SECONDARY}
                  onClick={() => {
                    setRemarking((r) => ({ ...r, [part]: true }));
                    setSelected(null);
                  }}
                >
                  Change marks
                </button>
              )}
            </div>
            {saveReview.error && (
              <p
                role="alert"
                className="m-0 rounded-[12px] bg-[#E9B987]/10 border border-[#E9B987]/30 px-3 py-2 text-[14px] text-[#E9B987]"
              >
                {saveReview.error.message}
              </p>
            )}
          </div>
        )}

        <div className="flex flex-wrap gap-x-3 gap-y-1.5">
          <Swatch kind="mark" label="Your marks" />
          {reviewing ? (
            <>
              <Swatch kind="suggestion" label="To check" />
              <Swatch kind="kept" label="Kept" />
              <Swatch kind="added" label="Added by you" />
              <Swatch kind="rejected" label="Rejected" />
            </>
          ) : (
            <Swatch kind="lookalike" label="Other parts" />
          )}
        </div>
      </aside>
    </div>
  );
}

/** The marking screen over the whole window, for one photo at a time of a tower inspection. */
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
      // No tower cards outside Telecom, so no server list of photos to check: go on to the next photo.
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
              aria-label="Close the marking screen"
              onClick={close}
              className="flex size-10 items-center justify-center rounded-full text-[#B8A99B] hover:bg-white/10 hover:text-[#F3EDE6] cursor-pointer"
            >
              <X className="size-5" />
            </button>
            <div className="flex min-w-0 flex-1 basis-[calc(100%-3.5rem)] flex-col sm:basis-0">
              <DialogPrimitive.Title className="m-0 truncate text-[17px] font-bold">
                {towerWork ? 'Check tower photos' : 'Check photos'} · {layerName}
              </DialogPrimitive.Title>
              <span className="truncate text-[13px] text-[#B8A99B] tabular-nums">
                Photo {index + 1} of {photos.length}
                {taken && ` · ${taken}`}
              </span>
            </div>
            <div className="flex items-center gap-1.5 max-sm:w-full max-sm:justify-end">
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
            <PhotoWorkspace key={photo.sha256} layerId={layerId} photo={photo} parts={parts} scanning={scanning} onDirtyChange={setDirty} />
          ) : (
            <p className="m-0 p-6 text-[15px] text-[#D8CCBF]">This inspection has no photos.</p>
          )}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
