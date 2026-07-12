<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **ingabe** (13879 symbols, 19534 relationships, 282 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `gitnexus_impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `gitnexus_detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `gitnexus_query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `gitnexus_context({name: "symbolName"})`.

## Never Do

- NEVER edit a function, class, or method without first running `gitnexus_impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `gitnexus_rename` which understands the call graph.
- NEVER commit changes without running `gitnexus_detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/ingabe/context` | Codebase overview, check index freshness |
| `gitnexus://repo/ingabe/clusters` | All functional areas |
| `gitnexus://repo/ingabe/processes` | All execution flows |
| `gitnexus://repo/ingabe/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->

## Harness Discipline

- Keep agent instructions as pointers, not long prompt dumps.
- When a repeated Sage/Hermes/Codex workflow appears, read `.claude/skills/sage-harness-upgrade/SKILL.md`.
- Push judgment into skills, execution into deterministic tools/tests/telemetry, and user-facing Sage language into outcomes plus evidence.

## Local-only operations

- Ingabe runs on the local Docker Compose stack. Do not use SSH, rsync, VPS hosts, remote deployment targets, or production Compose overrides.
- `scripts/deploy.sh` is the canonical local build/start/verification command. `scripts/deploy.sh --check-only` verifies the running stack without rebuilding it.
- The packaged local application is `http://localhost:8000`; Vite development is `http://localhost:5173` when started separately from `frontendts`.
- A geospatial workflow is not complete when an algorithm returns. It is complete only after source resolution, data-shape inspection, deterministic planning, execution, output validation, artifact persistence, local delivery verification, and an evidence-backed response.

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. The
skill has multi-step workflows, checklists, and quality gates that produce better
results than an ad-hoc answer. When in doubt, invoke the skill. A false positive is
cheaper than a false negative.

Key routing rules:
- Product ideas, "is this worth building", brainstorming -> invoke /office-hours
- Strategy, scope, "think bigger", "what should we build" -> invoke /plan-ceo-review
- Architecture, "does this design make sense" -> invoke /plan-eng-review
- Design system, brand, "how should this look" -> invoke /design-consultation
- Design review of a plan -> invoke /plan-design-review
- Developer experience of a plan -> invoke /plan-devex-review
- "Review everything", full review pipeline -> invoke /autoplan
- Bugs, errors, "why is this broken", "this doesn't work" -> invoke /investigate
- Test the site, find bugs, "does this work" -> invoke /qa (or /qa-only for report only)
- Code review, check the diff, "look at my changes" -> invoke /review
- Visual polish, design audit, "this looks off" -> invoke /design-review
- Developer experience audit, try onboarding -> invoke /devex-review
- Ship, deploy, create a PR, "send it" -> invoke /ship
- Merge + deploy + verify -> invoke /land-and-deploy
- Configure deployment -> invoke /setup-deploy
- Post-deploy monitoring -> invoke /canary
- Update docs after shipping -> invoke /document-release
- Weekly retro, "how'd we do" -> invoke /retro
- Second opinion, codex review -> invoke /codex
- Safety mode, careful mode, lock it down -> invoke /careful or /guard
- Restrict edits to a directory -> invoke /freeze or /unfreeze
- Upgrade gstack -> invoke /gstack-upgrade
- Save progress, "save my work" -> invoke /context-save
- Resume, restore, "where was I" -> invoke /context-restore
- Security audit, OWASP, "is this secure" -> invoke /cso
- Make a PDF, document, publication -> invoke /make-pdf
- Launch real browser for QA -> invoke /open-gstack-browser
- Import cookies for authenticated testing -> invoke /setup-browser-cookies
- Performance regression, page speed, benchmarks -> invoke /benchmark
- Review what gstack has learned -> invoke /learn
- Tune question sensitivity -> invoke /plan-tune
- Code quality dashboard -> invoke /health
