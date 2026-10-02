---
name: standards-retro
description: "Use after a mistake is found or fixed, at the end of a task that needed correction, or when the user says \"learn from this\", \"don't do that again\", \"add a lesson\", \"standards retro\", or \"why did this slip through\". Turns the mistake into a CODING_STANDARDS.md Lessons log entry and, where possible, a mechanical gate."
---

# Standards Retro

## Principle

A mistake that is fixed but not recorded will be made again. `CODING_STANDARDS.md` is the only home for the rules; this skill keeps its **Lessons log** growing and turns recurring lessons into gates.

## Inputs

- The mistake: what went wrong, where (file, commit, PR, chat turn), and how it was caught.
- `CODING_STANDARDS.md`, `scripts/check_standards.py`, `scripts/standards_baseline.json`.
- `git log` / the diff that fixed it, and any related `CHANGELOG.md` entry.

## Workflow

1. **State the mistake in one sentence**, with the concrete evidence (file:line, commit, failing output). No vague entries.
2. **Find the rule that prevents it.**
   - If an existing rule already covers it, the lesson is about why the rule did not fire — say so.
   - If no rule covers it, write the narrowest rule that would have prevented it. Prefer a rule about the code, not about agent behaviour.
3. **Decide whether it can be gated.**
   - Mechanically checkable (imports, names, literals, duplicated bodies, layer boundaries) → extend `scripts/check_standards.py` with a new check or a wider keyword list, and add a test in `tests/test_check_standards.py` that fails on the bad pattern and passes on the good one.
   - Not checkable → say why in the lesson, so review knows to look for it.
4. **Write the Lessons log entry** at the top of the log (newest first), three lines max:
   `- **YYYY-MM-DD** What went wrong. Rule: <the rule>. Gate: <check id or "review only: <why>">.`
5. **Promote recurring lessons.** If the same lesson appears twice, move the rule into the relevant section above the log (Design principles, Hard invariants, or How to work) and keep the log entry pointing at it.
6. **Verify.**
   - `python scripts/check_standards.py` passes (a new check may surface existing debt: fix it, or — only for debt that existed before the rule — list it in the baseline in the same commit and say so in the commit message).
   - `python -m pytest tests/test_check_standards.py --noconftest -q` passes.

## Guardrails

- Never copy rules into `CLAUDE.md`, `AGENTS.md`, skills, or prompts; point to `CODING_STANDARDS.md`.
- Never run `check_standards.py --write-baseline` to make a new violation go away. The baseline only shrinks.
- One lesson per real mistake. Do not pad the log with hypotheticals.

## Output

The diff to `CODING_STANDARDS.md` (and to the gate + its test when gated), plus one line telling the user which rule now prevents the mistake and whether it is enforced or review-only.
