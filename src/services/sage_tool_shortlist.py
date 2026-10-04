"""Per-turn tool shortlist for Sage: rank the whole catalog against the turn.

Sage's category filter sends a median of ~47 tool schemas and, on the routing
eval, drops the correct tool for ~10% of requests. This ranks every tool's
name, description and parameter names against the turn with BM25 and keeps
the top ``k``, so the model chooses from a short list that still contains the
right tool.

Two rankings are fused (reciprocal-rank fusion): keyword BM25, which is
strong on the vocabulary in the tool descriptions, and embedding similarity,
which generalizes better to wording the descriptions do not use. If
embeddings are unavailable the shortlist falls back to BM25 alone and says so.

Plain values in, plain values out (OpenAI-style tool dicts); stdlib only. The
embedding function is passed in, so this module does no I/O of its own.
"""
from __future__ import annotations

import asyncio
import math
import re
import unicodedata
import hashlib
import logging
from collections import Counter
from dataclasses import dataclass
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

# (texts) -> (vectors, model name); e.g. brain_embeddings.embed_texts.
Embedder = Callable[[list[str]], Awaitable[tuple[list[list[float]], str]]]

# BM25 parameters (standard defaults).
_K1 = 1.2
_B = 0.75
# Field weights: a word in the tool name says more than one in its prose.
_NAME_WEIGHT = 3
_PARAM_WEIGHT = 1
# Earlier user turns help follow-ups ("do the same for Huye") but must not
# outweigh the current request.
_HISTORY_WEIGHT = 0.5
# Tools already called in this conversation stay available for follow-ups.
_RECENT_TOOL_BONUS = 4.0
# Reciprocal-rank fusion constant (standard value from Cormack et al.).
_RRF_K = 60
# nomic-embed-text expects task prefixes; other models ignore them harmlessly.
_QUERY_PREFIX = "search_query: "
_DOC_PREFIX = "search_document: "
_DOC_DESCRIPTION_CHARS = 600

_STOPWORDS = frozenset(
    """a an and are as at be by can could do does for from get give how i in into is it its
    me my of on or our please s show tell that the their them then there these this those
    to us use using want was we what when where which who will with you your""".split()
)

# Words users say vs words tool descriptions use. Applied to the request only.
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "rain": ("precipitation", "rainfall"),
    "rainfall": ("precipitation",),
    "house": ("building",),
    "home": ("building",),
    "roof": ("building",),
    "green": ("vegetation", "ndvi"),
    "greenness": ("vegetation", "grvi"),
    "health": ("ndvi", "vegetation"),
    "stress": ("ndvi", "stress"),
    "pond": ("water",),
    "reservoir": ("water",),
    "flooded": ("flood",),
    "note": ("observation",),
    "log": ("observation",),
    "remember": ("observation", "brain"),
    "notes": ("brain",),
    "record": ("observation",),
    "picture": ("image",),
    "photo": ("image",),
    "drone": ("orthophoto", "raster"),
    "orthophoto": ("raster",),
    "cell": ("cell", "sector", "postgis"),
    "sector": ("sector", "cell", "postgis"),
    "dry": ("drought", "dry"),
    "forecast": ("weather",),
    "tomorrow": ("forecast",),
    "week": ("forecast",),
    "coop": ("cooperative", "brain"),
    "cooperative": ("brain",),
    "radar": ("sar",),
    "cloud": ("sar", "cloud"),
    # Map editing in everyday words.
    "around": ("buffer",),
    "radius": ("buffer",),
    "inside": ("clip",),
    "colour": ("style",),
    "color": ("style",),
    "feature": ("sql", "query"),
    "centroid": ("geometry", "expression"),
    "fly": ("zoom", "location"),
    "boundary": ("postgis",),
    "village": ("postgis",),
    # Observed weather vs forecast.
    "fell": ("statistic", "daily"),
    "last": ("statistic", "daily"),
    "month": ("statistic", "daily"),
    "atmosphere": ("weather",),
    # Crop stages.
    "flowering": ("stage", "growth", "phenology"),
    # French and Kinyarwanda (need native review).
    "prevision": ("forecast", "weather"),
    "meteo": ("weather", "forecast"),
    "demain": ("forecast",),
    "secheresse": ("drought",),
    "pluie": ("rain", "precipitation"),
    "imvura": ("rain", "precipitation", "forecast"),
    "icyumweru": ("forecast",),
    "gitaha": ("forecast",),
}


def _stem(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def tokenize(text: str) -> list[str]:
    """Lower-case word tokens, snake_case split, naive singulars, no stopwords."""
    plain = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    words = re.findall(r"[a-z0-9]+", plain.lower().replace("_", " "))
    return [_stem(w) for w in words if w not in _STOPWORDS and len(w) > 1]


@dataclass(frozen=True)
class _Doc:
    name: str
    terms: Counter
    length: int


def _tool_doc(tool: dict) -> _Doc:
    fn = tool.get("function", tool)
    name = str(fn.get("name", ""))
    params = (fn.get("parameters") or {}).get("properties") or {}
    terms: Counter = Counter()
    for token in tokenize(name):
        terms[token] += _NAME_WEIGHT
    for token in tokenize(fn.get("description", "")):
        terms[token] += 1
    for pname in params:
        for token in tokenize(pname):
            terms[token] += _PARAM_WEIGHT
    return _Doc(name=name, terms=terms, length=sum(terms.values()))


def _query_terms(user_text: str, history: list[dict]) -> Counter:
    query: Counter = Counter()

    def add(text: str, weight: float) -> None:
        for token in tokenize(text):
            query[token] += weight
            for extra in _SYNONYMS.get(token, ()):
                query[extra] += weight * 0.5

    add(user_text, 1.0)
    previous_users = [m.get("content") or "" for m in history if m.get("role") == "user"]
    if previous_users:
        add(str(previous_users[-1]), _HISTORY_WEIGHT)
    return query


def _recent_tool_names(history: list[dict]) -> set[str]:
    names: set[str] = set()
    for message in history:
        for call in message.get("tool_calls") or []:
            name = (call.get("function") or {}).get("name")
            if name:
                names.add(name)
    return names


def rank_tools(user_text: str, history: list[dict], tools: list[dict]) -> list[tuple[str, float]]:
    """(tool name, score) for every tool, best first; ties keep catalog order."""
    docs = [_tool_doc(tool) for tool in tools]
    if not docs:
        return []
    n = len(docs)
    avg_len = sum(d.length for d in docs) / n
    doc_freq: Counter = Counter()
    for doc in docs:
        doc_freq.update(doc.terms.keys())
    query = _query_terms(user_text, history)
    recent = _recent_tool_names(history)
    scored = []
    for position, doc in enumerate(docs):
        score = 0.0
        for term, q_weight in query.items():
            tf = doc.terms.get(term, 0)
            if not tf:
                continue
            idf = math.log(1 + (n - doc_freq[term] + 0.5) / (doc_freq[term] + 0.5))
            score += q_weight * idf * tf * (_K1 + 1) / (tf + _K1 * (1 - _B + _B * doc.length / avg_len))
        if doc.name in recent:
            score += _RECENT_TOOL_BONUS
        scored.append((doc.name, score, position))
    scored.sort(key=lambda item: (-item[1], item[2]))
    return [(name, score) for name, score, _ in scored]


def shortlist_tools(user_text: str, history: list[dict], tools: list[dict], *, k: int) -> list[dict]:
    """The ``k`` most relevant tools by BM25 alone, in the catalog's order."""
    keep = {name for name, _ in rank_tools(user_text, history, tools)[:k]}
    return [tool for tool in tools if tool.get("function", tool).get("name") in keep]


def fuse_rankings(*rankings: list[str]) -> list[str]:
    """Reciprocal-rank fusion of several best-first name rankings."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for position, name in enumerate(ranking):
            scores[name] = scores.get(name, 0.0) + 1.0 / (_RRF_K + position)
    first_seen = {name: i for i, name in enumerate(dict.fromkeys(n for r in rankings for n in r))}
    return sorted(scores, key=lambda name: (-scores[name], first_seen[name]))


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _tool_document(tool: dict) -> str:
    fn = tool.get("function", tool)
    name = str(fn.get("name", "")).replace("_", " ")
    return f"{_DOC_PREFIX}{name}. {str(fn.get('description', ''))[:_DOC_DESCRIPTION_CHARS]}"


def _doc_key(tool: dict) -> str:
    return hashlib.sha1(_tool_document(tool).encode()).hexdigest()


class ToolEmbeddingCache:
    """Tool-description embeddings, computed once per description.

    Embedding the whole catalog takes seconds (minutes on a cold Ollama), so
    a turn never waits for it: ``warm`` fills the cache in the background
    and turns use BM25 until ``ready``."""

    def __init__(self) -> None:
        self._vectors: dict[str, list[float]] = {}
        self._warming: asyncio.Task | None = None

    def ready(self, tools: list[dict]) -> bool:
        return all(_doc_key(tool) in self._vectors for tool in tools)

    async def vectors(self, tools: list[dict], embed: Embedder) -> list[list[float]]:
        docs = [_tool_document(tool) for tool in tools]
        keys = [hashlib.sha1(doc.encode()).hexdigest() for doc in docs]
        missing = [i for i, key in enumerate(keys) if key not in self._vectors]
        if missing:
            new, _model = await embed([docs[i] for i in missing])
            for i, vec in zip(missing, new):
                self._vectors[keys[i]] = vec
        return [self._vectors[key] for key in keys]

    def warm(self, tools: list[dict], embed: Embedder) -> None:
        """Start embedding the catalog in the background, once at a time."""
        if self._warming is not None and not self._warming.done():
            return

        async def run() -> None:
            try:
                await self.vectors(tools, embed)
            except Exception:
                logger.warning("tool shortlist: warming tool embeddings failed", exc_info=True)

        self._warming = asyncio.get_running_loop().create_task(run())


@dataclass(frozen=True)
class Shortlist:
    tools: list[dict]
    ranking: list[str]
    # "hybrid" normally. BM25 alone, and why: "bm25_warming" (tool embeddings
    # still being computed), "bm25_timeout" (query embedding too slow),
    # "bm25" (embeddings unavailable).
    method: str


# A turn waits at most this long for its query embedding (a warm local
# nomic-embed-text answers in ~50 ms); past it the turn uses BM25.
QUERY_EMBED_TIMEOUT_S = 1.0


async def hybrid_shortlist(
    user_text: str,
    history: list[dict],
    tools: list[dict],
    *,
    k: int,
    embed: Embedder | None,
    cache: ToolEmbeddingCache,
) -> Shortlist:
    """Fuse BM25 and embedding rankings and keep the top ``k`` tools."""
    bm25 = [name for name, _ in rank_tools(user_text, history, tools)]
    ranking, method = bm25, "bm25"
    if embed is not None and not cache.ready(tools):
        cache.warm(tools, embed)
        method = "bm25_warming"
    elif embed is not None:
        try:
            tool_vectors = await cache.vectors(tools, embed)
            previous = [m.get("content") or "" for m in history if m.get("role") == "user"]
            query = f"{_QUERY_PREFIX}{previous[-1] if previous else ''} {user_text}".strip()
            (query_vector,), _model = await asyncio.wait_for(embed([query]), QUERY_EMBED_TIMEOUT_S)
            names = [tool.get("function", tool).get("name") for tool in tools]
            sims = [_cosine(query_vector, vec) for vec in tool_vectors]
            by_similarity = [names[i] for i in sorted(range(len(names)), key=lambda i: -sims[i])]
            ranking, method = fuse_rankings(bm25, by_similarity), "hybrid"
        except asyncio.TimeoutError:
            logger.warning("tool shortlist: query embedding over %.1fs, using BM25 only", QUERY_EMBED_TIMEOUT_S)
            method = "bm25_timeout"
        except Exception:
            logger.warning("tool shortlist: embeddings unavailable, using BM25 only", exc_info=True)
    keep = set(ranking[:k])
    kept = [tool for tool in tools if tool.get("function", tool).get("name") in keep]
    return Shortlist(tools=kept, ranking=ranking, method=method)
