import { BookOpen, Camera, Check, ChevronDown, ChevronLeft, CircleDashed, Hammer, Plane, X } from 'lucide-react';
import { type GeoJSONSource, type MapLayerMouseEvent, type Map as MLMap, Popup } from 'maplibre-gl';
import { useEffect, useMemo, useRef, useState } from 'react';
import { type DroneCard, type DroneCardAnswer, type DroneDeck, useDroneCardAnswer, useDroneDeck } from '@/hooks/useDroneCards';
import type { MapLayer } from '@/lib/types';

// The question cards for a drone photo, over the map. Everything shown comes
// from the server (src/services/drone_cards.py); this file only lays it out
// and draws the answer's outlines on the photo.

const FONT = "-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Helvetica Neue', system-ui, sans-serif";
const COLLAPSED_KEY = 'ingabe.droneCards.collapsed';

// On phones the panel is a sheet over the lower part of the map, above the chat box. On larger screens it
// stops 280px above the bottom: the chat box and Sage's reply above it take about 260px there.
const PHONE_SHEET_SHARE = 0.52;

const SOURCE = 'drone-card-overlay';
const LAYERS = { fill: 'drone-card-fill', casing: 'drone-card-casing', line: 'drone-card-line' };
const EMPTY: GeoJSON.FeatureCollection = { type: 'FeatureCollection', features: [] };

// How each kind of answer is drawn on the photo (styling only; the kind comes from the server).
const OVERLAY_STYLE: Record<
  NonNullable<DroneCardAnswer['overlay']>['kind'],
  { fill: string; fillOpacity: number; line: string; width: number }
> = {
  bare: { fill: '#F0C896', fillOpacity: 0.32, line: '#FFF7EC', width: 2 },
  attention: { fill: '#D9A066', fillOpacity: 0.22, line: '#D9A066', width: 3 },
  good: { fill: '#F3EDE6', fillOpacity: 0.1, line: '#F3EDE6', width: 2.5 },
  outline: { fill: '#F3EDE6', fillOpacity: 0, line: '#F3EDE6', width: 2 },
};

function readCollapsed(): boolean {
  try {
    return localStorage.getItem(COLLAPSED_KEY) === '1';
  } catch {
    return false;
  }
}

function writeCollapsed(collapsed: boolean) {
  try {
    localStorage.setItem(COLLAPSED_KEY, collapsed ? '1' : '0');
  } catch {
    /* private window: the choice holds for this session */
  }
}

function StatusLine({ card }: { card: DroneCard }) {
  const icon = {
    ready: <Check className="size-3.5 text-[#D9A066]" strokeWidth={2.5} />,
    partly: <CircleDashed className="size-3.5 text-[#D9A066]" strokeWidth={2.5} />,
    needs_flight: <Plane className="size-3.5 text-[#B8A99B]" strokeWidth={2.2} />,
    needs_camera: <Camera className="size-3.5 text-[#B8A99B]" strokeWidth={2.2} />,
    to_build: <Hammer className="size-3.5 text-[#8C7B6E]" strokeWidth={2.2} />,
    learn: <BookOpen className="size-3.5 text-[#D9A066]" strokeWidth={2.2} />,
  }[card.status];
  return (
    <span className="inline-flex items-center gap-1.5 text-[12px] font-semibold text-[#D8CCBF]">
      {icon}
      {card.status_label}
    </span>
  );
}

function CardButton({ card, onOpen }: { card: DroneCard; onOpen: (id: string) => void }) {
  return (
    <button
      type="button"
      onClick={() => onOpen(card.id)}
      className="w-full text-left flex flex-col gap-1.5 rounded-[18px] bg-[#221813] border border-white/[0.07] px-4 py-3.5 hover:border-[#D9A066]/50 focus-visible:outline-2 focus-visible:outline-[#D9A066] transition-colors cursor-pointer"
    >
      <StatusLine card={card} />
      <span className="text-[17px] font-bold leading-snug tracking-[-0.01em] text-[#F3EDE6]">{card.question}</span>
      <span className="text-[14px] leading-snug text-[#B8A99B]">{card.preview}</span>
    </button>
  );
}

function SureBars({ bars }: { bars: number }) {
  return (
    <span className="flex gap-[3px]" aria-hidden="true">
      {[1, 2, 3].map((i) => (
        <span key={i} className={`h-4 w-[6px] rounded-[3px] ${i <= bars ? 'bg-[#D9A066]' : 'bg-[#F3EDE6]/20'}`} />
      ))}
    </span>
  );
}

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1 px-4 py-3 border-b border-white/[0.07] last:border-b-0">
      <span className="text-[11px] font-semibold uppercase tracking-[0.05em] text-[#B8A99B]">{label}</span>
      {children}
    </div>
  );
}

function AnswerView({ answer, onBack }: { answer: DroneCardAnswer; onBack: () => void }) {
  const [openTerm, setOpenTerm] = useState<string | null>(null);
  const termRef = useRef<HTMLDivElement>(null);
  const style = answer.overlay ? OVERLAY_STYLE[answer.overlay.kind] : null;
  const term = answer.terms.find((t) => t.id === openTerm);
  useEffect(() => {
    if (openTerm) termRef.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }, [openTerm]);
  return (
    <div className="flex flex-col gap-3">
      <button
        type="button"
        onClick={onBack}
        className="self-start inline-flex items-center gap-1 min-h-9 pr-3 pl-1 rounded-full text-[14px] font-semibold text-[#D9A066] hover:bg-white/5 cursor-pointer"
      >
        <ChevronLeft className="size-5" /> Questions
      </button>
      <div className="flex flex-col gap-1.5">
        <span className="text-[11px] font-semibold uppercase tracking-[0.07em] text-[#D9A066]">
          {answer.service} · {answer.service_name}
        </span>
        <h3 className="m-0 text-[22px] font-bold leading-tight tracking-[-0.015em] text-[#F3EDE6]">{answer.question}</h3>
        <StatusLine card={answer} />
      </div>

      {answer.overlay && style && (
        <span className="self-start inline-flex items-center gap-2 rounded-full bg-white/[0.06] border border-white/10 px-3 py-1.5 text-[13px] font-semibold text-[#F3EDE6]">
          <span
            className="inline-block h-3 w-4 rounded-[3px]"
            style={{ border: `2px solid ${style.line}`, background: `${style.fill}${style.fillOpacity ? '55' : '00'}` }}
          />
          {answer.overlay.legend}
        </span>
      )}

      <div className="flex flex-col rounded-[18px] bg-[#1E1612] border border-white/[0.06]">
        <Section label="What and where">
          <p className="m-0 text-[15px] leading-snug text-[#F3EDE6]">{answer.what}</p>
        </Section>
        {answer.facts.length > 0 && (
          <div className="flex flex-col px-4 py-2 border-b border-white/[0.07]">
            {answer.facts.map((fact) => (
              <div key={fact.label} className="flex justify-between gap-3 py-1 text-[14px]">
                <span className="text-[#B8A99B]">{fact.label}</span>
                <span className="font-semibold text-[#F3EDE6] text-right tabular-nums">{fact.value}</span>
              </div>
            ))}
          </div>
        )}
        <Section label="Why it matters">
          <p className="m-0 text-[15px] leading-snug text-[#F3EDE6]">{answer.why}</p>
        </Section>
        <Section label="What to do">
          <p className="m-0 text-[15px] leading-snug text-[#F3EDE6]">{answer.todo}</p>
        </Section>
        <Section label="How sure">
          <div className="flex items-center gap-2.5">
            <SureBars bars={answer.how_sure.bars} />
            <span className="text-[15px] font-bold text-[#F3EDE6]">{answer.how_sure.label}</span>
          </div>
          <ul className="m-0 mt-1 pl-4 list-disc text-[13px] leading-snug text-[#B8A99B]">
            {answer.how_sure.because.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
          {answer.how_sure.surer && (
            <p className="m-0 mt-1 text-[13px] leading-snug text-[#E9B987]">To be surer: {answer.how_sure.surer}</p>
          )}
        </Section>
      </div>

      {answer.terms.length > 0 && (
        <div className="flex flex-col gap-2">
          <span className="text-[13px] font-semibold text-[#B8A99B]">Words in this answer</span>
          <div className="flex flex-wrap gap-2">
            {answer.terms.map((t) => (
              <button
                key={t.id}
                type="button"
                aria-expanded={openTerm === t.id}
                onClick={() => setOpenTerm(openTerm === t.id ? null : t.id)}
                className={`min-h-9 px-3.5 rounded-full text-[13px] font-semibold border cursor-pointer ${
                  openTerm === t.id
                    ? 'bg-[#F3EDE6] text-[#140E0B] border-transparent'
                    : 'bg-white/[0.06] text-[#F3EDE6] border-white/10 hover:bg-white/10'
                }`}
              >
                {t.word}
              </button>
            ))}
          </div>
          {term && (
            <div ref={termRef} className="flex flex-col gap-2 rounded-[16px] bg-[#D9A066]/[0.12] border border-[#D9A066]/30 px-4 py-3">
              <span className="text-[15px] font-bold text-[#F3EDE6]">{term.word}</span>
              <p className="m-0 text-[14px] leading-snug text-[#F3EDE6]">{term.simple}</p>
              <p className="m-0 text-[14px] leading-snug text-[#E6DCD1]">
                <span className="font-semibold">Why it helps you: </span>
                {term.why}
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function DeckView({
  deck,
  photos,
  layerId,
  onPickPhoto,
  onPickAudience,
  onOpen,
}: {
  deck: DroneDeck;
  photos: MapLayer[];
  layerId: string;
  onPickPhoto: (id: string) => void;
  onPickAudience: (id: string) => void;
  onOpen: (id: string) => void;
}) {
  const [servicesOpen, setServicesOpen] = useState(false);
  const { photo } = deck;
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-1">
        {photos.length > 1 ? (
          <select
            aria-label="Drone photo"
            value={layerId}
            onChange={(e) => onPickPhoto(e.target.value)}
            className="bg-transparent text-[22px] font-bold tracking-[-0.015em] text-[#F3EDE6] outline-none cursor-pointer max-w-full"
          >
            {photos.map((p) => (
              <option key={p.id} value={p.id} className="bg-[#17110E] text-base">
                {p.name}
              </option>
            ))}
          </select>
        ) : (
          <h2 className="m-0 text-[22px] font-bold leading-tight tracking-[-0.015em] text-[#F3EDE6] break-words">{photo.name}</h2>
        )}
        {photo.place && <span className="text-[13px] text-[#D8CCBF]">{photo.place}</span>}
        <span className="text-[13px] text-[#B8A99B]">{photo.summary}</span>
      </div>

      <div className="grid grid-cols-4 gap-[2px] rounded-[12px] bg-[#221813] p-[3px]" role="group" aria-label="Who is reading">
        {deck.audiences.map((a) => (
          <button
            key={a.id}
            type="button"
            aria-pressed={deck.audience === a.id}
            onClick={() => onPickAudience(a.id)}
            className={`min-h-8 rounded-[9px] text-[12px] cursor-pointer ${
              deck.audience === a.id ? 'bg-[#3A2A21] text-[#F3EDE6] font-bold' : 'text-[#B8A99B] font-semibold hover:text-[#F3EDE6]'
            }`}
          >
            {a.label}
          </button>
        ))}
      </div>

      <div className="flex flex-col gap-2.5">
        <span className="text-[13px] font-semibold text-[#B8A99B]">For this photo</span>
        {deck.for_you.map((card) => (
          <CardButton key={card.id} card={card} onOpen={onOpen} />
        ))}
      </div>

      <div className="flex flex-col gap-2">
        <button
          type="button"
          aria-expanded={servicesOpen}
          onClick={() => setServicesOpen(!servicesOpen)}
          className="flex items-center justify-between min-h-10 text-[15px] font-bold text-[#F3EDE6] cursor-pointer"
        >
          All {deck.services.length} services
          <ChevronDown className={`size-5 text-[#B8A99B] transition-transform ${servicesOpen ? 'rotate-180' : ''}`} />
        </button>
        {servicesOpen && (
          <div className="flex flex-col rounded-[18px] bg-[#1A1310] border border-white/[0.06]">
            {deck.services.map((service) =>
              service.cards.map((card) => (
                <button
                  key={card.id}
                  type="button"
                  onClick={() => onOpen(card.id)}
                  className="flex flex-col gap-0.5 text-left px-4 py-3 border-b border-white/[0.07] last:border-b-0 hover:bg-white/[0.03] cursor-pointer"
                >
                  <span className="text-[11px] font-semibold uppercase tracking-[0.06em] text-[#D9A066]">
                    {service.service} · {service.name}
                  </span>
                  <span className="text-[14px] font-semibold text-[#F3EDE6]">{card.question}</span>
                  <StatusLine card={card} />
                </button>
              )),
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/** Draws the open answer's outlines on the map, and says the size of a patch when it is clicked. */
function useAnswerOverlay(map: MLMap | null, answer: DroneCardAnswer | null, bounds: [number, number, number, number] | null) {
  useEffect(() => {
    if (!map) return;
    const overlay = answer?.overlay ?? null;
    const apply = () => {
      try {
        if (!map.getSource(SOURCE)) map.addSource(SOURCE, { type: 'geojson', data: EMPTY });
        if (!map.getLayer(LAYERS.fill)) map.addLayer({ id: LAYERS.fill, type: 'fill', source: SOURCE, paint: {} });
        if (!map.getLayer(LAYERS.casing)) map.addLayer({ id: LAYERS.casing, type: 'line', source: SOURCE, paint: {} });
        if (!map.getLayer(LAYERS.line)) map.addLayer({ id: LAYERS.line, type: 'line', source: SOURCE, paint: {} });
      } catch {
        map.once('idle', apply);
        return;
      }
      const style = overlay ? OVERLAY_STYLE[overlay.kind] : OVERLAY_STYLE.outline;
      map.setPaintProperty(LAYERS.fill, 'fill-color', style.fill);
      map.setPaintProperty(LAYERS.fill, 'fill-opacity', overlay ? style.fillOpacity : 0);
      map.setPaintProperty(LAYERS.casing, 'line-color', '#0B0908');
      map.setPaintProperty(LAYERS.casing, 'line-width', style.width + 2.5);
      map.setPaintProperty(LAYERS.casing, 'line-opacity', 0.45);
      map.setPaintProperty(LAYERS.line, 'line-color', style.line);
      map.setPaintProperty(LAYERS.line, 'line-width', style.width);
      (map.getSource(SOURCE) as GeoJSONSource).setData(overlay?.geojson ?? EMPTY);
    };
    apply();
    map.on('style.load', apply);
    return () => {
      map.off('style.load', apply);
      map.off('idle', apply);
      try {
        (map.getSource(SOURCE) as GeoJSONSource | undefined)?.setData(EMPTY);
      } catch {
        /* style mid-reload */
      }
    };
  }, [map, answer]);

  // Bring the photo into view when an answer with outlines opens.
  useEffect(() => {
    if (!map || !answer?.overlay || !bounds) return;
    const padding =
      window.innerWidth >= 640
        ? { top: 70, bottom: 170, left: 60, right: 440 }
        : { top: 70, bottom: Math.round(window.innerHeight * PHONE_SHEET_SHARE) + 110, left: 20, right: 20 };
    map.fitBounds(bounds, { padding, maxZoom: 18, duration: 800 });
  }, [map, answer, bounds]);

  // Click a patch: its own label, written by the server. A new answer closes it.
  // biome-ignore lint/correctness/useExhaustiveDependencies: answer only resets the label
  useEffect(() => {
    if (!map) return;
    let popup: Popup | null = null;
    const onClick = (e: MapLayerMouseEvent) => {
      const label = e.features?.[0]?.properties?.label;
      if (!label) return;
      popup?.remove();
      popup = new Popup({ closeButton: false, className: 'drone-card-popup' }).setLngLat(e.lngLat).setText(String(label)).addTo(map);
    };
    map.on('click', LAYERS.fill, onClick);
    return () => {
      map.off('click', LAYERS.fill, onClick);
      popup?.remove();
    };
  }, [map, answer]);
}

export function DroneCards({
  map,
  layers,
  hiddenLayerIDs,
  historyOpen,
}: {
  map: MLMap | null;
  layers: MapLayer[];
  hiddenLayerIDs: string[];
  /** "Previous chats" is open; below xl it covers the right side of the map, so the cards step aside. */
  historyOpen: boolean;
}) {
  const photos = useMemo(() => layers.filter((l) => l.type === 'raster' && !hiddenLayerIDs.includes(l.id)), [layers, hiddenLayerIDs]);
  const [layerId, setLayerId] = useState<string | null>(null);
  const [audience, setAudience] = useState<string | null>(null);
  const [openCard, setOpenCard] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState(readCollapsed);

  // Follow the photos on the map: keep the chosen one while it is shown, else the first.
  useEffect(() => {
    if (!photos.some((p) => p.id === layerId)) {
      setLayerId(photos[0]?.id ?? null);
      setOpenCard(null);
    }
  }, [photos, layerId]);

  const deck = useDroneDeck(layerId, audience);
  const answer = useDroneCardAnswer(layerId, openCard, audience);
  useAnswerOverlay(map, collapsed ? null : (answer.data ?? null), deck.data?.photo.bounds ?? null);

  // A photo the cards cannot read (not a colour photo, not a raster) shows no panel.
  if (!layerId || (deck.error && [400, 404, 422].includes(deck.error.status))) return null;

  const setCollapsedAndSave = (value: boolean) => {
    setCollapsed(value);
    writeCollapsed(value);
  };

  if (collapsed) {
    return (
      <button
        type="button"
        onClick={() => setCollapsedAndSave(false)}
        style={{ fontFamily: FONT }}
        className={`absolute top-16 right-4 sm:right-14 xl:top-4 z-30 min-h-11 px-4 rounded-full bg-[#110C0A]/85 backdrop-blur-xl border border-white/10 text-[14px] font-semibold text-[#F3EDE6] shadow-lg cursor-pointer ${historyOpen ? 'max-xl:hidden' : ''}`}
      >
        Questions{deck.data ? ` · ${deck.data.for_you.length}` : ''}
      </button>
    );
  }

  return (
    <section
      aria-label="Questions for this drone photo"
      style={{ fontFamily: FONT }}
      className={`absolute z-30 inset-x-2 bottom-[100px] max-h-[52vh] sm:inset-auto sm:bottom-auto sm:top-16 xl:top-4 sm:right-14 sm:w-[372px] sm:max-h-[calc(100%-328px)] xl:max-h-[calc(100%-280px)] flex flex-col rounded-[26px] bg-[#110C0A]/[0.9] backdrop-blur-2xl border border-white/[0.09] shadow-2xl text-[#F3EDE6] ${historyOpen ? 'max-xl:hidden' : ''}`}
    >
      <div className="flex items-center justify-between px-5 pt-4 pb-1">
        <span className="text-[12px] font-semibold uppercase tracking-[0.08em] text-[#D9A066]">Questions</span>
        <button
          type="button"
          aria-label="Hide the questions"
          onClick={() => setCollapsedAndSave(true)}
          className="flex size-8 items-center justify-center rounded-full text-[#B8A99B] hover:bg-white/10 hover:text-[#F3EDE6] cursor-pointer"
        >
          <X className="size-4" />
        </button>
      </div>
      <div className="overflow-y-auto px-5 pb-5 pt-1">
        {deck.isPending && <p className="m-0 py-6 text-[15px] text-[#D8CCBF]">Ingabe is looking at your photo…</p>}
        {deck.error?.status === 409 && (
          <p className="m-0 py-6 text-[15px] text-[#D8CCBF]">
            The photo is still being prepared. The questions will appear when it is ready.
          </p>
        )}
        {deck.error && deck.error.status !== 409 && <p className="m-0 py-6 text-[15px] text-[#E9B987]">{deck.error.message}</p>}
        {deck.data && !openCard && (
          <DeckView
            deck={deck.data}
            photos={photos}
            layerId={layerId}
            onPickPhoto={(id) => {
              setLayerId(id);
              setOpenCard(null);
            }}
            onPickAudience={setAudience}
            onOpen={setOpenCard}
          />
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
              Back to the questions
            </button>
          </div>
        )}
        {openCard && answer.data && <AnswerView answer={answer.data} onBack={() => setOpenCard(null)} />}
      </div>
    </section>
  );
}
