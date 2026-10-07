import { ChevronDown, ChevronUp, LoaderCircle, Sparkles, X } from 'lucide-react';
import type { ReactNode } from 'react';
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { Components } from 'react-markdown';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { splitSources } from '@/lib/sageReplyText';

// Sage's reply over the map, in the question cards' look (night and chocolate glass, caramel accent, the
// system font). Sage writes a bold first line, a few short points and a "Sources:" line (see AnswerStyle in
// src/dependencies/system_prompt.py); this lays them out and turns the sources into small chips.

export interface SageError {
  id: string;
  message: string;
  timestamp: Date;
}

const MARKDOWN: Components = {
  p: ({ children }) => <p className="m-0 mb-2.5 last:mb-0">{children}</p>,
  strong: ({ children }) => <strong className="font-semibold text-[#F3EDE6]">{children}</strong>,
  em: ({ children }) => <em className="not-italic text-[#D8CCBF]">{children}</em>,
  ul: ({ children }) => <ul className="m-0 mb-2.5 p-0 list-none flex flex-col gap-1.5 last:mb-0">{children}</ul>,
  ol: ({ children }) => <ol className="m-0 mb-2.5 pl-5 flex flex-col gap-1.5 marker:text-[#D9A066] last:mb-0">{children}</ol>,
  li: ({ children }) => (
    <li className="relative pl-4 [ul>&]:before:absolute [ul>&]:before:left-0 [ul>&]:before:top-[0.62em] [ul>&]:before:size-1.5 [ul>&]:before:rounded-full [ul>&]:before:bg-[#D9A066]">
      {children}
    </li>
  ),
  a: ({ children, href }) => (
    <a href={href} target="_blank" rel="noreferrer" className="text-[#E9B987] underline decoration-[#D9A066]/50 underline-offset-2">
      {children}
    </a>
  ),
  h1: ({ children }) => <p className="m-0 mt-1 mb-1.5 text-[11px] font-semibold uppercase tracking-[0.07em] text-[#D9A066]">{children}</p>,
  h2: ({ children }) => <p className="m-0 mt-1 mb-1.5 text-[11px] font-semibold uppercase tracking-[0.07em] text-[#D9A066]">{children}</p>,
  h3: ({ children }) => <p className="m-0 mt-1 mb-1.5 text-[11px] font-semibold uppercase tracking-[0.07em] text-[#D9A066]">{children}</p>,
  code: ({ children }) => <code className="rounded-[6px] bg-white/[0.08] px-1.5 py-0.5 text-[13px]">{children}</code>,
  table: ({ children }) => (
    <div className="mb-2.5 overflow-x-auto rounded-[12px] border border-white/[0.08]">
      <table className="w-full border-collapse text-left text-[13px] tabular-nums">{children}</table>
    </div>
  ),
  th: ({ children }) => (
    <th className="border-b border-white/[0.08] bg-white/[0.04] px-2.5 py-1.5 font-semibold text-[#B8A99B]">{children}</th>
  ),
  td: ({ children }) => <td className="border-b border-white/[0.05] px-2.5 py-1.5 align-top">{children}</td>,
  img: ({ src, alt }) => (
    <img src={src} alt={alt ?? ''} className="my-2 block h-auto w-full max-w-[320px] rounded-[12px] border border-white/10" />
  ),
};

/** The reply's markdown: the first line larger, the points with caramel dots. */
function Answer({ text, streaming }: { text: string; streaming?: boolean }) {
  return (
    <div className="text-[15px] leading-[1.5] text-[#E8DED4] [&>p:first-child]:text-[16.5px] [&>p:first-child]:leading-snug [&>p:first-child]:text-[#F3EDE6] [&>p:first-child]:font-semibold">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={MARKDOWN}>
        {text}
      </ReactMarkdown>
      {streaming && <span className="ml-0.5 inline-block h-4 w-1.5 animate-pulse rounded-full bg-[#D9A066] align-text-bottom" />}
    </div>
  );
}

function Sources({ sources }: { sources: string[] }) {
  if (sources.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5 border-t border-white/[0.06] px-4 py-2.5">
      <span className="mr-0.5 text-[11px] font-semibold uppercase tracking-[0.07em] text-[#8E7F73]">Sources</span>
      {sources.map((source) => (
        <span key={source} className="rounded-full border border-white/10 bg-white/[0.05] px-2.5 py-0.5 text-[12px] text-[#D8CCBF]">
          {source}
        </span>
      ))}
    </div>
  );
}

export function SageReply({
  reply,
  streamingText,
  actions,
  errors,
  cancelling,
  onCancel,
  onDismissError,
  actionIcon,
}: {
  reply?: string;
  streamingText?: string;
  actions: { action_id: string; action: string }[];
  errors: SageError[];
  cancelling: boolean;
  onCancel: () => void;
  onDismissError: (id: string) => void;
  actionIcon: (action: string) => ReactNode;
}) {
  const [expanded, setExpanded] = useState(false);
  const [overflows, setOverflows] = useState(false);
  const [hidden, setHidden] = useState(false);
  const body = useRef<HTMLDivElement>(null);
  const working = !!streamingText || actions.length > 0;
  // While Sage works, the last answer gives way to what it is doing (it would read as the new answer).
  const text = streamingText || (working ? '' : reply || '');
  const { body: answer, sources } = streamingText ? { body: streamingText, sources: [] } : splitSources(text);

  // A new reply opens folded, and shows again if the last one was closed.
  // biome-ignore lint/correctness/useExhaustiveDependencies: a new reply is what resets the panel
  useEffect(() => {
    setExpanded(false);
    setHidden(false);
  }, [reply]);
  // biome-ignore lint/correctness/useExhaustiveDependencies: measure again when the text or the height changes
  useLayoutEffect(() => {
    const el = body.current;
    setOverflows(!!el && el.scrollHeight > el.clientHeight + 4);
  }, [text, expanded]);

  if (hidden && !working && errors.length === 0) return null;
  if (!text && !working && errors.length === 0) return null;

  return (
    <section
      aria-label="Sage's answer"
      aria-live="polite"
      className="z-30 absolute bottom-[100px] left-1/2 xl:left-3/5 -translate-x-1/2 w-[calc(100%-1rem)] sm:w-4/5 max-w-[34rem] flex flex-col overflow-hidden rounded-[22px] border border-white/[0.08] bg-[#17110E]/95 text-[#F3EDE6] shadow-[0_18px_48px_rgba(0,0,0,0.5)] backdrop-blur-xl"
      style={{ fontFamily: "-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Helvetica Neue', system-ui, sans-serif" }}
    >
      <header className="flex items-center justify-between gap-3 px-4 pt-3 pb-1.5">
        <span className="flex items-center gap-2">
          <span className="flex size-6 items-center justify-center rounded-full bg-[#D9A066]/15">
            <Sparkles className="size-3.5 text-[#D9A066]" />
          </span>
          <span className="text-[11px] font-semibold uppercase tracking-[0.08em] text-[#D9A066]">Sage</span>
          {working && <span className="text-[12px] text-[#B8A99B]">{streamingText ? 'writing…' : 'working…'}</span>}
        </span>
        <span className="flex items-center gap-1">
          {working &&
            (cancelling ? (
              <span className="text-[12px] text-[#B8A99B]">Stopping…</span>
            ) : (
              <button
                type="button"
                onClick={onCancel}
                className="min-h-8 rounded-full px-3 text-[12px] font-semibold text-[#E9B987] hover:bg-white/[0.06] cursor-pointer"
              >
                Stop
              </button>
            ))}
          {!working && text && (
            <button
              type="button"
              onClick={() => setHidden(true)}
              aria-label="Close the answer"
              className="flex size-8 items-center justify-center rounded-full text-[#B8A99B] hover:bg-white/[0.06] hover:text-[#F3EDE6] cursor-pointer"
            >
              <X className="size-4" />
            </button>
          )}
        </span>
      </header>

      {errors.map((error) => (
        <div
          key={error.id}
          className="mx-4 mb-2 flex items-start justify-between gap-3 rounded-[14px] border border-[#C2410C]/40 bg-[#C2410C]/10 px-3 py-2"
        >
          <span className="text-[14px] leading-snug text-[#F3C9B4]">{error.message}</span>
          <button
            type="button"
            onClick={() => onDismissError(error.id)}
            className="shrink-0 text-[12px] font-semibold text-[#E9B987] hover:underline cursor-pointer"
          >
            Dismiss
          </button>
        </div>
      ))}

      {!streamingText && actions.length > 0 && (
        <ol className="m-0 mx-4 mb-3 flex list-none flex-col gap-1.5 p-0">
          {actions.map((action) => (
            <li key={action.action_id} className="flex items-center text-[14px] text-[#D8CCBF] [&_svg]:text-[#D9A066]">
              {actionIcon(action.action)}
              <span>{action.action}</span>
            </li>
          ))}
        </ol>
      )}

      {text && (
        <div className="relative">
          <div ref={body} className={`overflow-y-auto px-4 pb-3 ${expanded ? 'max-h-[60vh]' : 'max-h-[30vh]'}`}>
            <Answer text={answer} streaming={!!streamingText} />
          </div>
          {overflows && !expanded && (
            <div className="pointer-events-none absolute inset-x-0 bottom-0 h-10 bg-gradient-to-t from-[#17110E] to-transparent" />
          )}
        </div>
      )}
      {text && (overflows || expanded) && !streamingText && (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="mx-4 mb-2 self-start inline-flex items-center gap-1 rounded-full px-2 py-1 text-[12px] font-semibold text-[#D9A066] hover:bg-white/[0.05] cursor-pointer"
        >
          {expanded ? <ChevronUp className="size-3.5" /> : <ChevronDown className="size-3.5" />}
          {expanded ? 'Show less' : 'Show all'}
        </button>
      )}
      {!streamingText && <Sources sources={sources} />}
      {working && !text && actions.length === 0 && (
        <div className="flex items-center gap-2 px-4 pb-3 text-[14px] text-[#B8A99B]">
          <LoaderCircle className="size-4 animate-spin text-[#D9A066]" /> Thinking…
        </div>
      )}
    </section>
  );
}
