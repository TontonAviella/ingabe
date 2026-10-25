# Sage writing eval

Does a new answer style explain better without losing a fact? `scripts/eval_sage_writing.py` (docstring has the
commands) writes each case's final answer with the live system prompt, only its `<AnswerStyle>` swapped
(`styles.py`), then a blind judge (same model, random order, told today's date and Sage's citation rules) compares
pairs, and code checks every number against the data.

`cases.jsonl`: 12 real questions from Roger's own projects (Agriculture only) with the real tool results Sage got.
Case w09 was dropped: in the real turn Sage went on to call a rain tool, so it cannot be answered from its saved data.
Runs and verdicts are in `runs/`; the results page is the Sage Writing Lab artifact.

## Result that shipped (2026-10-26)

`runs/run6.jsonl` + `runs/judged6.jsonl`: version 4 (now Sage's `<AnswerStyle>`) against the previous style, 12 cases,
5 answers each, Claude Haiku 5.5 writing and judging. Version 4: 8% shorter (median 126 vs 137 words), 17% fewer
technical words (1.18 vs 1.43 per answer), every number found in the data (100% both), overreach equal (30 vs 29 of
59 pairs); clearer in 58% of pairs (95% range 45-69%), so no proven gain in understanding. Shipped as a modest gain.

Lessons: adding style rules made answers longer and claim more than the data; subtracting worked. Caveats Sage must
give set a floor on length. Earlier attempts (1-5) swapped the style from the wrong place and ran without Sage's core
rules; `system_prompt()` now asserts that only the block changes. To compare a new style: add it to `styles.py`, run
`gen --styles <new>` and judge it against `current`.
