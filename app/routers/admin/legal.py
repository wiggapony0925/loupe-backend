"""Admin legal-document management (`/v1/admin/legal`) — the "Law" portal page.

Live control over every published legal document: the checked-in JSON corpus
merged with the operator's kv_cache overrides (edit / retire / restore / add —
no deploy, no migration), plus the shared entity block whose values interpolate
through all of them.

Every mutation is audit-logged with the acting admin, because "who changed the
arbitration clause, and when" is a question you only get asked once, at the
worst possible moment.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_admin
from app.db import get_db
from app.models.user import User
from app.schemas.legal import (
    AdminLegalView,
    LegalDocument,
    LegalDocumentRead,
    LegalEntityUpdate,
    LegalOverrides,
)
from app.services import audit_service
from app.services.legal import legal_registry

router = APIRouter(prefix="/legal", tags=["admin-legal"])


def _view(overrides: LegalOverrides) -> AdminLegalView:
    return AdminLegalView(
        entity=legal_registry.merged_entity(overrides),
        fileEntity=dict(legal_registry.FILE_CORPUS.entity),
        documents=legal_registry.merged_documents(overrides),
        dirty=legal_registry.is_dirty(overrides),
        updatedAt=overrides.updated_at,
        updatedBy=overrides.updated_by,
    )


@router.get(
    "",
    response_model=AdminLegalView,
    summary="Legal corpus — checked-in file + live operator overrides",
)
async def legal_overview() -> AdminLegalView:
    return _view(await legal_registry.get_overrides())


@router.get(
    "/unresolved",
    response_model=list[str],
    summary="Placeholders used in the copy but missing from the entity block",
)
async def legal_unresolved() -> list[str]:
    return legal_registry.unresolved_tokens(await legal_registry.get_overrides())


@router.get(
    "/preview/{slug}",
    response_model=LegalDocumentRead,
    summary="Render one document exactly as a reader would see it",
)
async def legal_preview(slug: str) -> LegalDocumentRead:
    overrides = await legal_registry.get_overrides()
    entity = legal_registry.merged_entity(overrides)
    for doc in legal_registry.merged_documents(overrides):
        if doc.slug == slug:
            return legal_registry.render(doc, entity)
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail="Unknown legal document"
    )


@router.put(
    "/entity",
    response_model=AdminLegalView,
    summary="Update the shared entity block (company, jurisdiction, contacts)",
)
async def update_entity(
    payload: LegalEntityUpdate,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminLegalView:
    overrides = await legal_registry.put_entity(payload.entity, actor=admin.email)
    await audit_service.record(
        db,
        request=request,
        user=admin,
        action="legal.entity.update",
        target_table="legal",
        target_id="entity",
        payload={"keys": sorted(payload.entity)},
    )
    return _view(overrides)


@router.put(
    "/{slug}",
    response_model=AdminLegalView,
    summary="Publish an edited (or new) legal document",
)
async def put_document(
    slug: str,
    payload: LegalDocument,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminLegalView:
    if payload.slug != slug:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Body slug does not match the path",
        )
    overrides = await legal_registry.put_document(payload, actor=admin.email)
    await audit_service.record(
        db,
        request=request,
        user=admin,
        action="legal.document.publish",
        target_table="legal",
        target_id=slug,
        payload={
            "title": payload.title,
            "updated": payload.updated,
            "sections": len(payload.sections),
        },
    )
    return _view(overrides)


@router.post(
    "/{slug}/reset",
    response_model=AdminLegalView,
    summary="Restore a document to its checked-in text",
)
async def reset_document(
    slug: str,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminLegalView:
    try:
        overrides = await legal_registry.reset_document(slug, actor=admin.email)
    except legal_registry.UnknownDocumentError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No checked-in document with that slug",
        ) from exc
    await audit_service.record(
        db,
        request=request,
        user=admin,
        action="legal.document.reset",
        target_table="legal",
        target_id=slug,
    )
    return _view(overrides)


@router.delete(
    "/{slug}",
    response_model=AdminLegalView,
    summary="Retire a document (file documents are tombstoned, restorable)",
)
async def remove_document(
    slug: str,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminLegalView:
    try:
        overrides = await legal_registry.remove_document(slug, actor=admin.email)
    except legal_registry.UnknownDocumentError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown legal document"
        ) from exc
    await audit_service.record(
        db,
        request=request,
        user=admin,
        action="legal.document.retire",
        target_table="legal",
        target_id=slug,
    )
    return _view(overrides)


@router.post(
    "/reset",
    response_model=AdminLegalView,
    summary="Discard every operator edit — back to the checked-in corpus",
)
async def reset_all(
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminLegalView:
    overrides = await legal_registry.reset_all(actor=admin.email)
    await audit_service.record(
        db,
        request=request,
        user=admin,
        action="legal.reset_all",
        target_table="legal",
    )
    return _view(overrides)
