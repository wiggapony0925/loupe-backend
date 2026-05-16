"""Pydantic v2 schemas for the loupe-backend API surface."""

from app.schemas.auth import (
    AppleSignInRequest,
    GoogleSignInRequest,
    RefreshRequest,
    TokenPair,
)
from app.schemas.card import CardRead, CardSearchQuery, CardSetRead
from app.schemas.collection import (
    CollectionCreate,
    CollectionItemAdd,
    CollectionRead,
    CollectionUpdate,
)
from app.schemas.common import ErrorEnvelope, Pagination
from app.schemas.grade import GradedCardCreate, GradedCardRead, GradedCardUpdate
from app.schemas.price import PriceQuery, PriceSnapshotRead
from app.schemas.scan import (
    ALL_ANGLES,
    PresignedUpload,
    ScanAngle,
    ScanJobCompleteRequest,
    ScanJobCreate,
    ScanJobCreateResponse,
    ScanJobRead,
    ScanProgressEvent,
)
from app.schemas.scanner import (
    ScannerCreate,
    ScannerHeartbeat,
    ScannerRead,
    ScannerUpdate,
)
from app.schemas.user import UserRead, UserSettingsRead, UserSettingsUpdate, UserUpdate

__all__ = [
    "ALL_ANGLES",
    "AppleSignInRequest",
    "CardRead",
    "CardSearchQuery",
    "CardSetRead",
    "CollectionCreate",
    "CollectionItemAdd",
    "CollectionRead",
    "CollectionUpdate",
    "ErrorEnvelope",
    "GoogleSignInRequest",
    "GradedCardCreate",
    "GradedCardRead",
    "GradedCardUpdate",
    "Pagination",
    "PresignedUpload",
    "PriceQuery",
    "PriceSnapshotRead",
    "RefreshRequest",
    "ScanAngle",
    "ScanJobCompleteRequest",
    "ScanJobCreate",
    "ScanJobCreateResponse",
    "ScanJobRead",
    "ScanProgressEvent",
    "ScannerCreate",
    "ScannerHeartbeat",
    "ScannerRead",
    "ScannerUpdate",
    "TokenPair",
    "UserRead",
    "UserSettingsRead",
    "UserSettingsUpdate",
    "UserUpdate",
]
