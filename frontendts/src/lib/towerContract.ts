import type { AnswerDownload, AnswerTerm, AskSage, CardAnswerCore } from '@/lib/cardAnswer';
import type { Box, Point } from '@/lib/photoView';

// The shapes of the tower inspection API (/api/towers/...), exactly as the shared contract between the backend and
// this frontend gives them. Boxes are [x0, y0, x1, y1] in the ORIGINAL photo's pixels. Every count, status and word
// comes from the server; the screens only show them. All routes answer 404 when towers are switched off (TOWERS) or
// the project is not a telecom project, and the panels then show nothing.

export type TowerPart = 'tower_dish' | 'tower_antenna' | 'tower_radio_unit' | 'maize_seedling' | 'tower_bolt_missing' | 'tower_bolt_loose';

export interface PartLabel {
  id: TowerPart;
  label: string;
  plural: string;
  /** Found by a trained model on every photo by itself (the scan): nobody draws examples first. */
  scanned?: boolean;
}

/** How far the scan of an inspection's photos is (/towers/layer/{id}/scan). */
export interface ScanState {
  running: boolean;
  parts: TowerPart[];
  photos: number;
  scanned: number;
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
  { id: 'tower_bolt_missing', label: 'Missing bolt', plural: 'missing bolts' },
  { id: 'tower_bolt_loose', label: 'Loose bolt', plural: 'loose bolts' },
];

// Who reads the tower questions (src/services/reader_roles.py ASSETS): each is asked its own questions.
export type TowerAudience = 'owner' | 'manager' | 'technician';

export const TOWER_AUDIENCES: { id: TowerAudience; label: string }[] = [
  { id: 'owner', label: 'Owner' },
  { id: 'manager', label: 'Manager' },
  { id: 'technician', label: 'Technician' },
];

export function isTowerAudience(role: string | null | undefined): role is TowerAudience {
  return TOWER_AUDIENCES.some((a) => a.id === role);
}

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
  /** The part's outline (polygons in the photo's pixels), once the server has worked it out; drawn, never counted. */
  outline?: number[][][];
}

/** POST .../photos/{sha256}/trace: the outline of a part the person tapped, to see before keeping it. */
export interface Traced {
  polygons: number[][][];
  box: Box;
}

/** One of the inspection's questions as the photo screen asks it for one photo
 * (GET .../photos/{sha256}/questions?audience=). `lights` names the parts it lights up on the photo. */
export interface PhotoQuestion {
  id: string;
  question: string;
  /** The inspection's status, or "needs_check" when suggestions wait on this photo. */
  status: string;
  status_label: string;
  /** What this photo can tell about the question. */
  on_photo: string;
  /** What would answer it, for a question not answerable yet. */
  needs: string | null;
  lights: 'equipment' | 'defects' | 'antennas' | null;
  /** Its answer changed since this reader last looked. */
  new?: boolean;
}

export interface PhotoQuestions {
  summary: string;
  questions: PhotoQuestion[];
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
  /** What the panel is called: "Tower checks". */
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

/** A close look (the server's close_look): a vision model asked narrow questions, for a person to check. It never
 * counts; "can see: no" means the picture cannot answer it. Boxes are in the photo's own pixels. */
export interface CloseLookWorry {
  id: string;
  can_see: 'yes' | 'partly' | 'no';
  answer: string;
  confidence: 'high' | 'medium' | 'low';
  needs: string;
}

export interface CloseLookFinding {
  kind: string;
  where: string;
  can_see: 'yes' | 'partly' | 'no';
  confidence: 'high' | 'medium' | 'low';
  /** Worth a person going to check. */
  flag: boolean;
}

export interface CloseLookRegion {
  n: number;
  name: string;
  why: string;
  box: Box;
  what_is_shown: string;
  findings: CloseLookFinding[];
  cannot_judge: string[];
}

export interface CloseLookCheck {
  suggestion_id: string;
  part: TowerPart;
  really: string;
  agrees: 'yes' | 'no' | 'unsure';
  damage: string;
  confidence: 'high' | 'medium' | 'low';
}

export type CloseLookStatus = 'ready' | 'working' | 'none' | 'off';

/** A part a close look counted inside the model's boxes: a point on it, traced into an outline when the outline fits
 * the part; otherwise `check` says why it is left for a person (no outline, then). A suggestion, never a count. */
export interface CountedPart {
  type: 'panel_antenna' | 'radio_unit' | 'microwave_dish';
  part: TowerPart;
  point: Point;
  confidence: 'high' | 'medium' | 'low';
  outline: number[][][];
  box: Box;
  check: string | null;
}

export interface CloseLookCount {
  method: string;
  parts: CountedPart[];
  counts: Record<CountedPart['type'], { outlined: number; to_check: number }>;
}

/** GET/POST .../photos/{sha256}/close-look */
export interface PhotoCloseLook {
  status: CloseLookStatus;
  result: { worries: CloseLookWorry[]; regions: CloseLookRegion[]; checks: CloseLookCheck[] } | null;
  /** The parts counted by masks in the model's boxes (null until counted); `counting` while it runs. */
  counted: CloseLookCount | null;
  counting: boolean;
  spent_today_usd: number;
  daily_usd: number;
}

/** GET/POST /api/towers/layer/{layer_id}/close-look: the photos looked at, where they were taken. */
export interface InspectionCloseLooks {
  photos: {
    sha256: string;
    lat: number;
    lon: number;
    filename: string;
    url: string;
    flags: string[];
    seen: { id: string; answer: string }[];
  }[];
  running: boolean;
  /** Small enough to be looked at whole; a larger inspection is looked at photo by photo. */
  whole: boolean;
}

/** GET/POST /api/layer/{layer_id}/close-look: a drone photo's whole field and squares pinned to their places. */
export interface FieldCloseLook {
  status: CloseLookStatus;
  result: {
    place: string;
    field: { land_uses: { what: string; where: string }[]; roads_buildings_water: string; worth_a_look: string[]; cannot_tell: string[] };
    squares: {
      n: number;
      lon: number;
      lat: number;
      cm_per_px: number;
      picture: string;
      crops: { crop: string; where: string; stage: string; confidence: string }[];
      bare_or_gaps: string;
      weeds: string;
      stress_or_disease: string;
      odd: string;
      cannot_tell: string[];
    }[];
  } | null;
}
