import {
  BookOpen,
  Camera,
  Check,
  ChevronRight,
  CircleDashed,
  Download,
  EyeOff,
  Hammer,
  LoaderCircle,
  MessageCircle,
  Plane,
  X,
} from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import type { AnswerDownload, AnswerItem, AnswerProgress, AnswerTerm, AskSage, CardAnswerCore } from '@/lib/cardAnswer';

// The pieces every card panel over the map shares: the drone photo cards (DroneCards.tsx) and the tower cards
// (towers/TowerCards.tsx). Everything shown comes from the server; these only lay it out.

export const PANEL_FONT = "-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Helvetica Neue', system-ui, sans-serif";

// On phones the panel is a sheet over the lower part of the map, above the chat box. On larger screens it
// stops 280px above the bottom: the chat box and Sage's reply above it take about 260px there.
const PHONE_SHEET_SHARE = 0.52;

/** Room the panel takes on the map, so what an answer points at stays in view beside or above it. */
export function panelPadding() {
  return window.innerWidth >= 640
    ? { top: 70, bottom: 170, left: 60, right: 440 }
    : { top: 70, bottom: Math.round(window.innerHeight * PHONE_SHEET_SHARE) + 110, left: 20, right: 20 };
}

/** Shift that puts a point in the middle of the free part of the map. An offset, not `padding`: padding given to
 * flyTo stays on the map and adds to the next fitBounds, which then cannot fit on a phone and does nothing. */
export function panelOffset(): [number, number] {
  const p = panelPadding();
  return [(p.left - p.right) / 2, (p.top - p.bottom) / 2];
}

/** The icon for a card's status; null for a status this panel has no icon for. */
export function StatusIcon({ status }: { status: string }) {
  switch (status) {
    case 'ready':
      return <Check className="size-3.5 text-[#D9A066]" strokeWidth={2.5} />;
    case 'partly':
      return <CircleDashed className="size-3.5 text-[#D9A066]" strokeWidth={2.5} />;
    case 'needs_flight':
      return <Plane className="size-3.5 text-[#B8A99B]" strokeWidth={2.2} />;
    case 'needs_camera':
      return <Camera className="size-3.5 text-[#B8A99B]" strokeWidth={2.2} />;
    case 'cant_tell':
      return <EyeOff className="size-3.5 text-[#B8A99B]" strokeWidth={2.2} />;
    case 'to_build':
      return <Hammer className="size-3.5 text-[#8C7B6E]" strokeWidth={2.2} />;
    case 'learn':
      return <BookOpen className="size-3.5 text-[#D9A066]" strokeWidth={2.2} />;
    case 'working':
      return <LoaderCircle className="size-3.5 text-[#D9A066] animate-spin" strokeWidth={2.5} />;
    default:
      return null;
  }
}

/** One card of a deck: its status line, its question and a one-line preview of the answer. */
export function CardButton({
  status,
  title,
  preview,
  onClick,
}: {
  status: React.ReactNode;
  title: string;
  preview: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="w-full text-left flex flex-col gap-1.5 rounded-[18px] bg-[#221813] border border-white/[0.07] px-4 py-3.5 hover:border-[#D9A066]/50 focus-visible:outline-2 focus-visible:outline-[#D9A066] transition-colors cursor-pointer"
    >
      {status}
      <span className="text-[17px] font-bold leading-snug tracking-[-0.01em] text-[#F3EDE6]">{title}</span>
      <span className="text-[14px] leading-snug text-[#B8A99B]">{preview}</span>
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

export function AskSageChips({ asks, onAsk, label }: { asks: AskSage[]; onAsk: (prompt: string) => void; label: string }) {
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

function Progress({ progress }: { progress: AnswerProgress }) {
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

/** What tapping a place does: the panel's own action when it has one for the item (a tower photo opens on the marking
 * screen), else flying to the item's position. An item with neither (no position: never flown to 0, 0) has none. */
function itemAction(
  item: AnswerItem,
  onGoTo: (lon: number, lat: number) => void,
  openItem?: (item: AnswerItem) => (() => void) | null,
): (() => void) | null {
  const open = openItem?.(item);
  if (open) return open;
  const { lon, lat } = item;
  return lon !== null && lat !== null ? () => onGoTo(lon, lat) : null;
}

function ItemRow({ item }: { item: AnswerItem }) {
  return (
    <>
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
    </>
  );
}

/** What and where (with its places and facts), why it matters, what to do and how sure: the body of every answer.
 * `openItem` gives a panel's own action for a place (see itemAction). */
export function AnswerBody({
  answer,
  onGoTo,
  openItem,
}: {
  answer: CardAnswerCore;
  onGoTo: (lon: number, lat: number) => void;
  openItem?: (item: AnswerItem) => (() => void) | null;
}) {
  return (
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
          {answer.items.map((item) => {
            // One photo can be listed twice (once per part), so the id alone is not a key.
            const key = `${item.id} ${item.detail}`;
            const action = itemAction(item, onGoTo, openItem);
            return action ? (
              <button
                key={key}
                type="button"
                onClick={action}
                className="flex items-center justify-between gap-3 min-h-11 px-4 py-1.5 text-left hover:bg-white/[0.04] cursor-pointer"
              >
                <ItemRow item={item} />
                <ChevronRight className="size-4 shrink-0 text-[#B8A99B]" />
              </button>
            ) : (
              <div key={key} className="flex items-center justify-between gap-3 min-h-11 px-4 py-1.5">
                <ItemRow item={item} />
              </div>
            );
          })}
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
        {answer.how_sure.surer && <p className="m-0 mt-1 text-[13px] leading-snug text-[#E9B987]">To be surer: {answer.how_sure.surer}</p>}
      </Section>
    </div>
  );
}

export function AnswerDownloads({ downloads }: { downloads: AnswerDownload[] }) {
  if (downloads.length === 0) return null;
  return (
    <div className="flex flex-col gap-2">
      <span className="text-[13px] font-semibold text-[#B8A99B]">Take it with you</span>
      <div className="flex flex-wrap gap-2">
        {downloads.map((d) => (
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
  );
}

/** The words an answer uses, each opening a plain explanation. */
export function AnswerTerms({ terms }: { terms: AnswerTerm[] }) {
  const [openTerm, setOpenTerm] = useState<string | null>(null);
  const termRef = useRef<HTMLDivElement>(null);
  const term = terms.find((t) => t.id === openTerm);
  useEffect(() => {
    if (openTerm) termRef.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }, [openTerm]);
  if (terms.length === 0) return null;
  return (
    <div className="flex flex-col gap-2">
      <span className="text-[13px] font-semibold text-[#B8A99B]">Words in this answer</span>
      <div className="flex flex-wrap gap-2">
        {terms.map((t) => (
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
  );
}

/** Whether a panel was last left hidden (per panel, in this browser). */
export function readCollapsed(key: string): boolean {
  try {
    return localStorage.getItem(key) === '1';
  } catch {
    return false;
  }
}

export function writeCollapsed(key: string, collapsed: boolean) {
  try {
    localStorage.setItem(key, collapsed ? '1' : '0');
  } catch {
    /* private window: the choice holds for this session */
  }
}

/** A question the hidden panel shows on its button, so the reader sees what is waiting before opening it. */
export type PanelTeaser = { id: string; text: string };

const TEASER_SECONDS = 6;
const TEASER_ROUNDS = 3;

/** The button that brings a hidden panel back. It says what is waiting (collapsedLabel) and shows one real question at
 * a time (teasers), changing every few seconds for a few rounds and then holding still, so it catches the eye when the
 * map opens without nagging all day. A click opens the question it shows. */
function CollapsedPanelButton({
  label,
  teasers,
  onTeaser,
  onOpen,
  className,
}: {
  label: string;
  teasers: PanelTeaser[];
  onTeaser?: (id: string) => void;
  onOpen: () => void;
  className: string;
}) {
  const [shown, setShown] = useState(0);
  const [paused, setPaused] = useState(false);
  const rounds = teasers.length > 1 ? teasers.length * TEASER_ROUNDS : 0;

  useEffect(() => {
    if (paused || shown >= rounds - 1) return;
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return;
    const timer = window.setTimeout(() => setShown((n) => n + 1), TEASER_SECONDS * 1000);
    return () => window.clearTimeout(timer);
  }, [paused, shown, rounds]);

  const teaser = teasers.length ? teasers[shown % teasers.length] : null;
  return (
    <button
      type="button"
      onClick={() => (teaser && onTeaser ? onTeaser(teaser.id) : onOpen())}
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      onFocus={() => setPaused(true)}
      onBlur={() => setPaused(false)}
      aria-label={teaser ? `${label}: ${teaser.text}` : label}
      style={{ fontFamily: PANEL_FONT }}
      className={`${className} group flex items-center gap-3 max-w-[min(340px,calc(100%-2rem))] min-h-11 py-2 pl-3.5 pr-2.5 rounded-[20px] bg-[#110C0A]/90 backdrop-blur-xl border border-[#D9A066]/40 text-left text-[#F3EDE6] shadow-[0_10px_28px_rgba(0,0,0,0.45)] hover:border-[#D9A066]/80 cursor-pointer`}
    >
      <span className="relative flex size-2.5 shrink-0" aria-hidden="true">
        <span className="absolute inline-flex size-full rounded-full bg-[#D9A066] opacity-70 motion-safe:[animation:ping_1s_cubic-bezier(0,0,0.2,1)_3_forwards]" />
        <span className="relative inline-flex size-2.5 rounded-full bg-[#D9A066]" />
      </span>
      <span className="flex min-w-0 flex-col">
        <span className="text-[11.5px] font-bold uppercase tracking-[0.07em] text-[#D9A066]">{label}</span>
        {teaser && (
          <span
            key={teaser.id}
            className="line-clamp-2 text-[14px] font-semibold leading-snug motion-safe:animate-in motion-safe:fade-in motion-safe:duration-500"
          >
            {teaser.text}
          </span>
        )}
      </span>
      <ChevronRight className="size-4 shrink-0 text-[#D9A066] transition-transform group-hover:translate-x-0.5" aria-hidden="true" />
    </button>
  );
}

/** The panel over the map that holds a deck of cards, or the button that brings it back when hidden. */
export function CardsPanel({
  ariaLabel,
  heading,
  hideLabel,
  collapsed,
  collapsedLabel,
  teasers = [],
  onTeaser,
  onCollapsedChange,
  historyOpen,
  belowChatsButton = false,
  children,
}: {
  ariaLabel: string;
  heading: string;
  hideLabel: string;
  collapsed: boolean;
  /** What is waiting, on the button while the panel is hidden ("4 answers ready"). */
  collapsedLabel: string;
  /** Questions the hidden panel's button shows one at a time; clicking it calls onTeaser with the one shown. */
  teasers?: PanelTeaser[];
  onTeaser?: (id: string) => void;
  onCollapsedChange: (collapsed: boolean) => void;
  /** "Previous chats" is open; below xl it covers the right side of the map, so the panel steps aside. */
  historyOpen: boolean;
  /** The chats panel is hidden and its "Chats" button holds the top-right corner on wide screens, so the panel sits
   * below it there, as it does on narrow screens (below the chat-history button). */
  belowChatsButton?: boolean;
  children: React.ReactNode;
}) {
  // Full class names, so Tailwind finds them in the source.
  const buttonTop = belowChatsButton ? 'top-16' : 'top-16 xl:top-4';
  const panelTop = belowChatsButton ? 'sm:top-16' : 'sm:top-16 xl:top-4 xl:max-h-[calc(100%-280px)]';
  if (collapsed) {
    return (
      <CollapsedPanelButton
        label={collapsedLabel}
        teasers={teasers}
        onTeaser={onTeaser}
        onOpen={() => onCollapsedChange(false)}
        className={`absolute ${buttonTop} right-4 sm:right-14 z-30 ${historyOpen ? 'max-xl:hidden' : ''}`}
      />
    );
  }
  return (
    <section
      aria-label={ariaLabel}
      style={{ fontFamily: PANEL_FONT }}
      className={`absolute z-30 inset-x-2 bottom-[100px] max-h-[52vh] sm:inset-auto sm:bottom-auto ${panelTop} sm:right-14 sm:w-[372px] sm:max-h-[calc(100%-328px)] flex flex-col rounded-[26px] bg-[#110C0A]/[0.9] backdrop-blur-2xl border border-white/[0.09] shadow-2xl text-[#F3EDE6] ${historyOpen ? 'max-xl:hidden' : ''}`}
    >
      <div className="flex items-center justify-between px-5 pt-4 pb-1">
        <span className="text-[12px] font-semibold uppercase tracking-[0.08em] text-[#D9A066]">{heading}</span>
        <button
          type="button"
          aria-label={hideLabel}
          onClick={() => onCollapsedChange(true)}
          className="flex size-8 items-center justify-center rounded-full text-[#B8A99B] hover:bg-white/10 hover:text-[#F3EDE6] cursor-pointer"
        >
          <X className="size-4" />
        </button>
      </div>
      <div className="overflow-y-auto px-5 pb-5 pt-1">{children}</div>
    </section>
  );
}
