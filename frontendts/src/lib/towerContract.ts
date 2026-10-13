import type { AnswerDownload, AnswerTerm, AskSage, CardAnswerCore } from '@/lib/cardAnswer';
import type { Box } from '@/lib/photoView';

// The shapes of the tower inspection API (/api/towers/...), exactly as the shared contract between the backend and
// this frontend gives them. Boxes are [x0, y0, x1, y1] in the ORIGINAL photo's pixels. Every count, status and word
// comes from the server; the screens only show them. All routes answer 404 when towers are switched off (TOWERS) or
// the project is not a telecom project, and the panels then show nothing.

export type TowerPart =
  | 'tower_dish'
  | 'tower_antenna'
  | 'tower_radio_unit'
  | 'maize_seedling'
  | 'power_pylon'
  | 'power_insulator'
  | 'power_nest'
  | 'tower_bolt_missing'
  | 'tower_bolt_loose';

export interface PartLabel {
  id: TowerPart;
  label: string;
  plural: string;
}

/** The parts in the order the screen offers them, with the words a reader knows them by. */
export const TOWER_PARTS: PartLabel[] = [
  { id: 'tower_dish', label: 'Microwave dish', plural: 'microwave dishes' },
  { id: 'tower_antenna', label: 'Panel antenna', plural: 'panel antennas' },
  { id: 'tower_radio_unit', label: 'Radio unit', plural: 'radio units' },
];

/** Every part any industry marks (Agriculture: maize plants). Which ones a photo inspection marks comes from the
 * server (InspectionPhotos.parts). */
export const ALL_PARTS: PartLabel[] = [
  ...TOWER_PARTS,
  { id: 'maize_seedling', label: 'Maize plant', plural: 'maize plants' },
  { id: 'power_pylon', label: 'Pylon', plural: 'pylons' },
  { id: 'power_insulator', label: 'Insulator', plural: 'insulators' },
  { id: 'power_nest', label: 'Bird nest', plural: 'bird nests' },
  { id: 'tower_bolt_missing', label: 'Missing bolt', plural: 'missing bolts' },
  { id: 'tower_bolt_loose', label: 'Loose bolt', plural: 'loose bolts' },
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
  /** The parts marked on this inspection's photos, from its project's industry, in the order to offer them. */
  parts?: PartLabel[];
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
  parts: Partial<Record<TowerPart, PartWork>>;
}

/** PUT .../marks. `lookalike_marks` are look-alikes the person drew for this part only; this screen draws none. The
 * server gives the finder the other parts' saved marks as look-alikes itself, for the parts measured with them, and
 * refuses any look-alikes (422) for a part measured without them (tower_inspection.lookalike_marks, set_marks). */
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
  /** What the panel is called for this industry: "Tower checks", "Line checks". */
  heading?: string;
}

/** The server's card listing the photos still to check, in the order to work on them (tower_cards `_photos_to_check`,
 * from tower_inspection.photos_to_check); each item is a photo, its id the photo's sha256, and the list is the same
 * for both readers. The screens open photos from this list: they never decide themselves which photo needs a check. */
export const PHOTOS_TO_CHECK_CARD = 'photos_to_check';
export const PHOTOS_TO_CHECK_AUDIENCE: TowerAudience = 'technician';

/** GET /api/towers/layer/{layer_id}/cards/{card_id}?audience= : the drone cards' answer shape. */
export interface TowerCardAnswer extends TowerCard, CardAnswerCore {
  terms?: AnswerTerm[];
  downloads?: AnswerDownload[];
  ask_sage?: AskSage[];
}
