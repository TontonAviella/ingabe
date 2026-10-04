#!/usr/bin/env python3
"""Coding-standards gate. The rules live in CODING_STANDARDS.md; this script
enforces the ones that can be checked mechanically.

Checks (rule id -> invariant in CODING_STANDARDS.md):

  lazy-import       H1  function-level import of an internal module inside a
                        domain module (src/services/) without `# lazy: <reason>`
  web-import        DC  domain module imports FastAPI, Starlette, src.routes or
                        src.dependencies
  private-import    EN  `from <internal module> import _name` across modules
  render-threshold  H3  business-threshold comparison (NDVI, risk, payout, ...)
                        in a rendering layer (routes, Sage tool handlers,
                        renderer, React components)
  dup-body          H2  function bodies that are identical after normalisation
  caplog            HW  a test takes pytest's `caplog` fixture; `src` loggers stop
                        propagating once the app's lifespan has run, so caplog
                        sees nothing and the test passes or fails by test order
  compose-mem-limit HW  an opt-in (profiled) docker-compose service without
                        mem_limit; the local Docker VM has fixed memory
  agents-md-sync    HW  CLAUDE.md does not just import AGENTS.md (`@AGENTS.md`):
                        agent guidance has one source so Claude Code and Codex
                        never drift apart

Existing debt is listed in scripts/standards_baseline.json. The baseline is a
ratchet:

  * a violation missing from the baseline fails (new debt);
  * a baseline entry that no longer occurs fails (fixed debt must be removed
    from the baseline in the same commit);
  * with --base-ref, the baseline may not contain entries that the base
    branch's baseline does not (it may only shrink).

Usage:
  python scripts/check_standards.py                       # local / push gate
  python scripts/check_standards.py --base-ref origin/main  # pull-request gate
  python scripts/check_standards.py --hook                # Claude Code Stop hook
  python scripts/check_standards.py --write-baseline      # bootstrap only

Stdlib only, so CI needs no dependency install.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = ROOT / "scripts" / "standards_baseline.json"
BASELINE_REL = "scripts/standards_baseline.json"

# Layers (see the table in CODING_STANDARDS.md).
DOMAIN_DIRS = ("src/services",)
RENDER_PY_DIRS = ("src/routes", "src/tools", "src/renderer")
RENDER_TS_DIRS = ("frontendts/src",)
DUP_BODY_DIRS = ("src", "services")
PRIVATE_IMPORT_DIRS = ("src",)

WEB_MODULE_PREFIXES = ("fastapi", "starlette", "src.routes", "src.dependencies")

# Domain terms whose numeric comparison is a business rule (H3). Kept to
# unambiguous agronomy / insurance vocabulary; widen it only with care, and
# remember terms outside this list are invisible to the gate.
BUSINESS_TERMS = (
    "ndvi", "evi", "savi", "ndwi", "ndmi", "ndre", "spi",
    "rainfall", "precip", "payout", "premium", "trigger",
    "risk", "severity", "deficit", "anomaly", "drought",
)
_TERM_RE = re.compile("|".join(BUSINESS_TERMS), re.IGNORECASE)

# Integer literals this common are almost always counts or sentinels, not
# business thresholds (`len(risks) > 0`, `trigger_count >= 1`).
TRIVIAL_LITERALS = {0, 1, -1}

# Function bodies smaller than this are too generic to call duplicates.
DUP_MIN_STATEMENTS = 4

TEST_DIRS = ("src", "tests")
COMPOSE_FILE = "docker-compose.yml"
AGENTS_FILE = "AGENTS.md"
CLAUDE_FILE = "CLAUDE.md"

SKIP_DIR_PARTS = {"node_modules", "__pycache__", "opensrc", "external", ".venv", "dist", "build"}


@dataclass(frozen=True)
class Violation:
    rule: str
    path: str
    line: int
    detail: str
    message: str

    @property
    def key(self) -> str:
        # No line numbers: unrelated edits above a violation must not churn
        # the baseline.
        return f"{self.rule}|{self.path}|{self.detail}"


# --------------------------------------------------------------------------- #
# File discovery
# --------------------------------------------------------------------------- #

def _is_test_file(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
        or "/tests/" in f"/{rel}"
        or ".test." in name
        or ".spec." in name
    )


def _iter_files(dirs: tuple[str, ...], suffixes: tuple[str, ...]) -> list[Path]:
    out: list[Path] = []
    for d in dirs:
        base = ROOT / d
        if not base.exists():
            continue
        for p in sorted(base.rglob("*")):
            if p.suffix not in suffixes or not p.is_file():
                continue
            if SKIP_DIR_PARTS.intersection(p.relative_to(ROOT).parts):
                continue
            rel = p.relative_to(ROOT).as_posix()
            if _is_test_file(rel):
                continue
            out.append(p)
    return out


def _rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


def _parse(p: Path) -> ast.Module | None:
    try:
        return ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
    except (SyntaxError, UnicodeDecodeError):
        return None


def _module_name(p: Path) -> str:
    parts = list(p.relative_to(ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _resolve_from(node: ast.ImportFrom, own_module: str, is_package: bool) -> str:
    """Absolute dotted name of the module an ImportFrom reads from."""
    if node.level == 0:
        return node.module or ""
    base = own_module.split(".")
    # A relative import inside a package's __init__ is relative to the package
    # itself; inside a module it is relative to the module's package.
    drop = node.level - 1 if is_package else node.level
    base = base[: len(base) - drop] if drop else base
    return ".".join([*base, node.module] if node.module else base)


class _QualnameVisitor(ast.NodeVisitor):
    """Walks a module tracking the enclosing function/class qualname."""

    def __init__(self) -> None:
        self.stack: list[str] = []
        self.func_depth = 0

    @property
    def qualname(self) -> str:
        return ".".join(self.stack) or "<module>"

    def _scoped(self, node: ast.AST, name: str, is_func: bool) -> None:
        self.stack.append(name)
        self.func_depth += is_func
        self.generic_visit(node)
        self.func_depth -= is_func
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scoped(node, node.name, True)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._scoped(node, node.name, True)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scoped(node, node.name, False)


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #

def _is_internal(module: str) -> bool:
    return module == "src" or module.startswith("src.")


def _has_lazy_marker(lines: list[str], node: ast.stmt) -> bool:
    end = getattr(node, "end_lineno", node.lineno) or node.lineno
    for i in range(node.lineno - 1, min(end, len(lines))):
        m = re.search(r"#\s*lazy:\s*(\S.*)", lines[i])
        if m:
            return True
    return False


class _DomainImportVisitor(_QualnameVisitor):
    def __init__(self, p: Path, found: list[Violation]) -> None:
        super().__init__()
        self.rel = _rel(p)
        self.own = _module_name(p)
        self.is_pkg = p.name == "__init__.py"
        self.lines = p.read_text(encoding="utf-8").splitlines()
        self.found = found

    def _imported_modules(self, node: ast.Import | ast.ImportFrom) -> list[str]:
        if isinstance(node, ast.Import):
            return [a.name for a in node.names]
        return [_resolve_from(node, self.own, self.is_pkg)]

    def _check(self, node: ast.Import | ast.ImportFrom) -> None:
        for mod in self._imported_modules(node):
            web = next((w for w in WEB_MODULE_PREFIXES if mod == w or mod.startswith(f"{w}.")), None)
            if web:
                self.found.append(Violation(
                    "web-import", self.rel, node.lineno, f"{self.qualname}->{web}",
                    f"domain module imports `{mod}`; domain code takes and returns "
                    "plain values and never depends on the web layer",
                ))
            if self.func_depth and _is_internal(mod) and not _has_lazy_marker(self.lines, node):
                self.found.append(Violation(
                    "lazy-import", self.rel, node.lineno, f"{self.qualname}->{mod}",
                    f"function-level import of `{mod}`; move the logic to its owning "
                    "module instead of dodging an import cycle (or annotate a genuinely "
                    "heavy import with `# lazy: <reason>`)",
                ))

    def visit_Import(self, node: ast.Import) -> None:
        self._check(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self._check(node)


def check_domain_imports() -> list[Violation]:
    """H1 lazy-import + web-import for domain modules."""
    found: list[Violation] = []
    for p in _iter_files(DOMAIN_DIRS, (".py",)):
        tree = _parse(p)
        if tree is not None:
            _DomainImportVisitor(p, found).visit(tree)
    return found


def check_private_imports() -> list[Violation]:
    """Encapsulation: never import another module's `_private` names."""
    found: list[Violation] = []
    for p in _iter_files(PRIVATE_IMPORT_DIRS, (".py",)):
        tree = _parse(p)
        if tree is None:
            continue
        rel = _rel(p)
        own = _module_name(p)
        is_pkg = p.name == "__init__.py"
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            src_mod = _resolve_from(node, own, is_pkg)
            if not _is_internal(src_mod) or src_mod == own:
                continue
            for alias in node.names:
                name = alias.name
                if name.startswith("_") and not name.startswith("__"):
                    found.append(Violation(
                        "private-import", rel, node.lineno, f"{src_mod}.{name}",
                        f"imports private `{name}` from `{src_mod}`; use that module's public "
                        "function (make one if needed) instead of its internals",
                    ))
    return found


def _numeric_literal(node: ast.expr) -> float | None:
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _numeric_literal(node.operand)
        return -inner if inner is not None else None
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    return None


def _mentions_term(node: ast.expr) -> bool:
    for sub in ast.walk(node):
        text = None
        if isinstance(sub, ast.Name):
            text = sub.id
        elif isinstance(sub, ast.Attribute):
            text = sub.attr
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            text = sub.value
        if text and _TERM_RE.search(text):
            return True
    return False


_ORDER_OPS = (ast.Lt, ast.LtE, ast.Gt, ast.GtE)


def _is_size_check(node: ast.expr) -> bool:
    # `len(ndvi_series) < 4` guards sample size; it is not a business threshold.
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "len"


def _threshold_hit(left: ast.expr, right: ast.expr) -> bool:
    for term_side, lit_side in ((left, right), (right, left)):
        lit = _numeric_literal(lit_side)
        if lit is None or lit in TRIVIAL_LITERALS or _is_size_check(term_side):
            continue
        if _mentions_term(term_side):
            return True
    return False


class _RenderThresholdVisitor(_QualnameVisitor):
    def __init__(self, rel: str, found: list[Violation]) -> None:
        super().__init__()
        self.rel = rel
        self.found = found

    def visit_Compare(self, node: ast.Compare) -> None:
        operands = [node.left, *node.comparators]
        for op, a, b in zip(node.ops, operands, operands[1:]):
            if isinstance(op, _ORDER_OPS) and _threshold_hit(a, b):
                expr = ast.unparse(node).replace(" ", "")
                self.found.append(Violation(
                    "render-threshold", self.rel, node.lineno, f"{self.qualname}:{expr}",
                    f"business threshold `{ast.unparse(node)}` in a rendering layer; "
                    "move the rule to the owning domain module and return the "
                    "value/class/label from there",
                ))
                break
        self.generic_visit(node)


def check_render_thresholds_py() -> list[Violation]:
    found: list[Violation] = []
    for p in _iter_files(RENDER_PY_DIRS, (".py",)):
        tree = _parse(p)
        if tree is not None:
            _RenderThresholdVisitor(_rel(p), found).visit(tree)
    return found


_TS_TERM = rf"[\w.]*(?:{'|'.join(BUSINESS_TERMS)})[\w.]*"
_TS_NUM = r"-?\d+(?:\.\d+)?"
_TS_CMP_RES = (
    re.compile(rf"(?P<lhs>{_TS_TERM})\s*(?P<op><=|>=|<|>)\s*(?P<num>{_TS_NUM})\b", re.IGNORECASE),
    re.compile(rf"\b(?P<num>{_TS_NUM})\s*(?P<op><=|>=|<|>)\s*(?P<lhs>{_TS_TERM})", re.IGNORECASE),
)


def check_render_thresholds_ts() -> list[Violation]:
    found: list[Violation] = []
    for p in _iter_files(RENDER_TS_DIRS, (".ts", ".tsx")):
        rel = _rel(p)
        for lineno, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("//", "*", "/*")):
                continue
            for rx in _TS_CMP_RES:
                for m in rx.finditer(line):
                    try:
                        lit = float(m.group("num"))
                    except ValueError:
                        continue
                    if lit in TRIVIAL_LITERALS or m.group("lhs").lower().endswith(".length"):
                        continue
                    expr = re.sub(r"\s+", "", m.group(0))
                    found.append(Violation(
                        "render-threshold", rel, lineno, expr,
                        f"business threshold `{m.group(0)}` in a React component; return the "
                        "class/label/colour key from the API instead",
                    ))
    return found


def _normalised_body_hash(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, int] | None:
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]  # drop docstring
    n_statements = sum(isinstance(n, ast.stmt) for stmt in body for n in ast.walk(stmt))
    if n_statements < DUP_MIN_STATEMENTS:
        return None
    dumped = "\n".join(ast.dump(s, annotate_fields=False, include_attributes=False) for s in body)
    return hashlib.sha1(dumped.encode()).hexdigest()[:12], n_statements


class _FunctionBodyVisitor(_QualnameVisitor):
    def __init__(self, rel: str, groups: dict[str, list[tuple[str, str, int]]]) -> None:
        super().__init__()
        self.rel = rel
        self.groups = groups

    def _fn(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        h = _normalised_body_hash(node)
        if h is not None:
            self.groups[h[0]].append((self.rel, ".".join([*self.stack, node.name]), node.lineno))
        self._scoped(node, node.name, True)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._fn(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._fn(node)


def check_duplicate_bodies() -> list[Violation]:
    """H2: identical function bodies. Only whole bodies are visible here;
    duplicated expressions inside larger functions need review."""
    groups: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
    for p in _iter_files(DUP_BODY_DIRS, (".py",)):
        tree = _parse(p)
        if tree is not None:
            _FunctionBodyVisitor(_rel(p), groups).visit(tree)

    found: list[Violation] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        for rel, qual, line in members:
            others = ", ".join(f"{r}::{q}" for r, q, _ in members if (r, q) != (rel, qual))
            found.append(Violation(
                "dup-body", rel, line, qual,
                f"body of `{qual}` is identical to {others}; move it to the owning module "
                "and call it from every site (H2)",
            ))
    return found


# --------------------------------------------------------------------------- #
# Test and runtime hygiene (HW, "How to work")
# --------------------------------------------------------------------------- #

def check_caplog_fixture() -> list[Violation]:
    """Tests must not rely on caplog: the app's logging config sets the "src"
    logger to propagate=False, so once any test in the worker runs lifespan,
    caplog's root handler sees no src.* records."""
    out: list[Violation] = []
    for d in TEST_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for p in sorted(base.rglob("test_*.py")):
            if SKIP_DIR_PARTS.intersection(p.relative_to(ROOT).parts):
                continue
            tree = _parse(p)
            if tree is None:
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                    a.arg == "caplog" for a in node.args.args + node.args.kwonlyargs
                ):
                    out.append(Violation(
                        "caplog", _rel(p), node.lineno, node.name,
                        f"`{node.name}` uses caplog; src.* loggers do not propagate after lifespan, "
                        "so assert by patching the module's logger (monkeypatch.setattr(mod.logger, ...))",
                    ))
    return out


def check_compose_mem_limits() -> list[Violation]:
    """Opt-in compose services (those with `profiles:`) must set mem_limit."""
    path = ROOT / COMPOSE_FILE
    if not path.exists():
        return []
    out: list[Violation] = []
    in_services = False
    current: str | None = None
    start = 0
    has_profile = has_limit = False

    def flush() -> None:
        if current and has_profile and not has_limit:
            out.append(Violation(
                "compose-mem-limit", COMPOSE_FILE, start, current,
                f"opt-in service `{current}` has no mem_limit; cap it so it cannot starve "
                "Postgres on the fixed-memory Docker VM",
            ))

    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            flush()
            current = None
            in_services = line.rstrip() == "services:"
            continue
        if not in_services:
            continue
        m = re.match(r"^  ([A-Za-z0-9_.-]+):\s*$", line)
        if m:
            flush()
            current, start, has_profile, has_limit = m.group(1), lineno, False, False
            continue
        if current and re.match(r"^    profiles:", line):
            has_profile = True
        if current and re.match(r"^    mem_limit:", line):
            has_limit = True
    flush()
    return out


def check_agents_md_sync() -> list[Violation]:
    """CLAUDE.md only imports AGENTS.md; guidance lives in AGENTS.md alone.

    Tools that write into CLAUDE.md (e.g. the GitNexus indexer re-adding its
    block) or a hand edit there would otherwise let the two files drift."""
    claude = ROOT / CLAUDE_FILE
    if not claude.exists() or not (ROOT / AGENTS_FILE).exists():
        return []
    text = claude.read_text(encoding="utf-8")
    out: list[Violation] = []
    if not re.search(r"^@AGENTS\.md\s*$", text, re.MULTILINE):
        out.append(Violation(
            "agents-md-sync", CLAUDE_FILE, 1, "missing @AGENTS.md import",
            "CLAUDE.md must import AGENTS.md with a line `@AGENTS.md`",
        ))
    for lineno, line in enumerate(text.splitlines(), 1):
        if line.startswith("## ") or "gitnexus:start" in line:
            out.append(Violation(
                "agents-md-sync", CLAUDE_FILE, lineno, line.strip()[:60],
                "guidance belongs in AGENTS.md (the single source); CLAUDE.md only imports it",
            ))
    return out


def collect() -> list[Violation]:
    violations = (
        check_domain_imports()
        + check_private_imports()
        + check_render_thresholds_py()
        + check_render_thresholds_ts()
        + check_duplicate_bodies()
        + check_caplog_fixture()
        + check_compose_mem_limits()
        + check_agents_md_sync()
    )
    return sorted(violations, key=lambda v: (v.rule, v.path, v.line, v.detail))


# --------------------------------------------------------------------------- #
# Baseline ratchet
# --------------------------------------------------------------------------- #

def _load_baseline_text(text: str) -> Counter[str]:
    data = json.loads(text)
    return Counter({k: int(v) for k, v in data.get("violations", {}).items()})


def load_baseline() -> Counter[str]:
    if not BASELINE_PATH.exists():
        return Counter()
    return _load_baseline_text(BASELINE_PATH.read_text(encoding="utf-8"))


def write_baseline(violations: list[Violation]) -> None:
    counts = Counter(v.key for v in violations)
    by_rule = Counter(v.rule for v in violations)
    data = {
        "_comment": (
            "Debt that existed when CODING_STANDARDS.md was adopted. A ratchet: "
            "delete entries as you fix them; never add new ones. Regenerating this "
            "file to admit new debt is a standards violation."
        ),
        "totals": dict(sorted(by_rule.items())),
        "violations": dict(sorted(counts.items())),
    }
    BASELINE_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def base_branch_baseline(ref: str) -> Counter[str] | None:
    try:
        out = subprocess.run(
            ["git", "show", f"{ref}:{BASELINE_REL}"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None  # base predates the standards gate
    return _load_baseline_text(out)


def evaluate(violations: list[Violation], baseline: Counter[str], base: Counter[str] | None) -> list[str]:
    """Return human-readable failure lines (empty list = pass)."""
    failures: list[str] = []
    current = Counter(v.key for v in violations)

    new_keys = current - baseline
    if new_keys:
        failures.append(f"NEW violations ({sum(new_keys.values())}) — fix these, do not baseline them:")
        remaining = Counter(new_keys)
        for v in violations:
            if remaining[v.key] > 0:
                remaining[v.key] -= 1
                failures.append(f"  [{v.rule}] {v.path}:{v.line}  {v.message}")

    stale = baseline - current
    if stale:
        failures.append(
            f"FIXED debt still listed in {BASELINE_REL} ({sum(stale.values())}) — "
            "delete these entries in the same commit:"
        )
        failures.extend(f"  {k}" for k in sorted(stale))

    if base is not None:
        grown = baseline - base
        if grown:
            failures.append(
                f"{BASELINE_REL} grew versus the base branch ({sum(grown.values())}) — "
                "the baseline may only shrink:"
            )
            failures.extend(f"  {k}" for k in sorted(grown))
    return failures


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-ref", help="git ref of the PR base; fails if the baseline grew versus it")
    ap.add_argument("--hook", action="store_true", help="run as a Claude Code Stop hook")
    ap.add_argument("--write-baseline", action="store_true", help="bootstrap only: record current debt")
    ap.add_argument("--list", action="store_true", help="print every current violation and exit 0")
    args = ap.parse_args(argv)

    stop_hook_active = False
    if args.hook:
        try:
            payload = json.loads(sys.stdin.read() or "{}")
            stop_hook_active = bool(payload.get("stop_hook_active"))
        except json.JSONDecodeError:
            pass

    violations = collect()

    if args.list:
        for v in violations:
            print(f"[{v.rule}] {v.path}:{v.line}  {v.detail}")
        print(f"\n{len(violations)} violations: {dict(Counter(v.rule for v in violations))}")
        return 0

    if args.write_baseline:
        write_baseline(violations)
        print(f"Wrote {BASELINE_REL}: {len(violations)} entries "
              f"{dict(sorted(Counter(v.rule for v in violations).items()))}")
        return 0

    base = base_branch_baseline(args.base_ref) if args.base_ref else None
    failures = evaluate(violations, load_baseline(), base)
    if not failures:
        if not args.hook:
            print(f"Standards gate passed ({len(violations)} baselined violations remain; see CODING_STANDARDS.md).")
        return 0

    report = "\n".join(["Coding-standards gate failed (rules: CODING_STANDARDS.md).", *failures])
    if args.hook:
        print(report, file=sys.stderr)
        # Exit 2 makes Claude Code feed the report back and keep working; once it
        # is already continuing from this hook, exit 1 surfaces the report to the
        # user without looping.
        return 1 if stop_hook_active else 2
    print(report)
    return 1


if __name__ == "__main__":
    sys.exit(main())
