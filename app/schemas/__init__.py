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
from app.schemas.envelope import (
    API_VERSION,
    Envelope,
    ErrorDetail,
    Meta,
    build_meta,
    build_pagination,
    fail,
    ok,
    page,
)
from app.schemas.envelope import Pagination as EnvelopePagination
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
    "API_VERSION",
    "AppleSignInRequest",
    "CardRead",
    "CardSearchQuery",
    "CardSetRead",
    "CollectionCreate",
    "CollectionItemAdd",
    "CollectionRead",
    "CollectionUpdate",
    "Envelope",
    "EnvelopePagination",
    "ErrorDetail",
    "ErrorEnvelope",
    "GoogleSignInRequest",
    "GradedCardCreate",
    "GradedCardRead",
    "GradedCardUpdate",
    "Meta",
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
    "build_meta",
    "build_pagination",
    "fail",
    "ok",
    "page",
]
