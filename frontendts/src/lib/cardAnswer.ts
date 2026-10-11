// The parts of a card's answer that every card panel shares (the drone photo cards, the tower cards). The server
// writes every word and number in them (src/services/drone_cards.py `_answer`, and the tower cards built the same
// way); src/components/CardAnswer.tsx only lays them out.

/** A question to send to Sage: a short label, and the full prompt with the photo and place named. */
export interface AskSage {
  label: string;
  prompt: string;
}

export interface HowSure {
  level: 'low' | 'medium' | 'high';
  label: string;
  bars: number;
  because: string[];
  surer: string | null;
}

/** A place to go first, with a point to fly to. The point is null when the place is not known (a tower photo with no
 * GPS position): such an item is never flown to, since a missing position is not 0, 0. */
export interface AnswerItem {
  id: string;
  title: string;
  detail: string;
  lon: number | null;
  lat: number | null;
  picture?: string;
}

/** Set while the answer is still being worked out on the server. */
export interface AnswerProgress {
  done: number;
  parts: number;
  minutes_left: number | null;
}

export interface AnswerTerm {
  id: string;
  word: string;
  simple: string;
  why: string;
}

export interface AnswerDownload {
  label: string;
  href: string;
}

/** What every answer says: what and where, why it matters, what to do, how sure, with its facts and places. */
export interface CardAnswerCore {
  what: string;
  why: string;
  todo: string;
  how_sure: HowSure;
  facts: { label: string; value: string }[];
  items: AnswerItem[];
  progress: AnswerProgress | null;
}
