"""Test helpers for need-to-know (5C): categories from the schemas, Gaia's proposals in
force, and a person holding some of them."""

from __future__ import annotations

import uuid

from gaia.access.categories import CategoryService
from gaia.access.policy import compute_policy, load_access_facts, restrict_index
from gaia.auth.privileges import Effective
from gaia.auth.service import Principal
from gaia.query.retrieval_store import load_retrieval_index


async def categorize(factory, data_source: str = "demo_pg") -> dict[str, str]:
    """One category per schema, every object placed by Gaia's proposal; name -> id."""
    svc = CategoryService(factory)
    await svc.from_schemas(data_source, actor="setup")
    await svc.propose(data_source)
    return {c["name"]: c["id"] for c in await svc.categories()}


def person(login: str, categories: list[str] | set[str], *, pii: bool = False,
           roles: set[str] | None = None) -> Principal:
    return Principal(uuid.uuid5(uuid.NAMESPACE_DNS, login), login, login.title(), None, False,
                     "acme", Effective(roles=roles or {"user"}, categories=set(categories),
                                       pii_processor=pii))


async def policy_of(factory, settings, who: Principal, data_source: str = "demo_pg"):
    async with factory() as s:
        index = await load_retrieval_index(s, data_source, settings)
        facts = await load_access_facts(s, index)
    policy = compute_policy(facts, categories=who.effective.categories,
                            pii_processor=who.effective.pii_processor, label=who.login)
    return index, facts, policy, restrict_index(index, policy)
