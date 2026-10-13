import { useQueryClient } from '@tanstack/react-query';
import { ChevronLeft, LoaderCircle, ScanSearch } from 'lucide-react';
import type { Map as MLMap } from 'maplibre-gl';
import { useEffect, useMemo, useState } from 'react';
import {
  AnswerBody,
  AnswerDownloads,
  AnswerTerms,
  AskSageChips,
  CardButton,
  CardsPanel,
  panelOffset,
  readCollapsed,
  StatusIcon,
  writeCollapsed,
} from '@/components/CardAnswer';
import { PhotoMarking } from '@/components/towers/PhotoMarking';
import { photosToCheckNow, useInspectionPhotos, useTowerCardAnswer, useTowerCards } from '@/hooks/useTowers';
import type { AnswerItem } from '@/lib/cardAnswer';
import { isPhotoInspectionLayer } from '@/lib/inspectionPhoto';
import { TOWER_AUDIENCES, type TowerAudience, type TowerCardAnswer } from '@/lib/towerContract';
import { photoToOpen } from '@/lib/towerReview';
import type { MapLayer } from '@/lib/types';

// The tower cards for a photo inspection in a telecom project, over the map where the drone cards show for farms.
// The server decides whether there are any (404 when towers are off or the project is not telecom: no panel) and
// writes every word and number; this panel lays them out and opens the marking screen.

const COLLAPSED_KEY = 'ingabe.towerCards.collapsed';

function AnswerView({
  answer,
  onBack,
  onGoTo,
  openItem,
  onAskSage,
}: {
  answer: TowerCardAnswer;
  onBack: () => void;
  onGoTo: (lon: number, lat: number) => void;
  openItem: (item: AnswerItem) => (() => void) | null;
  onAskSage: (prompt: string) => void;
}) {
  return (
    <div className="flex flex-col gap-3">
      <button
        type="button"
        onClick={onBack}
        className="self-start inline-flex items-center gap-1 min-h-9 pr-3 pl-1 rounded-full text-[14px] font-semibold text-[#D9A066] hover:bg-white/5 cursor-pointer"
      >
        <ChevronLeft className="size-5" /> Checks
      </button>
      <div className="flex items-start gap-2">
        <span className="mt-2">
          <StatusIcon status={answer.status} />
        </span>
        <h3 className="m-0 text-[22px] font-bold leading-tight tracking-[-0.015em] text-[#F3EDE6]">{answer.title}</h3>
      </div>
      <AnswerBody answer={answer} onGoTo={onGoTo} openItem={openItem} />
      <AskSageChips asks={answer.ask_sage ?? []} onAsk={onAskSage} label="Ask Sage for more" />
      <AnswerDownloads downloads={answer.downloads ?? []} />
      <AnswerTerms terms={answer.terms ?? []} />
    </div>
  );
}

export function TowerCards({
  map,
  layers,
  hiddenLayerIDs,
  historyOpen,
  onAskSage,
}: {
  map: MLMap | null;
  layers: MapLayer[];
  hiddenLayerIDs: string[];
  /** "Previous chats" is open; below xl it covers the right side of the map, so the cards step aside. */
  historyOpen: boolean;
  /** Sends a question to Sage in the chat. */
  onAskSage: (prompt: string) => void;
}) {
  const queryClient = useQueryClient();
  const inspections = useMemo(
    () => layers.filter((l) => isPhotoInspectionLayer(l) && !hiddenLayerIDs.includes(l.id)),
    [layers, hiddenLayerIDs],
  );
  const [layerId, setLayerId] = useState<string | null>(null);
  const [audience, setAudience] = useState<TowerAudience>('owner');
  const [openCard, setOpenCard] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState(() => readCollapsed(COLLAPSED_KEY));
  const [marking, setMarking] = useState<{ index: number; note: string | null } | null>(null);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);

  // Follow the inspections on the map: keep the chosen one while it is shown, else the first.
  useEffect(() => {
    if (!inspections.some((l) => l.id === layerId)) {
      setLayerId(inspections[0]?.id ?? null);
      setOpenCard(null);
      setMarking(null);
    }
  }, [inspections, layerId]);

  const cards = useTowerCards(layerId, audience);
  const photos = useInspectionPhotos(layerId);
  const answer = useTowerCardAnswer(layerId, openCard, audience);

  // The panel shows once the server has said this layer is tower work: an answer, or a conflict only a Telecom
  // inspection can get (409). Not while asking (every photo inspection asks, in every project), not on a 404
  // (switched off, or not a Telecom project) and not on a failed request.
  // Outside Telecom the photos load but the cards answer 404 (no tower cards there): the panel shows the photos to
  // mark without the cards.
  const noCards = cards.error?.status === 404;
  const heading = noCards ? 'Photo checks' : (cards.data?.heading ?? 'Tower checks');
  const noTowers = photos.error?.status === 404 || (noCards && !photos.data);
  const towerWork = !!(cards.data || photos.data) || cards.error?.status === 409 || photos.error?.status === 409;
  if (!layerId || noTowers || !towerWork) return null;

  const layer = inspections.find((l) => l.id === layerId);
  const photoList = photos.data?.photos ?? [];

  const checkPhotos = async () => {
    if (noCards) {
      setMarking({ index: 0, note: null });
      return;
    }
    setSearching(true);
    setSearchError(null);
    try {
      const next = photoToOpen(await photosToCheckNow(queryClient, layerId), photoList, null);
      setMarking({ index: next.index ?? 0, note: next.note });
    } catch (error) {
      setSearchError(error instanceof Error ? error.message : String(error));
    } finally {
      setSearching(false);
    }
  };

  // The tower cards' places are photos (ids their sha256): one opens on the marking screen, not on the map, since
  // many have no GPS position.
  const openPhoto = (item: AnswerItem) => {
    const index = photoList.findIndex((p) => p.sha256 === item.id);
    return index < 0 ? null : () => setMarking({ index, note: null });
  };

  const setCollapsedAndSave = (value: boolean) => {
    setCollapsed(value);
    writeCollapsed(COLLAPSED_KEY, value);
  };

  return (
    <>
      <CardsPanel
        ariaLabel="Checks for this tower inspection"
        heading={heading}
        hideLabel={`Hide the ${heading.toLowerCase()}`}
        collapsed={collapsed}
        collapsedLabel={heading}
        onCollapsedChange={setCollapsedAndSave}
        historyOpen={historyOpen}
      >
        {!openCard && (
          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-1">
              {inspections.length > 1 ? (
                <select
                  aria-label="Photo inspection"
                  value={layerId}
                  onChange={(e) => {
                    setLayerId(e.target.value);
                    setOpenCard(null);
                  }}
                  className="bg-transparent text-[22px] font-bold tracking-[-0.015em] text-[#F3EDE6] outline-none cursor-pointer max-w-full"
                >
                  {inspections.map((l) => (
                    <option key={l.id} value={l.id} className="bg-[#17110E] text-base">
                      {l.name}
                    </option>
                  ))}
                </select>
              ) : (
                <h2 className="m-0 text-[22px] font-bold leading-tight tracking-[-0.015em] text-[#F3EDE6] break-words">{layer?.name}</h2>
              )}
              {photos.data && (
                <span className="text-[13px] text-[#B8A99B] tabular-nums">
                  {photoList.length} {photoList.length === 1 ? 'photo' : 'photos'}
                </span>
              )}
            </div>

            {!noCards && (
              <div className="grid grid-cols-2 gap-[2px] rounded-[12px] bg-[#221813] p-[3px]" role="group" aria-label="Who is reading">
                {TOWER_AUDIENCES.map((a) => (
                  <button
                    key={a.id}
                    type="button"
                    aria-pressed={audience === a.id}
                    onClick={() => setAudience(a.id)}
                    className={`min-h-8 rounded-[9px] text-[12px] cursor-pointer ${
                      audience === a.id ? 'bg-[#3A2A21] text-[#F3EDE6] font-bold' : 'text-[#B8A99B] font-semibold hover:text-[#F3EDE6]'
                    }`}
                  >
                    {a.label}
                  </button>
                ))}
              </div>
            )}

            <div className="flex flex-col gap-1.5">
              <button
                type="button"
                disabled={searching || photoList.length === 0}
                onClick={checkPhotos}
                className="inline-flex items-center justify-center gap-2 min-h-11 px-4 rounded-[14px] text-[15px] font-semibold bg-[#D9A066] text-[#140E0B] hover:bg-[#E9B987] disabled:opacity-60 cursor-pointer"
              >
                {searching ? <LoaderCircle className="size-4 animate-spin" /> : <ScanSearch className="size-4" />}
                {searching ? 'Finding the next photo to check…' : 'Check photos'}
              </button>
              {photos.error && <p className="m-0 text-[13px] text-[#E9B987]">{photos.error.message}</p>}
              {searchError && (
                <p role="alert" className="m-0 text-[13px] text-[#E9B987]">
                  {searchError}
                </p>
              )}
            </div>

            {cards.isPending && !noCards && <p className="m-0 py-2 text-[15px] text-[#D8CCBF]">Loading the checks…</p>}
            {cards.error && !noCards && <p className="m-0 py-2 text-[15px] text-[#E9B987]">{cards.error.message}</p>}
            {cards.data && (
              <div className="flex flex-col gap-2.5">
                {cards.data.cards.map((card) => (
                  <CardButton
                    key={card.id}
                    status={<StatusIcon status={card.status} />}
                    title={card.title}
                    preview={card.preview}
                    onClick={() => setOpenCard(card.id)}
                  />
                ))}
              </div>
            )}
          </div>
        )}
        {openCard && answer.isPending && <p className="m-0 py-6 text-[15px] text-[#D8CCBF]">Working out the answer…</p>}
        {openCard && answer.error && (
          <div className="flex flex-col gap-3 py-4">
            <p className="m-0 text-[15px] text-[#E9B987]">{answer.error.message}</p>
            <button
              type="button"
              onClick={() => setOpenCard(null)}
              className="self-start text-[14px] font-semibold text-[#D9A066] cursor-pointer"
            >
              Back to the checks
            </button>
          </div>
        )}
        {openCard && answer.data && (
          <AnswerView
            answer={answer.data}
            onBack={() => setOpenCard(null)}
            onGoTo={(lon, lat) => map?.flyTo({ center: [lon, lat], zoom: 18.5, offset: panelOffset(), duration: 900 })}
            openItem={openPhoto}
            onAskSage={onAskSage}
          />
        )}
      </CardsPanel>
      {marking && photoList.length > 0 && (
        <PhotoMarking
          layerId={layerId}
          layerName={layer?.name ?? 'Photo inspection'}
          photos={photoList}
          parts={photos.data?.parts}
          startIndex={marking.index}
          note={marking.note}
          onClose={() => setMarking(null)}
        />
      )}
    </>
  );
}
