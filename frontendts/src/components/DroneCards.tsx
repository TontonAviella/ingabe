import {
  BookOpen,
  Camera,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CircleDashed,
  Download,
  FileUp,
  Hammer,
  LoaderCircle,
  MessageCircle,
  Plane,
  Shuffle,
  X,
} from 'lucide-react';
import { type ExpressionSpecification, type GeoJSONSource, type MapLayerMouseEvent, type Map as MLMap, Popup } from 'maplibre-gl';
import { useEffect, useMemo, useRef, useState } from 'react';
import {
  type AskSage,
  type DroneCard,
  type DroneCardAnswer,
  type DroneDeck,
  useAddFarmRecord,
  useChoosePlotSource,
  useDroneCardAnswer,
  useDroneDeck,
} from '@/hooks/useDroneCards';
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
const LAYERS = { fill: 'drone-card-fill', casing: 'drone-card-casing', line: 'drone-card-line', label: 'drone-card-label' };
// The rest of the photo is dimmed under the plots an answer points at, so they stand out at a glance.
const SPOTLIGHT = { source: 'drone-card-spotlight', fill: 'drone-card-spotlight-fill' };
// Exact spots measured inside those plots (bare soil), drawn bright above everything else.
const SPOTS = { source: 'drone-card-spots', fill: 'drone-card-spots-fill', line: 'drone-card-spots-line' };
// A short word on each plot that matters ("Gaps", "Weeds", "Maize"), dark on caramel so it reads like a tag.
// Only once plots are big enough to carry one: further out, dozens of tags hide the photo.
const BADGE_LAYER = 'drone-card-badge';
const BADGE_MIN_ZOOM = 16;
// Far out, a plot is a few pixels wide: it shows as a solid patch of colour with a hairline edge. Close in,
// the colour fades so the crop inside shows, and the edge carries the plot.
const FAR_ZOOM = 14;
const NEAR_ZOOM = 18;
const EMPTY: GeoJSON.FeatureCollection = { type: 'FeatureCollection', features: [] };
// The satellite basemap's glyph server has this font; plot numbers show from this zoom on.
const LABEL_FONT = ['Open Sans Semibold'];
const LABEL_MIN_ZOOM = 15.5;

type Paint<T> = T | ExpressionSpecification;
type OverlayStyle = {
  fill: Paint<string>;
  /** Fill close in; `farFill` is the fill zoomed out (the same when not given). */
  fillOpacity: Paint<number>;
  farFill?: Paint<number>;
  line: Paint<string>;
  width: Paint<number>;
  /** Plot numbers on the photo. */
  numbers: boolean;
  /** The legend's swatch. */
  swatch: { fill: string; line: string };
};

// Plots in the least green fifth are filled, plots showing mostly soil shaded lightly; the server sets each group.
const BY_GROUP = <T,>(leastGreen: T, mostlySoil: T, other: T): ExpressionSpecification =>
  ['match', ['get', 'group'], 'least_green', leastGreen, 'mostly_soil', mostlySoil, other] as ExpressionSpecification;

// Crop colours on the crop map: warm, told apart, and no green (the photo is green). Unknown crops are grey.
const CROP_COLOURS: Record<string, string> = {
  maize: '#F2C14E',
  beans: '#C2410C',
  cassava: '#F0C896',
  banana: '#B5651D',
  sorghum: '#8E3B2F',
  pineapple: '#E8743B',
  rice: '#FFF1E6',
  irish_potato: '#A0785A',
  sweet_potato: '#D97757',
  soybean: '#E9B987',
  groundnut: '#C9A27E',
  coffee: '#6B3E26',
  tea: '#9C6B4E',
  sugarcane: '#D4A373',
  vegetables: '#E07A5F',
  fruit_trees: '#B56576',
  grass_or_pasture: '#BFB5A8',
  woodlot: '#7A6A5E',
  fallow_or_bare: '#5C4A3E',
  other: '#9E9188',
};
const UNSURE_COLOUR = '#8C7B6E';
const BY_CROP = ['match', ['get', 'crop'], ...Object.entries(CROP_COLOURS).flat(), UNSURE_COLOUR] as unknown as ExpressionSpecification;
const FLAGGED: ExpressionSpecification = ['==', ['get', 'flag'], true];

// How each kind of answer is drawn on the photo (styling only; the kind comes from the server).
const OVERLAY_STYLE: Record<NonNullable<DroneCardAnswer['overlay']>['kind'], OverlayStyle> = {
  bare: { fill: '#F0C896', fillOpacity: 0.32, line: '#FFF7EC', width: 2, numbers: false, swatch: { fill: '#F0C89655', line: '#FFF7EC' } },
  attention: {
    fill: '#D9A066',
    fillOpacity: 0.22,
    line: '#D9A066',
    width: 3,
    numbers: false,
    swatch: { fill: '#D9A06655', line: '#D9A066' },
  },
  good: { fill: '#F3EDE6', fillOpacity: 0.1, line: '#F3EDE6', width: 2.5, numbers: false, swatch: { fill: '#F3EDE622', line: '#F3EDE6' } },
  outline: { fill: '#F3EDE6', fillOpacity: 0, line: '#F3EDE6', width: 2, numbers: false, swatch: { fill: '#00000000', line: '#F3EDE6' } },
  plots: { fill: '#F3EDE6', fillOpacity: 0.04, line: '#F3EDE6', width: 1.5, numbers: true, swatch: { fill: '#F3EDE611', line: '#F3EDE6' } },
  plot_groups: {
    fill: BY_GROUP('#E8743B', '#F0C896', '#F3EDE6'),
    fillOpacity: BY_GROUP(0.3, 0.12, 0.02),
    farFill: BY_GROUP(0.8, 0.4, 0.03),
    line: BY_GROUP('#FFF1E6', '#F0C896', '#F3EDE6'),
    width: BY_GROUP(2.5, 1, 1),
    numbers: true,
    swatch: { fill: '#E8743B88', line: '#FFF1E6' },
  },
  crop_map: {
    fill: BY_CROP,
    fillOpacity: ['case', ['==', ['get', 'crop'], 'unsure'], 0.04, 0.3],
    farFill: ['case', ['==', ['get', 'crop'], 'unsure'], 0.08, 0.85],
    line: BY_CROP,
    width: 2,
    numbers: true,
    swatch: { fill: '#F2C14E88', line: '#F2C14E' },
  },
  // Plots with a problem are lit caramel far out; close in they stay lit (the rest of the photo is dimmed)
  // and the bare spots inside show red-orange, a colour nothing else on the photo uses.
  plot_flags: {
    fill: '#F2C14E',
    fillOpacity: ['case', FLAGGED, 0.05, 0.02],
    farFill: ['case', FLAGGED, 0.8, 0.03],
    line: ['case', FLAGGED, '#FFF1E6', '#F3EDE6'],
    width: ['case', FLAGGED, 2.5, 1],
    numbers: true,
    swatch: { fill: '#F2C14ECC', line: '#FFF1E6' },
  },
};
const SPOT_COLOUR = '#FF4D1A';

/** A paint value that goes from `far` (zoomed out) to `near` (zoomed in). */
function byZoom<T>(far: Paint<T>, near: Paint<T>): ExpressionSpecification {
  return ['interpolate', ['linear'], ['zoom'], FAR_ZOOM, far, NEAR_ZOOM, near] as ExpressionSpecification;
}

/** Room the panel takes on the map, so what an answer points at stays in view beside or above it. */
function panelPadding() {
  return window.innerWidth >= 640
    ? { top: 70, bottom: 170, left: 60, right: 440 }
    : { top: 70, bottom: Math.round(window.innerHeight * PHONE_SHEET_SHARE) + 110, left: 20, right: 20 };
}

/** Shift that puts a point in the middle of the free part of the map. An offset, not `padding`: padding given to
 * flyTo stays on the map and adds to the next fitBounds, which then cannot fit on a phone and does nothing. */
function panelOffset(): [number, number] {
  const p = panelPadding();
  return [(p.left - p.right) / 2, (p.top - p.bottom) / 2];
}

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
    working: <LoaderCircle className="size-3.5 text-[#D9A066] animate-spin" strokeWidth={2.5} />,
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

function AskSageChips({ asks, onAsk, label }: { asks: AskSage[]; onAsk: (prompt: string) => void; label: string }) {
  if (asks.length === 0) return null;
  return (
    <div className="flex flex-col gap-2">
      <span className="text-[13px] font-semibold text-[#B8A99B]">{label}</span>
      <div className="flex flex-col gap-1.5">
        {asks.map((ask) => (
          <button
            key={ask.label}
            type="button"
            title={ask.prompt}
            onClick={() => onAsk(ask.prompt)}
            className="flex items-center gap-2.5 min-h-11 px-3.5 rounded-[14px] text-left text-[14px] font-semibold text-[#F3EDE6] bg-[#D9A066]/[0.1] border border-[#D9A066]/35 hover:bg-[#D9A066]/[0.18] cursor-pointer"
          >
            <MessageCircle className="size-4 shrink-0 text-[#D9A066]" />
            <span className="leading-snug">{ask.label}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

function AddDocument({ layerId, upload }: { layerId: string; upload: NonNullable<DroneCardAnswer['upload']> }) {
  const add = useAddFarmRecord(layerId);
  const input = useRef<HTMLInputElement>(null);
  const result = add.data;
  return (
    <div className="flex flex-col gap-2">
      <input
        ref={input}
        type="file"
        accept={upload.accept}
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) add.mutate({ href: upload.href, file });
          e.target.value = '';
        }}
      />
      <button
        type="button"
        disabled={add.isPending}
        onClick={() => input.current?.click()}
        className="inline-flex items-center justify-center gap-2 min-h-11 px-4 rounded-[14px] text-[14px] font-semibold bg-[#D9A066] text-[#140E0B] hover:bg-[#E9B987] disabled:opacity-60 cursor-pointer"
      >
        {add.isPending ? <LoaderCircle className="size-4 animate-spin" /> : <FileUp className="size-4" />}
        {add.isPending ? 'Reading your document…' : upload.label}
      </button>
      {result && (
        <p className="m-0 text-[13px] leading-snug text-[#D8CCBF]">
          {result.kind === 'soil_report' && `Read ${result.soil_samples} soil samples.`}
          {result.kind === 'harvest_records' && `Read ${result.harvests} harvests.`}
          {result.kind === 'other' && 'This does not look like a soil report or harvest records; nothing was added to the cards.'}
          {result.warnings.length > 0 && ` Note: ${result.warnings.join(' ')}`}
        </p>
      )}
      {add.error && <p className="m-0 text-[13px] text-[#E9B987]">{add.error.message}</p>}
    </div>
  );
}

function Progress({ progress }: { progress: NonNullable<DroneCardAnswer['progress']> }) {
  const share = progress.parts ? progress.done / progress.parts : 0;
  return (
    <div className="flex flex-col gap-1.5" role="status">
      <div className="h-1.5 rounded-full bg-white/10 overflow-hidden">
        <div
          className="h-full rounded-full bg-[#D9A066] transition-[width] duration-700"
          style={{ width: `${Math.max(4, share * 100)}%` }}
        />
      </div>
      <span className="text-[12px] text-[#B8A99B] tabular-nums">
        Part {progress.done} of {progress.parts}
        {progress.minutes_left !== null && ` · about ${progress.minutes_left} min left`}
      </span>
    </div>
  );
}

function Choices({ layerId, choices }: { layerId: string; choices: NonNullable<DroneCardAnswer['choices']> }) {
  const choose = useChoosePlotSource(layerId);
  return (
    <div className="flex flex-col gap-2">
      <span className="text-[13px] font-semibold text-[#B8A99B]">{choices.label}</span>
      <div className="flex flex-col rounded-[16px] bg-[#1E1612] border border-white/[0.06]" role="radiogroup" aria-label={choices.label}>
        {choices.options.map((option) => {
          const busy = choose.isPending && choose.variables?.source === option.id;
          return (
            <button
              key={option.id}
              type="button"
              role="radio"
              aria-checked={option.selected}
              disabled={choose.isPending || option.selected}
              onClick={() => choose.mutate({ href: choices.href, source: option.id })}
              className="flex items-center justify-between gap-3 min-h-12 px-4 py-2 text-left border-b border-white/[0.07] last:border-b-0 hover:bg-white/[0.03] disabled:cursor-default cursor-pointer"
            >
              <span className="flex flex-col">
                <span className="text-[15px] font-semibold text-[#F3EDE6]">{option.label}</span>
                <span className="text-[13px] text-[#B8A99B]">{option.detail}</span>
              </span>
              {busy ? (
                <LoaderCircle className="size-4 shrink-0 text-[#D9A066] animate-spin" />
              ) : (
                option.selected && <Check className="size-4 shrink-0 text-[#D9A066]" strokeWidth={2.5} />
              )}
            </button>
          );
        })}
      </div>
      {choose.error && <p className="m-0 text-[13px] text-[#E9B987]">{choose.error.message}</p>}
    </div>
  );
}

function AnswerView({
  answer,
  layerId,
  onBack,
  onGoTo,
  onAskSage,
}: {
  answer: DroneCardAnswer;
  layerId: string;
  onBack: () => void;
  onGoTo: (lon: number, lat: number) => void;
  onAskSage: (prompt: string) => void;
}) {
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

      {answer.overlay && style && !answer.overlay.legend_items && (
        <span className="self-start inline-flex items-center gap-2 rounded-full bg-white/[0.06] border border-white/10 px-3 py-1.5 text-[13px] font-semibold text-[#F3EDE6]">
          <span
            className="inline-block h-3 w-4 rounded-[3px]"
            style={{ border: `2px solid ${style.swatch.line}`, background: style.swatch.fill }}
          />
          {answer.overlay.legend}
        </span>
      )}
      {answer.overlay?.legend_items && (
        <div className="flex flex-col gap-1.5">
          <span className="text-[13px] font-semibold text-[#B8A99B]">{answer.overlay.legend}</span>
          <div className="flex flex-wrap gap-1.5">
            {answer.overlay.legend_items.map((item) => {
              const colour = CROP_COLOURS[item.key] ?? UNSURE_COLOUR;
              return (
                <span
                  key={item.key}
                  className="inline-flex items-center gap-1.5 rounded-full bg-white/[0.06] border border-white/10 px-2.5 py-1 text-[12px] font-semibold text-[#F3EDE6]"
                >
                  <span className="inline-block size-2.5 rounded-full" style={{ background: colour }} />
                  {item.label}
                  <span className="text-[#B8A99B] tabular-nums">{item.count}</span>
                </span>
              );
            })}
          </div>
        </div>
      )}

      {answer.overlay?.spots && answer.overlay.spots.features.length > 0 && (
        <span className="self-start inline-flex items-center gap-2 rounded-full bg-white/[0.06] border border-white/10 px-3 py-1.5 text-[13px] font-semibold text-[#F3EDE6]">
          <span className="inline-block h-3 w-4 rounded-[3px] bg-[#FF4D1A] border border-[#FFF1E6]" />
          {answer.overlay.spots_legend}
        </span>
      )}

      {answer.choices && <Choices layerId={layerId} choices={answer.choices} />}

      <div className="flex flex-col rounded-[18px] bg-[#1E1612] border border-white/[0.06]">
        <Section label="What and where">
          <p className="m-0 text-[15px] leading-snug text-[#F3EDE6]">{answer.what}</p>
          {answer.progress && (
            <div className="mt-2">
              <Progress progress={answer.progress} />
            </div>
          )}
        </Section>
        {answer.items.length > 0 && (
          <div className="flex flex-col py-1 border-b border-white/[0.07]">
            <span className="px-4 pt-2 pb-1 text-[11px] font-semibold uppercase tracking-[0.05em] text-[#B8A99B]">Go here first</span>
            {answer.items.map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => onGoTo(item.lon, item.lat)}
                className="flex items-center justify-between gap-3 min-h-11 px-4 py-1.5 text-left hover:bg-white/[0.04] cursor-pointer"
              >
                {item.picture && (
                  <img
                    src={item.picture}
                    alt={`What the vision model saw of ${item.title}`}
                    loading="lazy"
                    className="size-14 shrink-0 rounded-[10px] object-cover border border-white/10 bg-[#221813]"
                  />
                )}
                <span className="flex flex-col flex-1 min-w-0">
                  <span className="text-[15px] font-semibold text-[#F3EDE6]">{item.title}</span>
                  <span className="text-[13px] leading-snug text-[#B8A99B] tabular-nums">{item.detail}</span>
                </span>
                <ChevronRight className="size-4 shrink-0 text-[#B8A99B]" />
              </button>
            ))}
          </div>
        )}
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

      {answer.upload && <AddDocument layerId={layerId} upload={answer.upload} />}

      <AskSageChips asks={answer.ask_sage} onAsk={onAskSage} label="Ask Sage for more" />

      {answer.downloads.length > 0 && (
        <div className="flex flex-col gap-2">
          <span className="text-[13px] font-semibold text-[#B8A99B]">Take it with you</span>
          <div className="flex flex-wrap gap-2">
            {answer.downloads.map((d) => (
              <a
                key={d.href}
                href={d.href}
                download
                className="inline-flex items-center gap-1.5 min-h-9 px-3.5 rounded-full text-[13px] font-semibold bg-white/[0.06] text-[#F3EDE6] border border-white/10 hover:bg-white/10 no-underline"
              >
                <Download className="size-3.5 text-[#D9A066]" />
                {d.label}
              </a>
            ))}
          </div>
        </div>
      )}

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
  onShuffle,
  onAskSage,
}: {
  deck: DroneDeck;
  photos: MapLayer[];
  layerId: string;
  onPickPhoto: (id: string) => void;
  onPickAudience: (id: string) => void;
  onOpen: (id: string) => void;
  onShuffle: () => void;
  onAskSage: (prompt: string) => void;
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
        <div className="flex items-center justify-between">
          <span className="text-[13px] font-semibold text-[#B8A99B]">For this photo</span>
          <button
            type="button"
            onClick={onShuffle}
            className="inline-flex items-center gap-1.5 min-h-8 px-2.5 rounded-full text-[13px] font-semibold text-[#D9A066] hover:bg-white/5 cursor-pointer"
          >
            <Shuffle className="size-3.5" />
            Other questions
          </button>
        </div>
        {deck.for_you.map((card) => (
          <CardButton key={card.id} card={card} onOpen={onOpen} />
        ))}
      </div>

      <AskSageChips asks={deck.ask_sage} onAsk={onAskSage} label="Ask Sage about this photo" />

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
        if (!map.getSource(SPOTLIGHT.source)) map.addSource(SPOTLIGHT.source, { type: 'geojson', data: EMPTY });
        if (!map.getLayer(SPOTLIGHT.fill))
          map.addLayer({
            id: SPOTLIGHT.fill,
            type: 'fill',
            source: SPOTLIGHT.source,
            paint: { 'fill-color': '#0B0908', 'fill-opacity': 0.58 },
          });
        if (!map.getSource(SOURCE)) map.addSource(SOURCE, { type: 'geojson', data: EMPTY });
        if (!map.getLayer(LAYERS.fill)) map.addLayer({ id: LAYERS.fill, type: 'fill', source: SOURCE, paint: {} });
        if (!map.getLayer(LAYERS.casing)) map.addLayer({ id: LAYERS.casing, type: 'line', source: SOURCE, paint: {} });
        if (!map.getLayer(LAYERS.line)) map.addLayer({ id: LAYERS.line, type: 'line', source: SOURCE, paint: {} });
        if (!map.getLayer(LAYERS.label))
          map.addLayer({
            id: LAYERS.label,
            type: 'symbol',
            source: SOURCE,
            minzoom: LABEL_MIN_ZOOM,
            layout: {
              'text-field': ['coalesce', ['get', 'tag'], ['to-string', ['get', 'number']]],
              'text-font': LABEL_FONT,
              'text-size': 12,
              'symbol-placement': 'point',
              visibility: 'none',
            },
            paint: { 'text-color': '#FFF7EC', 'text-halo-color': '#0B0908', 'text-halo-width': 1.6 },
          });
        if (!map.getSource(SPOTS.source)) map.addSource(SPOTS.source, { type: 'geojson', data: EMPTY });
        if (!map.getLayer(SPOTS.fill))
          map.addLayer({
            id: SPOTS.fill,
            type: 'fill',
            source: SPOTS.source,
            paint: { 'fill-color': SPOT_COLOUR, 'fill-opacity': 0.9 },
          });
        if (!map.getLayer(SPOTS.line))
          map.addLayer({
            id: SPOTS.line,
            type: 'line',
            source: SPOTS.source,
            paint: { 'line-color': '#FFF1E6', 'line-width': byZoom(0.4, 1.5) },
          });
        if (!map.getLayer(BADGE_LAYER))
          map.addLayer({
            id: BADGE_LAYER,
            type: 'symbol',
            source: SOURCE,
            minzoom: BADGE_MIN_ZOOM,
            filter: ['has', 'badge'],
            layout: {
              'text-field': ['get', 'badge'],
              'text-font': LABEL_FONT,
              'text-size': 13,
              'text-offset': [0, -1.1],
              'symbol-placement': 'point',
            },
            paint: { 'text-color': '#140E0B', 'text-halo-color': '#F2C14E', 'text-halo-width': 3.5 },
          });
      } catch {
        map.once('idle', apply);
        return;
      }
      const style = overlay ? OVERLAY_STYLE[overlay.kind] : OVERLAY_STYLE.outline;
      map.setPaintProperty(LAYERS.fill, 'fill-color', style.fill);
      map.setPaintProperty(LAYERS.fill, 'fill-opacity', overlay ? byZoom(style.farFill ?? style.fillOpacity, style.fillOpacity) : 0);
      map.setPaintProperty(LAYERS.casing, 'line-color', '#0B0908');
      map.setPaintProperty(LAYERS.casing, 'line-width', byZoom(0, ['+', style.width, 2.5]));
      map.setPaintProperty(LAYERS.casing, 'line-opacity', 0.45);
      map.setPaintProperty(LAYERS.line, 'line-color', style.line);
      map.setPaintProperty(LAYERS.line, 'line-width', byZoom(['*', style.width, 0.3], style.width));
      map.setLayoutProperty(LAYERS.label, 'visibility', overlay && style.numbers ? 'visible' : 'none');
      (map.getSource(SOURCE) as GeoJSONSource).setData(overlay?.geojson ?? EMPTY);
      (map.getSource(SPOTLIGHT.source) as GeoJSONSource).setData(overlay?.spotlight ?? EMPTY);
      (map.getSource(SPOTS.source) as GeoJSONSource).setData(overlay?.spots ?? EMPTY);
    };
    apply();
    map.on('style.load', apply);
    return () => {
      map.off('style.load', apply);
      map.off('idle', apply);
      try {
        for (const id of [SOURCE, SPOTLIGHT.source, SPOTS.source]) (map.getSource(id) as GeoJSONSource | undefined)?.setData(EMPTY);
      } catch {
        /* style mid-reload */
      }
    };
  }, [map, answer]);

  // Bring the photo into view when an answer with outlines opens (not again when the same answer refreshes).
  const hasOverlay = !!answer?.overlay;
  // biome-ignore lint/correctness/useExhaustiveDependencies: the card id decides when to move the map
  useEffect(() => {
    if (!map || !hasOverlay || !bounds) return;
    map.fitBounds(bounds, { padding: panelPadding(), maxZoom: 18, duration: 800 });
  }, [map, answer?.id, hasOverlay, bounds]);

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
    map.on('click', SPOTS.fill, onClick);
    return () => {
      map.off('click', LAYERS.fill, onClick);
      map.off('click', SPOTS.fill, onClick);
      popup?.remove();
    };
  }, [map, answer]);
}

/** A new mix of questions each time the panel opens, and on "Other questions". */
function newSeed() {
  return 1 + Math.floor(Math.random() * 1_000_000);
}

export function DroneCards({
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
  const [seed, setSeed] = useState(newSeed);
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

  const deck = useDroneDeck(layerId, audience, seed);
  const answer = useDroneCardAnswer(layerId, openCard, audience, seed);
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
            onShuffle={() => setSeed(newSeed())}
            onAskSage={onAskSage}
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
        {openCard && answer.data && (
          <AnswerView
            answer={answer.data}
            layerId={layerId}
            onBack={() => setOpenCard(null)}
            onGoTo={(lon, lat) => map?.flyTo({ center: [lon, lat], zoom: 18.5, offset: panelOffset(), duration: 900 })}
            onAskSage={onAskSage}
          />
        )}
      </div>
    </section>
  );
}
