"""Scanner CRUD endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_user
from app.db import get_db
from app.models.user import User
from app.schemas.scanner import (
    ScannerCreate,
    ScannerHeartbeat,
    ScannerRead,
    ScannerUpdate,
)
from app.services import scanner_service

router = APIRouter(prefix="/scanners", tags=["scanners"])


@router.get("", response_model=list[ScannerRead], summary="List my scanners")
async def list_scanners(
    user: User = Depends(require_user), db: AsyncSession = Depends(get_db)
) -> list[ScannerRead]:
    rows = await scanner_service.list_for_user(db, user)
    return [ScannerRead.model_validate(r) for r in rows]


@router.get(
    "/status",
    summary="Most-recently-active scanner status",
    description=(
        "Returns the scanner with the most-recent `last_seen_at` for the "
        "signed-in user, or `null` when the user has never paired one. "
        "Frontend uses this to render the 'Scanner connection' widget on "
        "the Command Center without requiring the user to pick a specific "
        "device id."
    ),
)
async def get_status(
    user: User = Depends(require_user), db: AsyncSession = Depends(get_db)
) -> ScannerRead | None:
    rows = await scanner_service.list_for_user(db, user)
    if not rows:
        return None
    # Prefer rows with a heartbeat; fall back to most-recently-paired.
    rows.sort(
        key=lambda r: (r.last_seen_at or r.created_at,),
        reverse=True,
    )
    return ScannerRead.model_validate(rows[0])


@router.post(
    "",
    response_model=ScannerRead,
    status_code=status.HTTP_201_CREATED,
    summary="Pair a scanner",
)
async def pair_scanner(
    payload: ScannerCreate,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> ScannerRead:
    scanner = await scanner_service.pair(db, user, payload)
    return ScannerRead.model_validate(scanner)


@router.get("/{scanner_id}", response_model=ScannerRead, summary="Get one scanner")
async def get_scanner(
    scanner_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> ScannerRead:
    scanner = await scanner_service.get_for_user(db, user, scanner_id)
    if scanner is None:
        raise HTTPException(status_code=404, detail="Scanner not found")
    return ScannerRead.model_validate(scanner)


@router.patch("/{scanner_id}", response_model=ScannerRead, summary="Update scanner")
async def update_scanner(
    scanner_id: uuid.UUID,
    payload: ScannerUpdate,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> ScannerRead:
    scanner = await scanner_service.update(db, user, scanner_id, payload)
    if scanner is None:
        raise HTTPException(status_code=404, detail="Scanner not found")
    return ScannerRead.model_validate(scanner)


@router.delete("/{scanner_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_scanner(
    scanner_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    deleted = await scanner_service.delete(db, user, scanner_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Scanner not found")


@router.post(
    "/{scanner_id}/heartbeat",
    response_model=ScannerRead,
    summary="Record a scanner heartbeat",
)
async def heartbeat(
    scanner_id: uuid.UUID,
    payload: ScannerHeartbeat,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> ScannerRead:
    scanner = await scanner_service.record_heartbeat(db, user, scanner_id, payload)
    if scanner is None:
        raise HTTPException(status_code=404, detail="Scanner not found")
    return ScannerRead.model_validate(scanner)


__all__ = ["router"]
