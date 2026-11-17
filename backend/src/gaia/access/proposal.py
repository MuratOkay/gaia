"""Gaia's proposal of each object's data category — version 2 (D77).

Still deterministic and explainable (D74: no model decides anything about access, and
every proposal is shown to, and confirmed by, people). What changed is *which* evidence
counts, after the owner found related tables proposed apart (``dbo.inventory`` → Product,
``dbo.inventory_movements`` → Sales: one word in the second one's purpose decided it):

1. **Direct evidence** — the category's name and keywords found in the object's schema
   (5: in a database organised by schemas the schema outweighs the name and business name
   together), name (2) or business name (2) is *direct*; found only in what Gaia wrote about it —
   its entity (1) or purpose (½) — it is *weak*: a purpose mentions other domains all the
   time ("stock movements caused by sales, returns …").
2. **Related evidence** — a table belongs with the table it is **named after**
   (``inventory_movements`` after ``inventory``, ``order_lines`` after ``orders``: 2.5)
   and, less, with the tables it **references** (declared foreign keys and joins an SME
   confirmed: 1 each). A table referenced by others takes a small vote (½) from those
   whose own evidence is direct. Votes are weighted by how sure the neighbour's category
   is: confirmed by a person 1.5 (people outweigh Gaia's words), direct 1, related 0.7,
   weak 0.4. Three rounds let it travel along a
   chain (lines → orders → customers).
3. **Views** that read objects of one category get it (as before), else as a table.

**Version 2.1 (D79)**, from the owner's confirmations on ``nge_small``: a schema that
names a category decides unless a person says otherwise (8 — more than every other word
together: ``hr.staff_addresses`` is HR data, whatever "addresses" suggests); a table whose
primary key holds its foreign key — a line of its header — belongs with that header like a
table named after it (2.5); the other references share one vote (a fact table referencing
products and customers is neither), and so do the tables referencing one (a lookup table
referenced by everything is not everyone's); a table named like an object of another schema
or data source — ``dw.dim_shop`` and ``dbo.shops`` — is about the same thing (2, from objects
whose category is confirmed or direct only).

The best category wins; a tie, or nothing, proposes nothing (the object stays readable by
nobody until someone decides). Every proposal carries its **basis** — ``direct``,
``view``, ``related`` or ``weak`` — and its reasons in words, so the administrator can
look at the uncertain ones first. Confirmed categories are never replaced, and they
anchor their neighbours: confirming ``inventory`` moves ``inventory_movements`` with it at
the next proposal.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from gaia.query.retrieval import tokens

__all__ = ["BASES", "CategoryTerms", "ObjectFacts", "Proposal", "literal_stems",
           "propose_all", "schema_keywords", "word_stems"]

FIELDS = (("schema", 8.0, True), ("name", 2.0, True), ("business_name", 2.0, True),
          ("entity", 1.0, False), ("purpose", 0.5, False))
HEAD, PARENT, CHILD, ALIAS = 2.5, 1.0, 0.5, 2.0
TRUST = {"confirmed": 1.5, "direct": 1.0, "view": 1.0, "related": 0.7, "weak": 0.4}
BASES = ("direct", "view", "related", "weak")
ROUNDS = 3
FIELD_WORDS = {"schema": "its schema", "name": "its name", "business_name": "its business "
               "name", "entity": "what it is about", "purpose": "its description"}
# words of table names that say how data is kept, not what it is about
GENERIC = frozenset("""
id key code type types status line lines item items detail details entry entries history
log logs event events audit archive backup bak old new tmp temp stage staging stg etl run
runs map mapping link links xref ref lookup data info master main base aux misc table tbl tb
view vw mv dim fact agg rpt report summary daily monthly yearly by per v x xx
""".split())


@dataclass(frozen=True)
class CategoryTerms:
    id: uuid.UUID
    name: str
    words: frozenset[str]  # stems of its name and keywords


@dataclass
class ObjectFacts:
    key: str  # schema.object, lower case
    schema: str
    name: str
    kind: str = "table"
    business_name: str | None = None
    entity: str | None = None
    purpose: str | None = None
    references: set[str] = field(default_factory=set)  # keys this object points at
    # references whose foreign key is part of this object's primary key (a line of a header)
    identifying: set[str] = field(default_factory=set)
    reads: set[str] | None = None  # a view: the objects its definition reads
    confirmed: uuid.UUID | None = None  # a category confirmed by an SME or an administrator


@dataclass
class Proposal:
    category: CategoryTerms | None
    basis: str | None  # direct | view | related | weak; None when nothing is proposed
    why: str


def _name_tokens(name: str) -> list[str]:
    return [t for t in tokens(name) if t not in GENERIC]


def literal_stems(text: str | None) -> set[str]:
    """The stems of the words as written — short ones kept, abbreviations *not* expanded:
    a category's vocabulary (``HR`` is "hr", not the "hour" retrieval reads it as)."""
    from gaia.query.retrieval import _CAMEL, _SPLIT, _stem

    return {_stem(w.lower()) for w in _SPLIT.findall(_CAMEL.sub(" ", text or ""))
            if not w.isdigit()}


def word_stems(word: str) -> set[str]:
    """What one word of an object's text may match: its literal stem and, for an
    abbreviation, what it stands for (``cust`` → customer)."""
    return literal_stems(word) | set(tokens(word))


def _direct(obj: ObjectFacts, cats: list[CategoryTerms]
            ) -> tuple[Counter[uuid.UUID], dict[uuid.UUID, bool], dict[uuid.UUID, list[str]]]:
    """Scores from the object's own words; whether each category's hits include a strong
    field; the reasons."""
    scores: Counter[uuid.UUID] = Counter()
    strong: dict[uuid.UUID, bool] = {}
    why: dict[uuid.UUID, list[str]] = {}
    for fname, weight, is_strong in FIELDS:
        text = getattr(obj, fname)
        if not text:
            continue
        seen: set[tuple[uuid.UUID, str]] = set()
        for word in re.findall(r"[A-Za-z0-9]+", text):  # the words as written
            for stem in sorted(word_stems(word)):
                for c in cats:
                    if stem in c.words and (c.id, stem) not in seen:
                        seen.add((c.id, stem))
                        scores[c.id] += weight
                        strong[c.id] = strong.get(c.id, False) or is_strong
                        why.setdefault(c.id, []).append(
                            f"“{word.lower()}” in {FIELD_WORDS[fname]}")
    return scores, strong, why


def _heads(objects: dict[str, ObjectFacts]) -> dict[str, str]:
    """For each object, the table of its schema it is named after — the longest other
    name whose words start its own (``inventory_movements`` → ``inventory``). Every word
    counts here, generic and stop words too: ``trade_in_items`` is not named after
    ``trade_ins``, and certainly not the other way round."""
    names = {k: tokens(o.name, keep_stop=True) for k, o in objects.items()}
    out: dict[str, str] = {}
    for k, o in objects.items():
        mine = names[k]
        best = None
        for other, theirs in names.items():
            if other == k or objects[other].schema.lower() != o.schema.lower():
                continue
            if objects[other].kind == "view" or not theirs or len(theirs) >= len(mine):
                continue
            if mine[:len(theirs)] == theirs and (best is None or len(theirs) > len(names[best])):
                best = other
        if best is not None:
            out[k] = best
    return out


def _pick(scores: Counter[uuid.UUID]) -> tuple[uuid.UUID | None, bool]:
    """(the best category, whether it is a tie)."""
    ranked = [(c, s) for c, s in scores.most_common() if s > 0]
    if not ranked:
        return None, False
    if len(ranked) > 1 and abs(ranked[0][1] - ranked[1][1]) < 1e-9:
        return None, True
    return ranked[0][0], False


def _subject(name: str) -> tuple[str, ...]:
    """What a name is about, without the words that say how data is kept
    (``dim_shop`` and ``shops`` → ("shop",))."""
    return tuple(_name_tokens(name))


def propose_all(objects: Iterable[ObjectFacts], categories: Iterable[CategoryTerms],
                elsewhere: Iterable[ObjectFacts] = ()) -> dict[str, Proposal]:
    """``elsewhere``: objects of other data sources whose category a person confirmed —
    evidence for objects named like them, never proposed themselves."""
    objs = {o.key: o for o in objects}
    anchors = [o for o in elsewhere if o.confirmed is not None]
    cats = sorted(categories, key=lambda c: c.name.lower())
    by_id = {c.id: c for c in cats}
    if not cats:
        return {k: Proposal(None, None, "no categories defined") for k in objs}
    direct = {k: _direct(o, cats) for k, o in objs.items()}
    heads = _heads(objs)
    parents = {k: {r for r in o.references if r in objs and r != k} for k, o in objs.items()}
    children: dict[str, set[str]] = {k: set() for k in objs}
    for k, ps in parents.items():
        for p in ps:
            children[p].add(k)
    # objects named like one of another schema (or data source): the same subject
    by_subject: dict[tuple[str, ...], list[str]] = {}
    for k, o in objs.items():
        if _subject(o.name):
            by_subject.setdefault(_subject(o.name), []).append(k)
    alike = {k: sorted(other for other in by_subject.get(_subject(o.name), [])
                       if objs[other].schema.lower() != o.schema.lower())
             for k, o in objs.items()}
    outside: dict[tuple[str, ...], list[ObjectFacts]] = {}
    for a in anchors:
        if _subject(a.name):
            outside.setdefault(_subject(a.name), []).append(a)

    # round 0: the objects' own evidence (and what people confirmed)
    state: dict[str, tuple[uuid.UUID | None, str | None]] = {}
    for k, o in objs.items():
        if o.confirmed is not None and o.confirmed in by_id:
            state[k] = (o.confirmed, "confirmed")
            continue
        scores, strong, _why = direct[k]
        best, _tie = _pick(scores)
        state[k] = (best, None if best is None else ("direct" if strong.get(best) else "weak"))

    tables = sorted(k for k, o in objs.items() if o.kind != "view")
    views = sorted(k for k, o in objs.items() if o.kind == "view")
    result: dict[str, Proposal] = {}

    def related(k: str, prev: dict[str, tuple[uuid.UUID | None, str | None]]
                ) -> tuple[Counter[uuid.UUID], dict[uuid.UUID, list[str]]]:
        votes: Counter[uuid.UUID] = Counter()
        why: dict[uuid.UUID, list[str]] = {}

        def vote(other: str, weight: float, words: str) -> None:
            cat, basis = prev.get(other, (None, None))
            if cat is None or basis is None:
                return
            votes[cat] += weight * TRUST[basis]
            why.setdefault(cat, []).append(f"{words} {objs[other].schema}.{objs[other].name}")

        head = heads.get(k)
        if head is not None:
            vote(head, HEAD, "named after")
        owners = {p for p in objs[k].identifying if p in parents[k] and p != head}
        for p in sorted(owners):  # a line of its header: belongs with it
            vote(p, HEAD, "belongs to (its key holds the reference)")
        others = sorted(parents[k] - owners)
        for p in others:  # the rest share one vote
            vote(p, PARENT / len(others), "and references" if p == head else "references")
        voters = sorted(c for c in children[k]
                        if prev.get(c, (None, None))[1] in ("direct", "confirmed")
                        and heads.get(c) != k and k not in objs[c].identifying)
        for c in voters:  # the tables referencing it share one small vote
            vote(c, CHILD / len(voters), "referenced by")
        for other in alike[k]:  # the same subject in another schema
            if prev.get(other, (None, None))[1] in ("direct", "confirmed"):
                vote(other, ALIAS, "named like")
        for a in outside.get(_subject(objs[k].name), []):  # … or in another data source
            if a.confirmed in by_id:
                votes[a.confirmed] += ALIAS * TRUST["confirmed"]
                why.setdefault(a.confirmed, []).append(
                    f"named like {a.key} (confirmed there)")
        return votes, why

    def decide(k: str, prev) -> tuple[uuid.UUID | None, str | None, str]:
        scores, strong, dwhy = direct[k]
        votes, vwhy = related(k, prev)
        total = scores + votes
        best, tie = _pick(total)
        if best is None:
            if tie:
                top = total.most_common(2)
                return None, None, "a tie between " + " and ".join(
                    sorted(by_id[c].name for c, _s in top))
            return None, None, ("no category's name or keywords match, and no related "
                                "object has a category")
        if strong.get(best):  # its own name, schema or business name says so
            basis = "direct"
        elif votes[best] >= scores[best]:
            basis = "related"
        else:
            basis = "weak"
        return best, basis, _reasons(dwhy.get(best, []), vwhy.get(best, []), basis)

    explained: dict[str, str] = {}
    for _round in range(ROUNDS):
        nxt = dict(state)
        for k in tables:
            if objs[k].confirmed is not None and objs[k].confirmed in by_id:
                continue
            cat, basis, why = decide(k, state)
            nxt[k] = (cat, basis)
            explained[k] = why
        if nxt == state:
            break
        state = nxt

    for k in tables:
        o = objs[k]
        if o.confirmed is not None and o.confirmed in by_id:
            result[k] = Proposal(by_id[o.confirmed], "confirmed", "confirmed")
            continue
        cat, basis = state[k]
        result[k] = Proposal(by_id[cat] if cat else None, basis if cat else None,
                             explained.get(k, ""))

    for k in views:
        o = objs[k]
        if o.confirmed is not None and o.confirmed in by_id:
            result[k] = Proposal(by_id[o.confirmed], "confirmed", "confirmed")
            continue
        if o.reads:
            found = {state.get(r, (None, None))[0] if r not in result else
                     (result[r].category.id if result[r].category else None)
                     for r in o.reads if r != k}
            if len(found) == 1 and None not in found:
                cat = by_id[found.pop()]
                result[k] = Proposal(cat, "view", "reads only objects of this category")
                continue
        scores, strong, dwhy = direct[k]
        best, tie = _pick(scores)
        if best is None:
            result[k] = Proposal(None, None, "a tie between " + " and ".join(
                sorted(by_id[c].name for c, _s in scores.most_common(2))) if tie else
                "it reads objects of several categories, and no category's name or keywords "
                "match" if o.reads else "no category's name or keywords match")
            continue
        basis = "direct" if strong.get(best) else "weak"
        result[k] = Proposal(by_id[best], basis, _reasons(dwhy.get(best, []), [], basis))
    return result


def _reasons(direct: list[str], related: list[str], basis: str) -> str:
    parts = []
    if direct:
        parts.append("matches " + ", ".join(dict.fromkeys(direct)))
    if related:
        parts.append(", ".join(dict.fromkeys(related)))
    text = "; ".join(parts) or "—"
    if basis == "weak":
        text += " (only what Gaia wrote about it matches: check it)"
    return text


def _literal_words(name: str) -> list[str]:
    """The words a name is written with — as stems, without generic words, and without the
    abbreviations retrieval expands (``trx`` is not evidence that a table is about
    "transactions": legacy names are cryptic on purpose)."""
    from gaia.query.retrieval import _CAMEL, _SPLIT, _STOP, ABBREVIATIONS, _stem

    out = []
    for part in _SPLIT.findall(_CAMEL.sub(" ", name)):
        word = part.lower()
        if word.isdigit() or word in ABBREVIATIONS or word in _STOP or len(word) <= 2:
            continue
        stem = _stem(word)
        if stem not in GENERIC:
            out.append(stem)
    return out


def schema_keywords(objects: dict[str, list[str]]) -> dict[str, list[str]]:
    """For "categories from the schemas": besides the schema's name, the first word of each
    of its tables' names (``sales`` → order, payment, return …), so that the categories also
    recognise the same kind of data in another data source, where the schema is ``dbo``.
    Only literal words count (no expanded abbreviations, no generic words such as ``line``
    or ``history``); a word two schemas share says nothing and is left out. Only tables
    count (views name their subjects)."""
    words: dict[str, set[str]] = {}
    for schema, names in objects.items():
        for name in names:
            found = _literal_words(name)
            words.setdefault(schema, set()).update(found[:1])
    owners: Counter[str] = Counter(w for ws in words.values() for w in ws)
    return {s: sorted(w for w in ws if owners[w] == 1) for s, ws in words.items()}
