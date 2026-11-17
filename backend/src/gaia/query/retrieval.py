"""Retrieval: from a business question to a compact schema context (Phase 4B).

The context is what the SQL generator (4C) will see: the objects and columns a
question needs, what Gaia believes they mean and how sure it is, the values and
ranges that make filters writable, and the only joins it may use.

**Two channels, fused.** *Lexical*: BM25 over names and meanings — object and
column names split into words (abbreviations expanded: ``qty``, ``amt``,
``cust``, ``trx`` …), business names, synonyms, purposes, semantic types, and the
readable values of low-cardinality columns (so "web orders" finds the column
that holds ``'web'``). It needs no extension and no model, so retrieval works
when the embedding service is down and on objects that were never reasoned
(their names are all there is). *Vector*: cosine similarity of the question to
the stored ``bge-m3`` embeddings of the current table and column meanings
(pgvector). The ranks are fused (reciprocal rank fusion), column hits are rolled
up to their objects, and every object's relevance is weighted by how sure Gaia is
of its meanings.

**Every non-abstain meaning is used, ranked by score, and uncertainty is
disclosed (R2).** ``accepted`` is not a filter — in the worst case almost nothing
is accepted before an SME has looked. Abstained and SME-rejected meanings are not
used (the column keeps its name only); SME-reviewed meanings count as certain.
Every column in the context says whether its meaning is certain, uncertain
(score and band) or absent, and the context lists the uncertain ones the answer
relies on.

**Joins.** The objects found are connected through the same join paths the SQL
guard accepts (``query.catalog``: declared FKs, tested model hints — D48 —, SME
hints, view lineage), shortest path first, declared FKs preferred; objects on
such a path are added as *bridges*. The context lists exactly those joins.

**Implied objects.** Questions name the things they rank, not the activity that
ranks them: "the best supplier in 2025" needs the order lines that sold its products
and the orders that date them. Two structural rules, true of any schema, add what
words cannot find: a question that ranks or aggregates gets, for each of its two
best objects that other tables reference, the largest table referencing it (the
facts behind the ranking); a question naming a period gets, for each fact in the
context without a date, the dated table it references (where the dates are). At
most ``max_expansions`` objects, marked ``expansion`` in the context.

**Values reach the model only as the exposure policy allows (D24)**: enum labels,
the values the model itself stated, and the profiled values of low-cardinality
columns that no detector marks as personal data; ranges for dates and numbers.

Deterministic for a given store, question and query vector.
"""

from __future__ import annotations

import heapq
import math
import re
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from gaia.query.catalog import GuardCatalog, JoinEdge

__all__ = [
    "IndexedColumn",
    "IndexedObject",
    "RetrievalIndex",
    "RetrievalSettings",
    "SchemaContext",
    "VectorHit",
    "retrieve",
    "tokens",
]

# -- words ---------------------------------------------------------------------------------

_STOP = frozenset("""
a an the of by for in on at per to from with and or is are was were be been what which who
whom how many much show list give me tell us our we i do does did each all any every top
bottom most least than then this that these those it its as into over under between
please get find display number count total where other but than greater higher lower
more less better worse compare comparing compared versus vs list show give most
one two three four five six seven eight nine ten eleven twelve fifteen twenty thirty
forty fifty hundred first second third last time times ever all-time""".split())
# "number", "count", "total" are too common to rank by, yet matter to the generator:
# they stay in the question, only not in matching.

ABBREVIATIONS = {
    "qty": "quantity", "amt": "amount", "num": "number", "no": "number", "nbr": "number",
    "cust": "customer", "cus": "customer", "clt": "client", "trx": "transaction",
    "txn": "transaction", "tx": "transaction", "dt": "date", "ts": "timestamp",
    "desc": "description", "addr": "address", "ccy": "currency", "cur": "currency",
    "prod": "product", "cat": "category", "emp": "employee", "dept": "department",
    "inv": "invoice", "mst": "master", "prm": "parameter", "param": "parameter",
    "cnt": "count", "pct": "percent", "avg": "average", "min": "minimum", "max": "maximum",
    "yr": "year", "mo": "month", "mth": "month", "wk": "week", "hr": "hour",
    "sku": "sku", "id": "id", "pmt": "payment", "pay": "payment", "ord": "order",
    "qtty": "quantity", "val": "value", "bal": "balance", "acct": "account",
    "acc": "account", "loc": "location", "str": "store", "sup": "supplier",
    "supp": "supplier", "vnd": "vendor", "mgr": "manager", "ref": "reference",
}
# Everyday business words and the words databases use for them. Applied to the question
# only, as extra words (the question keeps its own): "shops" also looks for "store".
# Lexical matching needs them when the embeddings are unavailable or the meanings are
# missing; with bge-m3 and reasoned synonyms they rarely change a ranking.
BUSINESS_SYNONYMS = {
    "shop": ["store"], "outlet": ["store"], "branch": ["store"], "boutique": ["store"],
    "client": ["customer"], "buyer": ["customer"], "consumer": ["customer"],
    "item": ["product"], "good": ["product"], "article": ["product"], "merchandise": ["product"],
    "sale": ["order", "amount"], "sold": ["order"], "sell": ["order"],
    "purchase": ["order"], "revenue": ["amount"], "turnover": ["amount"],
    "income": ["amount"], "spend": ["amount"], "spent": ["amount"],
    "profit": ["margin", "cost"], "profitable": ["profit", "margin", "cost"],
    "valuable": ["amount", "value"], "value": ["amount"], "vendor": ["supplier"],
    "staff": ["employee"], "worker": ["employee"], "personnel": ["employee"],
    "representative": ["employee"], "salesperson": ["employee"], "seller": ["employee"],
    "stock": ["inventory", "quantity"], "refund": ["return"], "returned": ["return"],
}
# Not here on purpose: time words ("per day", "monthly"). They say how to group, not which
# object; as synonyms of "date" they pulled every dated object ahead of the one asked for.
_SPLIT = re.compile(r"[A-Za-z]+|\d+")
# "last month", "this year", "past 7 days": a time filter, not words to match
_PERIOD = re.compile(
    r"\b((19|20)\d\d|january|february|march|april|may|june|july|august|september|october|"
    r"november|december|year|years|month|months|quarter|week|weekly|daily|monthly|yearly|"
    r"annual|today|yesterday|since|until|between)\b", re.IGNORECASE)
_RANKING = re.compile(
    r"\b(best|worst|top|bottom|most|least|highest|lowest|largest|smallest|biggest|"
    r"performing|performance|profitable|valuable|total|sum|average|how many|how much|"
    r"count|ranking|rank)\b", re.IGNORECASE)
_RELATIVE_TIME = re.compile(
    r"\b(last|this|previous|next|past|current|prior)\s+(\d+\s+)?"
    r"(day|week|month|quarter|year|hour)s?\b", re.IGNORECASE)
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])")


def _stem(word: str) -> str:
    if len(word) == 4 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]  # days, cats: plural of a three-letter word
    if len(word) <= 4:
        return word
    for suffix, repl in (("ies", "y"), ("sses", "ss"), ("ing", ""), ("es", ""), ("ed", ""),
                         ("s", "")):
        if suffix == "s" and word.endswith(("us", "is", "ss")):
            continue  # status, basis, class: not plurals
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            if suffix == "es" and not word.endswith(("ses", "xes", "zes", "ches", "shes")):
                continue
            return word[: len(word) - len(suffix)] + repl
    return word


def question_tokens(question: str) -> list[str]:
    """``tokens`` of a question, relative time phrases ("last month") left out and
    everyday business words extended with the words databases use for them."""
    out = []
    for t in tokens(_RELATIVE_TIME.sub(" ", question)):
        out.append(t)
        out += [x for x in BUSINESS_SYNONYMS.get(t, []) if x not in out]
    return out


def tokens(text: str | None, *, keep_stop: bool = False) -> list[str]:
    """Lower-case word stems; identifiers split on ``_``, digits and camelCase;
    abbreviations expanded."""
    if not text:
        return []
    out = []
    for part in _SPLIT.findall(_CAMEL.sub(" ", str(text))):
        word = part.lower()
        if word.isdigit():
            continue
        word = ABBREVIATIONS.get(word, word)
        if not keep_stop and word in _STOP:
            continue
        out.append(_stem(word))
    return out


class _BM25:
    """Okapi BM25 over small in-memory documents (k1 1.2, b 0.75)."""

    def __init__(self, docs: dict[Any, list[str]]) -> None:
        self.docs = {k: Counter(v) for k, v in docs.items() if v}
        self.lengths = {k: sum(c.values()) for k, c in self.docs.items()}
        self.avg = (sum(self.lengths.values()) / len(self.lengths)) if self.lengths else 1.0
        df: Counter[str] = Counter()
        for c in self.docs.values():
            df.update(c.keys())
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def search(self, query: list[str], k: int) -> list[tuple[Any, float, list[str]]]:
        terms = [t for t in dict.fromkeys(query) if t in self.idf]
        scored = []
        for key, counts in self.docs.items():
            score, matched = 0.0, []
            for t in terms:
                f = counts.get(t)
                if not f:
                    continue
                norm = f * 2.2 / (f + 1.2 * (0.25 + 0.75 * self.lengths[key] / self.avg))
                score += self.idf[t] * norm
                matched.append(t)
            if score > 0:
                scored.append((key, score, matched))
        scored.sort(key=lambda x: (-x[1], str(x[0])))
        return scored[:k]


# -- the index -----------------------------------------------------------------------------


@dataclass
class IndexedColumn:
    name: str
    data_type: str
    ordinal: int = 0
    business_name: str | None = None
    purpose: str | None = None
    role: str | None = None
    entity: str | None = None
    unit: str | None = None
    synonyms: list[str] = field(default_factory=list)
    semantic_type: str | None = None
    score: float | None = None
    band: str | None = None
    status: str | None = None
    values: list[str] = field(default_factory=list)  # readable under the exposure policy
    # what the values mean, stated by an SME (5B): {"2": "overdue"}
    meanings: dict[str, str] = field(default_factory=dict)
    distinct: int | None = None  # how many values there are (the list may be the top ones)
    value_note: str | None = None  # e.g. "~2,300 distinct values" when not listed
    range: tuple[str, str] | None = None  # min, max for dates and numbers
    is_key: bool = False
    personal: bool = False  # a detector found personal data: values stay local (D24)

    @property
    def reasoned(self) -> bool:
        return self.business_name is not None

    @property
    def certainty(self) -> str:
        """certain | uncertain | none — what the context discloses."""
        if not self.reasoned:
            return "none"
        if self.status == "sme_reviewed" or self.band == "accepted":
            return "certain"
        return "uncertain"


@dataclass
class IndexedObject:
    schema: str
    name: str
    kind: str  # table | view
    rows: int | None = None
    business_name: str | None = None
    purpose: str | None = None
    grain: str | None = None
    entity: str | None = None
    synonyms: list[str] = field(default_factory=list)
    score: float | None = None
    status: str | None = None
    columns: list[IndexedColumn] = field(default_factory=list)
    primary_key: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.schema}.{self.name}"

    def column(self, name: str) -> IndexedColumn | None:
        low = name.lower()
        return next((c for c in self.columns if c.name.lower() == low), None)

    @property
    def confidence(self) -> float:
        """How sure Gaia is of this object's meanings, 0..1 (unreasoned: 0.5)."""
        if self.status == "sme_reviewed":
            return 1.0
        scores = [c.score for c in self.columns if c.reasoned and c.score is not None]
        if self.score is not None:
            scores.append(self.score)
        return sum(scores) / len(scores) if scores else 0.5


@dataclass
class RetrievalIndex:
    data_source: str
    dialect: str
    database: str
    objects: dict[str, IndexedObject]  # "schema.name" -> object
    catalog: GuardCatalog
    data_source_id: uuid.UUID | None = None
    snapshot_id: str | None = None
    # business terms SMEs defined (5B): (term, definition, synonyms)
    glossary: list[tuple[str, str, list[str]]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._lex_columns: _BM25 | None = None
        self._lex_objects: _BM25 | None = None

    def obj(self, key: str) -> IndexedObject | None:
        return self.objects.get(key) or next(
            (o for k, o in self.objects.items() if k.lower() == key.lower()), None)

    # lexical documents (built once)
    def _docs(self) -> tuple[_BM25, _BM25]:
        if self._lex_columns is None or self._lex_objects is None:
            col_docs: dict[tuple[str, str], list[str]] = {}
            obj_docs: dict[str, list[str]] = {}
            for o in self.objects.values():
                odoc = tokens(o.name) * 2 + tokens(o.business_name) * 3
                for s in o.synonyms:
                    odoc += tokens(s) * 2
                odoc += tokens(o.entity) + tokens(o.purpose) + tokens(o.grain)
                for c in o.columns:
                    odoc += tokens(c.business_name or c.name)
                    doc = tokens(c.name) * 2 + tokens(c.business_name) * 3
                    for s in c.synonyms:
                        doc += tokens(s) * 2
                    doc += tokens(c.entity) + tokens(c.purpose) + tokens(c.semantic_type)
                    doc += tokens(c.unit)
                    for v in c.values:
                        doc += tokens(v) * 2
                    for meaning in c.meanings.values():  # "overdue" finds the status column
                        doc += tokens(meaning) * 2
                    doc += tokens(o.business_name or o.name)
                    col_docs[(o.key, c.name)] = doc
                obj_docs[o.key] = odoc
            self._lex_columns, self._lex_objects = _BM25(col_docs), _BM25(obj_docs)
        return self._lex_columns, self._lex_objects


# -- retrieval -----------------------------------------------------------------------------


@dataclass(frozen=True)
class RetrievalSettings:
    max_objects: int = 6  # found by relevance; bridges come on top
    max_bridges: int = 3
    max_expansions: int = 2  # implied objects (the facts behind a ranking, their dates)
    max_join_hops: int = 3
    relative_cutoff: float = 0.25  # objects below this share of the best are dropped
    lexical_k: int = 40
    all_columns_up_to: int = 20  # smaller objects are shown whole
    max_columns_per_object: int = 25
    context_budget_tokens: int = 6000
    rrf_k: int = 60


@dataclass(frozen=True)
class VectorHit:
    """One nearest embedding: an object (``column`` None) or a column, with similarity."""

    obj: str
    column: str | None
    similarity: float


@dataclass
class Match:
    channel: str  # lexical | vector | value
    detail: str

    def __str__(self) -> str:
        return f"{self.channel}: {self.detail}"


@dataclass
class RetrievedObject:
    obj: IndexedObject
    relevance: float
    rank: int
    bridge: bool = False
    matches: list[Match] = field(default_factory=list)
    matched_columns: list[str] = field(default_factory=list)
    columns: list[IndexedColumn] = field(default_factory=list)  # shown in the context


@dataclass
class SchemaContext:
    question: str
    data_source: str
    dialect: str
    objects: list[RetrievedObject]
    joins: list[JoinEdge]
    ranking: list[tuple[str, float]]  # every candidate object and its fused relevance
    notes: list[str] = field(default_factory=list)
    snapshot_id: str | None = None
    definitions: list[tuple[str, str]] = field(default_factory=list)  # glossary terms used

    @property
    def tables(self) -> list[str]:
        return [r.obj.key for r in self.objects]

    @property
    def uncertain(self) -> list[str]:
        """Columns in the context whose meaning is machine-inferred and not accepted."""
        out = []
        for r in self.objects:
            for c in r.columns:
                if c.certainty == "uncertain":
                    out.append(f"{r.obj.key}.{c.name}: {c.business_name} "
                               f"({c.band}, {c.score:.2f})" if c.score is not None else
                               f"{r.obj.key}.{c.name}: {c.business_name} ({c.band})")
        return out

    def render(self) -> str:
        return render_context(self)

    def estimate_tokens(self) -> int:
        return math.ceil(len(self.render()) / 3.5)


def _rrf(ranked: list[Any], k: int) -> dict[Any, float]:
    return {key: 1.0 / (k + i + 1) for i, key in enumerate(ranked)}


def retrieve(index: RetrievalIndex, question: str, *, vector_hits: list[VectorHit] | None = None,
             settings: RetrievalSettings | None = None, notes: list[str] | None = None
             ) -> SchemaContext:
    """Rank objects for ``question``, connect them and choose what to show."""
    s = settings or RetrievalSettings()
    notes = list(notes or [])
    definitions = glossary_matches(index, question)
    # a definition's words search too: "overdue invoice = finance.invoices.status = 2" brings
    # the invoices and their status column although the question names neither
    q = question_tokens(" ".join([question, *(d for _t, d in definitions)]))
    lex_cols, lex_objs = index._docs()
    col_hits = lex_cols.search(q, s.lexical_k)
    obj_hits = lex_objs.search(q, s.lexical_k)

    matches: dict[str, list[Match]] = defaultdict(list)
    col_score: dict[tuple[str, str], float] = defaultdict(float)
    obj_score: dict[str, float] = defaultdict(float)

    for (okey, col), rrf in _rrf([h[0] for h in col_hits], s.rrf_k).items():
        col_score[(okey, col)] += rrf
    for (okey, col), _score, matched in col_hits[:12]:
        c = index.objects[okey].column(col)
        words = ", ".join(matched)
        in_values = c is not None and any(set(matched) & set(tokens(v)) for v in c.values)
        matches[okey].append(Match("value" if in_values else "lexical",
                                   f"{col} ({words})"))
    for okey, rrf in _rrf([h[0] for h in obj_hits], s.rrf_k).items():
        obj_score[okey] += rrf
    for okey, _score, matched in obj_hits[:8]:
        matches[okey].append(Match("lexical", f"object ({', '.join(matched)})"))

    if vector_hits:
        ranked_cols = [(h.obj, h.column) for h in vector_hits if h.column]
        ranked_objs = [h.obj for h in vector_hits if not h.column]
        for key, rrf in _rrf(ranked_cols, s.rrf_k).items():
            if key[0] in index.objects:
                col_score[key] += rrf
        for key, rrf in _rrf(ranked_objs, s.rrf_k).items():
            if key in index.objects:
                obj_score[key] += rrf
        for h in vector_hits[:16]:
            if h.obj in index.objects:
                matches[h.obj].append(Match("vector", f"{h.column or 'object'} "
                                                      f"({h.similarity:.2f})"))
    elif vector_hits is None:
        notes.append("vector search not used: lexical ranking only")

    # roll columns up to their objects: the object's own rank, plus its best columns
    per_obj: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for (okey, col), v in col_score.items():
        per_obj[okey].append((v, col))
    relevance: dict[str, float] = {}
    for okey in set(obj_score) | set(per_obj):
        best = sorted(per_obj.get(okey, []), reverse=True)[:3]
        rel = obj_score.get(okey, 0.0) + sum(v * w for (v, _c), w in zip(best, (1, .5, .25),
                                                                         strict=False))
        relevance[okey] = rel * (0.6 + 0.4 * index.objects[okey].confidence)  # R2
    ranking = sorted(relevance.items(), key=lambda kv: (-kv[1], kv[0]))
    if not ranking:
        notes.append("nothing in the catalog matched the question")
        return SchemaContext(question, index.data_source, index.dialect, [], [], [], notes,
                             index.snapshot_id, definitions)

    top = ranking[0][1]
    chosen = [k for k, v in ranking if v >= s.relative_cutoff * top][: s.max_objects]
    expanded = _implied(index, chosen, question, s.max_expansions)
    picked: dict[str, RetrievedObject] = {}
    for i, key in enumerate(chosen, start=1):
        matched = [c for v, c in sorted(per_obj.get(key, []), reverse=True)]
        picked[key] = RetrievedObject(index.objects[key], relevance[key], i,
                                      matches=_dedupe(matches[key]), matched_columns=matched)

    for key, why in expanded:
        picked[key] = RetrievedObject(index.objects[key], relevance.get(key, 0.0),
                                      len(picked) + 1, matches=[Match("expansion", why)])
    joins, bridges, unconnected = _connect(index.catalog, chosen + [k for k, _ in expanded], s)
    for b in bridges[: s.max_bridges]:
        obj = index.obj(b)
        if obj is not None and obj.key not in picked:
            picked[obj.key] = RetrievedObject(obj, relevance.get(obj.key, 0.0), len(picked) + 1,
                                              bridge=True,
                                              matches=[Match("join", "bridge on a join path")])
    for key in unconnected:
        notes.append(f"{key}: no join path to the other objects (answer separately or ask)")
    keys = {k.lower() for k in picked}
    joins = [e for e in joins if e.from_table.lower() in keys and e.to_table.lower() in keys]

    join_cols: dict[str, set[str]] = defaultdict(set)
    for e in joins:
        join_cols[e.from_table.lower()].add(e.from_column.lower())
        join_cols[e.to_table.lower()].add(e.to_column.lower())
    for r in picked.values():
        r.columns = _choose_columns(r, join_cols.get(r.obj.key.lower(), set()), s)

    ctx = SchemaContext(question, index.data_source, index.dialect, list(picked.values()),
                        joins, ranking, notes, index.snapshot_id, definitions)
    _fit_budget(ctx, s)
    return ctx


def glossary_matches(index: RetrievalIndex, question: str) -> list[tuple[str, str]]:
    """The glossary terms (or their synonyms) a question uses, as whole words."""
    text = f" {' '.join(tokens(question))} "
    out = []
    for term, definition, synonyms in index.glossary:
        for name in (term, *synonyms):
            words = " ".join(tokens(name))
            if words and f" {words} " in text:
                out.append((term, definition))
                break
    return out


def _temporal(obj: IndexedObject) -> bool:
    types = ("date", "time", "timestamp")
    return any(c.role == "timestamp" or any(t in c.data_type.lower() for t in types)
               or c.semantic_type in ("date_int", "date_text", "datetime_text")
               for c in obj.columns)


def _implied(index: RetrievalIndex, chosen: list[str], question: str, limit: int
             ) -> list[tuple[str, str]]:
    """Objects the question needs but does not name (module docstring)."""
    if limit <= 0 or not chosen:
        return []
    catalog = index.catalog
    edges = [e for e in catalog.join_edges() if e.kind != "via_view"
             and catalog.is_unique(e.right) and e.from_table.lower() != e.to_table.lower()]
    lower = {k.lower(): k for k in index.objects}
    have = {k.lower() for k in chosen}
    out: list[tuple[str, str]] = []

    def rows(key: str) -> int:
        return index.objects[lower[key]].rows or 0

    def add(key: str, why: str) -> None:
        if key in lower and key not in have and len(out) < limit:
            have.add(key)
            out.append((lower[key], why))

    if _RANKING.search(question):
        for key in [k.lower() for k in chosen[:2]]:
            children = [e.from_table.lower() for e in edges if e.to_table.lower() == key
                        and e.from_table.lower() in lower]
            facts = [c for c in children if rows(c) > rows(key)]
            if facts and not any(c in have for c in facts):
                best = max(facts, key=lambda c: (rows(c), c))
                add(best, f"the activity behind a ranking of {lower[key]}")
    if _PERIOD.search(question):
        for key in list(have):
            if key not in lower or _temporal(index.objects[lower[key]]):
                continue
            parents = [e.to_table.lower() for e in edges if e.from_table.lower() == key
                       and e.to_table.lower() in lower]
            dated = [p for p in parents if _temporal(index.objects[lower[p]])
                     and rows(p) * 10 >= rows(key)]
            if dated and not any(p in have for p in dated):
                best = max(dated, key=lambda p: (rows(p), p))
                add(best, f"the dates of {lower[key]}")
    return out


def _dedupe(items: list[Match]) -> list[Match]:
    seen, out = set(), []
    for m in items:
        key = (m.channel, m.detail)
        if key not in seen:
            seen.add(key)
            out.append(m)
    return out


# Declared FKs and SME paths first, then through a view, then model hints (D48).
_KIND_COST = {"fk": 1.0, "sme": 1.0, "via_view": 1.2, "hint": 1.5}


def _connect(catalog: GuardCatalog, chosen: list[str], s: RetrievalSettings
             ) -> tuple[list[JoinEdge], list[str], list[str]]:
    """Join paths connecting the chosen objects (greedy shortest paths from the best
    one): (edges, bridge objects, objects that could not be connected)."""
    # only joins the guard accepts: one side unique (no fan trap), so never a sibling
    # pair — the generator joins through the parent or aggregates first
    edges = [e for e in catalog.join_edges()
             if catalog.is_unique(e.left) or catalog.is_unique(e.right)]
    graph: dict[str, list[tuple[str, JoinEdge]]] = defaultdict(list)
    for e in edges:
        a, b = e.from_table.lower(), e.to_table.lower()
        if a != b:
            graph[a].append((b, e))
            graph[b].append((a, e))
    if not chosen:
        return [], [], []
    connected = {chosen[0].lower()}
    used: list[JoinEdge] = []
    bridges: list[str] = []
    unconnected: list[str] = []
    for key in chosen[1:]:
        start = key.lower()
        if start in connected:
            continue
        path = _shortest(graph, start, connected, s.max_join_hops)
        if path is None:
            unconnected.append(key)
            continue
        for node, edge in path:
            used.append(edge)
            if node not in connected and node != start and node not in {c.lower()
                                                                         for c in chosen}:
                bridges.append(node)
            connected.add(node)
        connected.add(start)
    # every declared path between objects in the final set (e.g. two FKs to one key)
    final = connected | {b for b in bridges}
    for e in edges:
        if e.from_table.lower() in final and e.to_table.lower() in final and e not in used:
            used.append(e)
    used.sort(key=lambda e: (_KIND_COST.get(e.kind, 2.0), e.from_table, e.from_column))
    names = {k.lower(): k for k in catalog_names(catalog)}
    return used, [names.get(b, b) for b in dict.fromkeys(bridges)], unconnected


def catalog_names(catalog: GuardCatalog) -> list[str]:
    return [f"{s}.{o}" for s, objs in catalog.objects.items() for o in objs]


def _shortest(graph, start: str, targets: set[str], max_hops: int
              ) -> list[tuple[str, JoinEdge]] | None:
    """Dijkstra from ``start`` to the nearest node in ``targets``; the path as
    (node reached, edge used) pairs."""
    best: dict[str, float] = {start: 0.0}
    heap: list[tuple[float, int, str, list]] = [(0.0, 0, start, [])]
    counter = 0
    while heap:
        cost, hops, node, path = heapq.heappop(heap)
        if node in targets and node != start:
            return path
        if hops >= max_hops or cost > best.get(node, math.inf):
            continue
        for nxt, edge in sorted(graph.get(node, []), key=lambda x: (x[0], x[1].from_column)):
            c = cost + _KIND_COST.get(edge.kind, 2.0)
            if c < best.get(nxt, math.inf):
                best[nxt] = c
                counter += 1
                heapq.heappush(heap, (c, hops + 1, nxt, [*path, (nxt, edge)]))
    return None


def _choose_columns(r: RetrievedObject, join_cols: set[str], s: RetrievalSettings
                    ) -> list[IndexedColumn]:
    cols = r.obj.columns
    if len(cols) <= s.all_columns_up_to:
        return list(cols)
    keep = {c.lower() for c in r.obj.primary_key} | join_cols
    keep |= {c.lower() for c in r.matched_columns[:12]}
    chosen = [c for c in cols if c.name.lower() in keep]
    for c in cols:  # then time and measures: what questions filter and aggregate by
        if len(chosen) >= s.max_columns_per_object:
            break
        if c not in chosen and c.role in ("timestamp", "measure"):
            chosen.append(c)
    return sorted(chosen[: s.max_columns_per_object], key=lambda c: c.ordinal)


def _fit_budget(ctx: SchemaContext, s: RetrievalSettings) -> None:
    """Trim to the token budget: first unmatched, unkeyed columns of the lowest-ranked
    objects, then whole non-bridge objects from the end."""
    if ctx.estimate_tokens() <= s.context_budget_tokens:
        return
    for r in sorted(ctx.objects, key=lambda r: -r.rank):
        keep = {c.lower() for c in r.obj.primary_key} | {c.lower() for c in r.matched_columns[:6]}
        own = r.obj.key.lower()
        keep |= {e.from_column.lower() for e in ctx.joins if e.from_table.lower() == own}
        keep |= {e.to_column.lower() for e in ctx.joins if e.to_table.lower() == own}
        r.columns = [c for c in r.columns if c.name.lower() in keep] or r.columns[:3]
        if ctx.estimate_tokens() <= s.context_budget_tokens:
            ctx.notes.append("context trimmed to the token budget (columns)")
            return
    while len(ctx.objects) > 1 and ctx.estimate_tokens() > s.context_budget_tokens:
        victim = max((r for r in ctx.objects if not r.bridge), key=lambda r: r.rank,
                     default=None)
        if victim is None or victim is ctx.objects[0]:
            break
        ctx.objects.remove(victim)
        keys = {r.obj.key.lower() for r in ctx.objects}
        ctx.joins = [e for e in ctx.joins
                     if e.from_table.lower() in keys and e.to_table.lower() in keys]
        ctx.notes.append(f"context trimmed to the token budget: {victim.obj.key} left out")


# -- rendering -----------------------------------------------------------------------------

SEMANTIC_TYPE_HINTS = {
    "postgres": {
        "date_int": "integer date yyyymmdd: to_date({c}::text, 'YYYYMMDD'), or compare as integers",
        "time_int": "integer time hhmmss: hour = {c} / 10000",
        "date_text": "text holding dates: to_date({c}, <format>) after checking the values",
        "datetime_text": "text holding timestamps: convert with to_timestamp({c}, <format>)",
        "numeric_text": "text holding numbers: nullif(trim({c}), '')::numeric",
        "boolean_text": "text flag: compare with the listed values",
    },
    "mssql": {
        "date_int": "integer date yyyymmdd: CONVERT(date, CONVERT(char(8), {c}), 112), "
                    "or compare as integers",
        "time_int": "integer time hhmmss: hour = {c} / 10000",
        "date_text": "text holding dates: TRY_CONVERT(date, {c}, <style>) after checking values",
        "datetime_text": "text holding timestamps: TRY_CONVERT(datetime2, {c}, <style>)",
        "numeric_text": "text holding numbers: "
                        "TRY_CONVERT(decimal(18, 4), NULLIF(LTRIM(RTRIM({c})), ''))",
        "boolean_text": "text flag: compare with the listed values",
    },
}
_DIALECT_NAMES = {"postgres": "PostgreSQL", "postgresql": "PostgreSQL",
                  "mssql": "SQL Server (T-SQL)"}


def _column_line(ctx: SchemaContext, obj: IndexedObject, c: IndexedColumn,
                 refs: dict[str, list[str]]) -> str:
    parts = [f"{c.name} {c.data_type}"]
    if c.reasoned:
        desc = c.business_name or ""
        tags = [t for t in (c.role, c.unit) if t]
        if c.is_key:
            tags.append("key")
        line = f"{' '.join(parts)} — {desc}" + (f" [{', '.join(tags)}]" if tags else "")
        if c.certainty == "uncertain":
            score = f" {c.score:.2f}" if c.score is not None else ""
            line += f" (uncertain{score})"
    else:
        line = f"{' '.join(parts)} — (no meaning yet)" + (" [key]" if c.is_key else "")
    if refs.get(c.name.lower()):
        line += f" → {', '.join(refs[c.name.lower()])}"
    hint = SEMANTIC_TYPE_HINTS.get(ctx.dialect, {}).get(c.semantic_type or "")
    if hint:
        line += f"; {hint.format(c=c.name)}"
    elif c.semantic_type:
        line += f"; holds {c.semantic_type.replace('_', ' ')}"
    if c.meanings:  # an SME said what the values mean (5B)
        shown = ", ".join(f"'{v}' = {m}" for v, m in list(c.meanings.items())[:20])
        line += f"; values (meanings confirmed by an expert): {shown}"
    elif c.values:
        shown = ", ".join(f"'{v}'" for v in c.values[:20])
        total = max(c.distinct or 0, len(c.values))
        if total > min(len(c.values), 20):
            line += f"; most frequent values ({min(len(c.values), 20)} of ~{total:,}): {shown}"
        else:
            line += f"; values: {shown}"
    elif c.value_note:
        line += f"; {c.value_note}"
    if c.range:
        line += f"; range {c.range[0]} .. {c.range[1]}"
    return line


def render_context(ctx: SchemaContext) -> str:
    """The compact text the SQL generator sees."""
    lines = [f"Database: {ctx.data_source} ({_DIALECT_NAMES.get(ctx.dialect, ctx.dialect)}). "
             "Use only the objects, columns and joins below; qualify every object with its "
             "schema."]
    refs: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for e in ctx.joins:
        refs[e.from_table.lower()][e.from_column.lower()].append(f"{e.to_table}.{e.to_column}")
    for r in ctx.objects:
        o = r.obj
        head = f"\n{'VIEW' if o.kind == 'view' else 'TABLE'} {o.key}"
        if o.business_name:
            head += f' — "{o.business_name}"'
        if o.rows is not None:
            head += f", ~{o.rows:,} rows"
        lines.append(head)
        if o.purpose:
            lines.append(f"  {o.purpose}" + (f" Grain: {o.grain}" if o.grain else ""))
        elif not o.business_name:
            lines.append("  (no meaning yet: only its names are known)")
        for c in r.columns:
            lines.append("  - " + _column_line(ctx, o, c, refs[o.key.lower()]))
        hidden = len(o.columns) - len(r.columns)
        if hidden > 0:
            lines.append(f"  ({hidden} more column(s) not shown)")
    if ctx.definitions:
        lines.append("\nDEFINITIONS (business terms as the company's experts define them; "
                     "apply them):")
        for term, definition in ctx.definitions:
            lines.append(f"  {term}: {definition}")
    if ctx.joins:
        lines.append("\nJOINS (the only joins allowed):")
        for e in ctx.joins:
            kind = {"fk": "declared foreign key", "hint": "tested, inferred by the model",
                    "sme": "confirmed by an expert",
                    "via_view": "the view column's base column has this join path",
                    }.get(e.kind, e.kind)
            lines.append(f"  {e.from_table}.{e.from_column} = {e.to_table}.{e.to_column}"
                         f"  ({kind})")
    if any(c.certainty != "certain" for r in ctx.objects for c in r.columns):
        lines.append("\nMeanings marked (uncertain) are machine-inferred and not yet reviewed; "
                     "(no meaning yet) columns are known by name only. State any assumption "
                     "that rests on them.")
    for n in ctx.notes:
        if "no join path" in n:
            lines.append(f"Note: {n}")
    return "\n".join(lines)
