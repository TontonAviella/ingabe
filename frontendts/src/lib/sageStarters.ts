// Questions offered on Sage's start screen, so a first-time reader sees what can be asked and can ask it with one
// tap. Each one is a case of the routing test, word for word (evals/sage_routing/cases/starters.jsonl; held by
// src/dependencies/test_sage_starters.py): a question is offered here only while Sage is measured to take it to
// the right tool.

export const FARM_PHOTO_STARTERS = [
  'How healthy are the crops on this photo?',
  'Which plots on this photo have gaps or yellow leaves?',
  'Has it rained enough around this farm this season?',
];

export const FARM_STARTERS = [
  'Has it rained enough here this season?',
  'What is the rain forecast for the next 10 days here?',
  'Is it drier than normal here right now?',
];

export const TOWER_STARTERS = [
  'Which tower photos still need checking?',
  'What equipment is on this tower?',
  'Is strong wind or a storm expected here in the next days?',
];

/** The three questions to start from: by the project's industry and, on a farm, whether a photo is on the map. */
export function sageStarters(farmProject: boolean, photoOnMap: boolean): string[] {
  if (!farmProject) return TOWER_STARTERS;
  return photoOnMap ? FARM_PHOTO_STARTERS : FARM_STARTERS;
}
