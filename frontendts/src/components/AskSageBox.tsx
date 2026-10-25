import { MessageCircle, Send, Users } from 'lucide-react';
import { useState } from 'react';

// Ask Sage your own question about a photo, or one of the questions others asked (src/services/asked_questions.py).

export const ASK_CHARS = 300;

export function AskSageBox({
  othersAsked,
  onAsk,
  busy = false,
  placeholder = 'Ask Sage your own question…',
}: {
  othersAsked: string[];
  onAsk: (question: string) => void;
  /** Sage is still answering the last question. */
  busy?: boolean;
  placeholder?: string;
}) {
  const [text, setText] = useState('');
  const ask = (question: string) => {
    const q = question.trim();
    if (!q || busy) return;
    onAsk(q);
    setText('');
  };
  return (
    <div className="flex flex-col gap-2">
      {othersAsked.length > 0 && (
        <div className="flex flex-col gap-1.5">
          <span className="flex items-center gap-1.5 text-[13px] font-semibold text-[#B8A99B]">
            <Users className="size-3.5" /> Others asked
          </span>
          {othersAsked.map((question) => (
            <button
              key={question}
              type="button"
              disabled={busy}
              onClick={() => ask(question)}
              className="flex items-center gap-2.5 min-h-11 px-3.5 rounded-[14px] text-left text-[14px] font-semibold text-[#F3EDE6] bg-[#D9A066]/[0.1] border border-[#D9A066]/35 hover:bg-[#D9A066]/[0.18] disabled:opacity-50 cursor-pointer disabled:cursor-default"
            >
              <MessageCircle className="size-4 shrink-0 text-[#D9A066]" />
              <span className="leading-snug">{question}</span>
            </button>
          ))}
        </div>
      )}
      <form
        className="flex items-center gap-2 rounded-[16px] border border-white/10 bg-white/[0.04] pl-3.5 pr-1.5 focus-within:border-[#D9A066]/60"
        onSubmit={(e) => {
          e.preventDefault();
          ask(text);
        }}
      >
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          maxLength={ASK_CHARS}
          placeholder={placeholder}
          aria-label="Ask Sage your own question"
          className="min-h-11 flex-1 bg-transparent text-[14px] text-[#F3EDE6] placeholder:text-[#8E7F73] outline-none"
        />
        <button
          type="submit"
          aria-label="Ask Sage"
          disabled={busy || !text.trim()}
          className="flex size-9 items-center justify-center rounded-full bg-[#D9A066] text-[#17110E] hover:bg-[#E9B987] disabled:opacity-40 cursor-pointer disabled:cursor-default"
        >
          <Send className="size-4" />
        </button>
      </form>
    </div>
  );
}
