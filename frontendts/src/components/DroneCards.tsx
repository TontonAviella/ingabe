import { Check, ChevronDown, ChevronLeft, FileUp, LoaderCircle, Shuffle } from 'lucide-react';
import { type ExpressionSpecification, type GeoJSONSource, type MapLayerMouseEvent, type Map as MLMap, Popup } from 'maplibre-gl';
import { useEffect, useMemo, useRef, useState } from 'react';
import { AskSageBox } from '@/components/AskSageBox';
import {
  AnswerBody,
  AnswerDownloads,
  AnswerTerms,
  AskSageChips,
  CardButton,
  CardsPanel,
  panelOffset,
  panelPadding,
  readCollapsed,
  StatusIcon,
  writeCollapsed,
} from '@/components/CardAnswer';
import { useReaderRole } from '@/components/ReportAudienceSelect';
import { rememberAsked, useOthersAsked } from '@/hooks/useAskedQuestions';
import {
  type DroneCard,
  type DroneCardAnswer,
  type DroneDeck,
  useAddFarmRecord,
  useCheckPlot,
  useChoosePlotSource,
  useDroneCardAnswer,
  useDroneDeck,
} from '@/hooks/useDroneCards';
import type { MapLayer } from '@/lib/types';

// The question cards for a drone photo, over the map. Everything shown comes
// from the server (src/services/drone_cards.py); this file only lays it out
// and draws the answer's outlines on the photo.

const COLLAPSED_KEY = 'ingabe.droneCards.collapsed';

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

function StatusLine({ card }: { card: DroneCard }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-[12px] font-semibold text-[#D8CCBF]">
      {card.new && (
        <span className="rounded-full bg-[#D9A066] px-1.5 py-px text-[10.5px] font-bold uppercase tracking-[0.04em] text-[#17110E]">
          New
        </span>
      )}
      <StatusIcon status={card.status} />
      {card.status_label}
    </span>
  );
}

function DroneCardButton({ card, onOpen }: { card: DroneCard; onOpen: (id: string) => void }) {
  return <CardButton status={<StatusLine card={card} />} title={card.question} preview={card.preview} onClick={() => onOpen(card.id)} />;
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

// The crops offered first when checking a plot; the rest open behind "Other crops".
const COMMON_CHECK_CROPS = ['maize', 'cassava', 'beans', 'banana', 'irish_potato', 'sweet_potato', 'fallow_or_bare'];

function FieldCheck({
  layerId,
  check,
  onGoTo,
}: {
  layerId: string;
  check: NonNullable<DroneCardAnswer['field_check']>;
  onGoTo: (lon: number, lat: number) => void;
}) {
  const save = useCheckPlot(layerId);
  const [more, setMore] = useState<number | null>(null);
  const common = check.options.filter((o) => COMMON_CHECK_CROPS.includes(o.id));
  const rest = check.options.filter((o) => !COMMON_CHECK_CROPS.includes(o.id));
  return (
    <div className="flex flex-col gap-2 rounded-[18px] bg-[#1E1612] border border-[#D9A066]/30 p-4">
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-[15px] font-bold text-[#F3EDE6]">{check.title}</span>
        {check.checked > 0 && <span className="text-[12px] text-[#B8A99B] tabular-nums">{check.checked} checked</span>}
      </div>
      <p className="m-0 text-[13px] leading-snug text-[#B8A99B]">{check.detail}</p>
      {check.plots.map((plot) => {
        const busy = save.isPending && save.variables?.number === plot.number;
        return (
          <div key={plot.id} className="flex flex-col gap-2 pt-3 border-t border-white/[0.07] first-of-type:border-t-0">
            <button type="button" onClick={() => onGoTo(plot.lon, plot.lat)} className="flex items-center gap-3 text-left cursor-pointer">
              {plot.picture && (
                <img
                  src={plot.picture}
                  alt={`${plot.title} on the photo`}
                  loading="lazy"
                  className="size-14 shrink-0 rounded-[10px] object-cover border border-white/10 bg-[#221813]"
                />
              )}
              <span className="flex flex-col min-w-0">
                <span className="text-[15px] font-semibold text-[#F3EDE6]">{plot.title}</span>
                <span className="text-[13px] leading-snug text-[#B8A99B]">{plot.detail}</span>
              </span>
            </button>
            <div className="flex flex-wrap gap-1.5" role="group" aria-label={`What grows in ${plot.title}`}>
              {(more === plot.number ? [...common, ...rest] : common).map((option) => (
                <button
                  key={option.id}
                  type="button"
                  disabled={save.isPending}
                  onClick={() => save.mutate({ href: check.href, number: plot.number, crop: option.id })}
                  className="min-h-9 px-3 rounded-full text-[13px] font-semibold bg-white/[0.06] text-[#F3EDE6] border border-white/10 hover:bg-[#D9A066] hover:text-[#140E0B] disabled:opacity-60 cursor-pointer"
                >
                  {option.label}
                </button>
              ))}
              {more !== plot.number && rest.length > 0 && (
                <button
                  type="button"
                  onClick={() => setMore(plot.number)}
                  className="min-h-9 px-3 rounded-full text-[13px] font-semibold text-[#D9A066] hover:bg-white/5 cursor-pointer"
                >
                  Other crops
                </button>
              )}
              {busy && <LoaderCircle className="size-4 self-center text-[#D9A066] animate-spin" />}
            </div>
          </div>
        );
      })}
      {save.error && <p className="m-0 text-[13px] text-[#E9B987]">{save.error.message}</p>}
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
  const style = answer.overlay ? OVERLAY_STYLE[answer.overlay.kind] : null;
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

      <AnswerBody answer={answer} onGoTo={onGoTo} />

      {answer.field_check && <FieldCheck layerId={layerId} check={answer.field_check} onGoTo={onGoTo} />}

      {answer.upload && <AddDocument layerId={layerId} upload={answer.upload} />}

      <AskSageChips asks={answer.ask_sage} onAsk={onAskSage} label="Ask Sage for more" />

      <AnswerDownloads downloads={answer.downloads} />

      <AnswerTerms terms={answer.terms} />
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
  const cardsBase = `/api/layer/${encodeURIComponent(layerId)}`;
  const othersAsked = useOthersAsked(cardsBase);
  // A question of the reader's own goes to Sage on the map (the answer shows there) and is kept to learn from.
  const askOwn = (question: string) => {
    rememberAsked(cardsBase, question);
    onAskSage(`About the drone photo "${photo.name}": ${question}`);
  };
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

      <label className="flex items-center gap-2 rounded-[12px] bg-[#221813] px-3 py-1.5">
        <span className="text-[12px] font-semibold text-[#B8A99B]">Questions for</span>
        <select
          value={deck.audience}
          onChange={(e) => onPickAudience(e.target.value)}
          aria-label="Who is reading: the questions are asked for this role"
          className="min-h-8 flex-1 cursor-pointer rounded-[9px] border border-white/10 bg-[#17110E] px-2 text-[13px] font-semibold text-[#F3EDE6] focus:border-[#D9A066]/60 focus:outline-none"
        >
          {deck.audiences.map((a) => (
            <option key={a.id} value={a.id}>
              {a.label}
            </option>
          ))}
        </select>
      </label>

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
          <DroneCardButton key={card.id} card={card} onOpen={onOpen} />
        ))}
      </div>

      <div className="flex flex-col gap-2.5">
        <AskSageChips asks={deck.ask_sage} onAsk={onAskSage} label="Ask Sage about this photo" />
        <AskSageBox othersAsked={othersAsked.data?.questions ?? []} onAsk={askOwn} />
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

/** Another mix of questions, for "Other questions". The panel opens on the day's set (seed 0: the server picks the
 * day's), so the questions change each day, not on every page load. */
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
  const [seed, setSeed] = useState(0);
  const reader = useReaderRole();
  const photos = useMemo(() => layers.filter((l) => l.type === 'raster' && !hiddenLayerIDs.includes(l.id)), [layers, hiddenLayerIDs]);
  const [layerId, setLayerId] = useState<string | null>(null);
  const [audience, setAudience] = useState<string | null>(null);
  const [openCard, setOpenCard] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState(() => readCollapsed(COLLAPSED_KEY));

  // Follow the photos on the map: keep the chosen one while it is shown, else the first.
  useEffect(() => {
    if (!photos.some((p) => p.id === layerId)) {
      setLayerId(photos[0]?.id ?? null);
      setOpenCard(null);
    }
  }, [photos, layerId]);

  const deck = useDroneDeck(layerId, audience, seed);
  // The answer is asked with the deck's own seed (the day's set when none was picked), so its question reads the same.
  const answer = useDroneCardAnswer(layerId, openCard, audience, deck.data?.seed ?? seed);
  useAnswerOverlay(map, collapsed ? null : (answer.data ?? null), deck.data?.photo.bounds ?? null);

  // A photo the cards cannot read (not a colour photo, not a raster) shows no panel.
  if (!layerId || (deck.error && [400, 404, 422].includes(deck.error.status))) return null;

  const setCollapsedAndSave = (value: boolean) => {
    setCollapsed(value);
    writeCollapsed(COLLAPSED_KEY, value);
  };

  return (
    <CardsPanel
      ariaLabel="Questions for this drone photo"
      heading="Questions"
      hideLabel="Hide the questions"
      collapsed={collapsed}
      collapsedLabel={`Questions${deck.data ? ` · ${deck.data.for_you.length}` : ''}`}
      onCollapsedChange={setCollapsedAndSave}
      historyOpen={historyOpen}
    >
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
          onPickAudience={(role) => {
            setAudience(role);
            reader.save.mutate(role); // the role sticks: on the next photo, visit and in Sage's reports
          }}
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
    </CardsPanel>
  );
}
