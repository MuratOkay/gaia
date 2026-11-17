"""Data categories over HTTP (Phase 5C, D70, D74) — the administrator decides.

    GET    /api/admin/categories?include_retired=true
    POST   /api/admin/categories                          {"name", "description"?, "keywords"?}
    PATCH  /api/admin/categories/{id or name}             {"name"?, "description"?, "keywords"?}
    DELETE /api/admin/categories/{id or name}             retire (only when no object is in it)
    POST   /api/admin/data-sources/{ds}/categories/from-schemas   one category per schema
    POST   /api/admin/data-sources/{ds}/categories/propose        Gaia's proposals
    GET    /api/admin/data-sources/{ds}/objects           every object and its category
    PUT    /api/admin/data-sources/{ds}/objects/{table_id}/category   {"category", "note"?}
    GET    /api/admin/category-suggestions                directory roles → categories

Granting a category is a grant like any other (``POST /api/admin/grants`` with
``"privilege": "category", "category": "Finance"``). SMEs confirm an object's category on
the review screen (``/api/review/{ds}/tables/{id}/category``); the auditor reads
``/api/audit/categories``.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from gaia.access.categories import CategoryError, CategoryService
from gaia.auth.deps import get_auth, require
from gaia.auth.service import AuthService, Principal

__all__ = ["get_categories", "router"]

router = APIRouter(prefix="/api/admin", tags=["access"])
ADMIN = require("admin")


class NewCategory(BaseModel):
    name: str = Field(min_length=1)
    description: str | None = None
    keywords: list[str] = Field(default_factory=list)


class CategoryChange(BaseModel):
    name: str | None = None
    description: str | None = None
    keywords: list[str] | None = None


class Placement(BaseModel):
    category: str = Field(min_length=1)
    note: str | None = None


def get_categories(request: Request) -> CategoryService:
    service = getattr(request.app.state, "categories", None)
    if service is None:
        auth: AuthService = get_auth(request)
        service = request.app.state.categories = CategoryService(auth.factory)
    return service


async def _run(work):
    try:
        return await work
    except CategoryError as exc:
        raise HTTPException(400, str(exc)) from None


@router.get("/categories")
async def categories(include_retired: bool = False,
                     _who: Principal = Depends(ADMIN),  # noqa: B008
                     svc: CategoryService = Depends(get_categories)) -> list[dict[str, Any]]:  # noqa: B008
    return await svc.categories(include_retired=include_retired)


@router.post("/categories", status_code=201)
async def add_category(body: NewCategory, who: Principal = Depends(ADMIN),  # noqa: B008
                       svc: CategoryService = Depends(get_categories)) -> dict[str, Any]:  # noqa: B008
    return await _run(svc.add(body.name, actor=who.login, description=body.description,
                              keywords=body.keywords))


@router.patch("/categories/{ref}")
async def change_category(ref: str, body: CategoryChange,
                          who: Principal = Depends(ADMIN),  # noqa: B008
                          svc: CategoryService = Depends(get_categories)) -> dict[str, Any]:  # noqa: B008
    return await _run(svc.change(ref, actor=who.login, name=body.name,
                                 description=body.description, keywords=body.keywords))


@router.delete("/categories/{ref}")
async def retire_category(ref: str, who: Principal = Depends(ADMIN),  # noqa: B008
                          svc: CategoryService = Depends(get_categories)) -> dict[str, Any]:  # noqa: B008
    return await _run(svc.retire(ref, actor=who.login))


@router.post("/data-sources/{ds}/categories/from-schemas", status_code=201)
async def from_schemas(ds: str, who: Principal = Depends(ADMIN),  # noqa: B008
                       svc: CategoryService = Depends(get_categories)) -> list[dict[str, Any]]:  # noqa: B008
    return await _run(svc.from_schemas(ds, actor=who.login))


@router.post("/data-sources/{ds}/categories/propose")
async def propose(ds: str, who: Principal = Depends(ADMIN),  # noqa: B008
                  svc: CategoryService = Depends(get_categories)) -> dict[str, Any]:  # noqa: B008
    return await _run(svc.propose(ds, actor=who.login))


@router.get("/data-sources/{ds}/objects")
async def objects(ds: str, _who: Principal = Depends(ADMIN),  # noqa: B008
                  svc: CategoryService = Depends(get_categories)) -> list[dict[str, Any]]:  # noqa: B008
    return await _run(svc.objects(ds))


@router.put("/data-sources/{ds}/objects/{table_id}/category")
async def place(ds: str, table_id: uuid.UUID, body: Placement,
                who: Principal = Depends(ADMIN),  # noqa: B008
                svc: CategoryService = Depends(get_categories)) -> dict[str, Any]:  # noqa: B008
    return await _run(svc.assign(ds, table_id, body.category, actor=who.login, source="admin",
                                 note=body.note))


@router.get("/category-suggestions")
async def suggestions(_who: Principal = Depends(ADMIN),  # noqa: B008
                      svc: CategoryService = Depends(get_categories)) -> list[dict[str, Any]]:  # noqa: B008
    return await svc.role_suggestions()
