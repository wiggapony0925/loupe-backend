"""Public legal documents — ``/v1/public/legal/*``.

Open to everyone, signed in or not: a Terms of Service you have to
authenticate to read is not a Terms of Service. Bodies are Markdown with every
``{{placeholder}}`` already resolved, so a client only has to render.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, status

from app.schemas.legal import LegalDocumentRead, LegalIndex
from app.services.legal import legal_registry

router = APIRouter(prefix="/public/legal", tags=["public"])

#: Legal copy changes a few times a year. Cache hard at the edge, but keep it
#: revalidating so a portal publish reaches readers within the hour.
_CACHE_CONTROL = "public, max-age=600, stale-while-revalidate=3600"


@router.get(
    "",
    response_model=LegalIndex,
    summary="Every published legal document (index)",
)
async def legal_index(response: Response) -> LegalIndex:
    documents, updated = await legal_registry.index()
    response.headers["Cache-Control"] = _CACHE_CONTROL
    return LegalIndex(documents=documents, updated=updated)


@router.get(
    "/{slug}",
    response_model=LegalDocumentRead,
    summary="One legal document, rendered",
)
async def legal_document(slug: str, response: Response) -> LegalDocumentRead:
    doc = await legal_registry.published(slug)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown legal document"
        )
    response.headers["Cache-Control"] = _CACHE_CONTROL
    return doc
