import type { AnswerDownload, AnswerTerm, AskSage, CardAnswerCore } from '@/lib/cardAnswer';
import type { Box } from '@/lib/photoView';

// The shapes of the tower inspection API (/api/towers/...), exactly as the shared contract between the backend and
// this frontend gives them. Boxes are [x0, y0, x1, y1] in the ORIGINAL photo's pixels. Every count, status and word
// comes from the server; the screens only show them. All routes answer 404 when towers are switched off (TOWERS) or
// the project is not a telecom project, and the panels then show nothing.

export type TowerPart = 'tower_dish' | 'tower_antenna' | 'tower_radio_unit';

/** The parts in the order the screen offers them, with the words a reader knows them by. */
export const TOWER_PARTS: { id: TowerPart; label: string; plural: string }[] = [
  { id: 'tower_dish', label: 'Microwave dish', plural: 'microwave dishes' },
  { id: 'tower_antenna', label: 'Panel antenna', plural: 'panel antennas' },
  { id: 'tower_radio_unit', label: 'Radio unit', plural: 'radio units' },
];

export type TowerAudience = 'owner' | 'technician';

export const TOWER_AUDIENCES: { id: TowerAudience; label: string }[] = [
  { id: 'owner', label: 'Owner' },
  { id: 'technician', label: 'Technician' },
];

/** GET /api/towers/layer/{layer_id}/photos */
export interface InspectionPhoto {
  sha256: string;
  url: string;
  width: number;
  height: number;
  taken_at: string | null;
  lat: number | null;
  lon: number | null;
  relative_altitude_m: number | null;
  gimbal_pitch: number | null;
  gimbal_yaw: number | null;
}

export interface InspectionPhotos {
  photos: InspectionPhoto[];
}

/** A find job of the example finder. The finder's states are queued, running, done and failed. */
export interface FinderJob {
  job_id: string;
  status: string;
}

export interface Suggestion {
  id: string;
  box: Box;
  score: number;
}

/** What the person decided about the suggestions: kept as found, rejected, kept with a corrected box, and missed
 * objects they drew themselves. */
export interface PartReview {
  kept: string[];
  rejected: string[];
  fixed: { id: string; box: Box }[];
  added: Box[];
}

/** One part's work on one photo. `counted_by_you` is the server's count once the person checked every suggestion
 * (null until then); `waiting` is how many suggestions still wait for their check. */
export interface PartWork {
  marks: Box[];
  lookalike_marks: Box[];
  job: FinderJob | null;
  suggestions: Suggestion[];
  reviewed: PartReview;
  counted_by_you: number | null;
  waiting: number;
}

/** GET /api/towers/layer/{layer_id}/photos/{sha256}/work */
export interface PhotoWork {
  parts: Record<TowerPart, PartWork>;
}

/** PUT .../marks */
export interface MarksPayload {
  part: TowerPart;
  marks: Box[];
  lookalike_marks: Box[];
}

/** PUT .../review */
export interface ReviewPayload extends PartReview {
  part: TowerPart;
}

/** GET /api/towers/layer/{layer_id}/cards?audience= */
export interface TowerCard {
  id: string;
  title: string;
  status: string;
  preview: string;
}

export interface TowerCards {
  cards: TowerCard[];
}

/** GET /api/towers/layer/{layer_id}/cards/{card_id}?audience= : the drone cards' answer shape. */
export interface TowerCardAnswer extends TowerCard, CardAnswerCore {
  terms?: AnswerTerm[];
  downloads?: AnswerDownload[];
  ask_sage?: AskSage[];
}
