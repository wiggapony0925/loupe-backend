"""SQLAlchemy ORM models for the loupe-backend domain."""

from app.models.api_key import ApiKey
from app.models.audit import AuditLog
from app.models.blog import BlogPost
from app.models.card import Card, CardSet
from app.models.card_external_ref import CardExternalRef
from app.models.career import ApplicationEvent, JobApplication, JobPosting
from app.models.catalog_hash import CatalogImageHash
from app.models.catalog_mirror import CatalogMirrorCard, CatalogMirrorSet
from app.models.collection import Collection, CollectionItem
from app.models.email_log import EmailLog
from app.models.enums import (
    ApplicationStatusEnum,
    BlogStatusEnum,
    EmploymentTypeEnum,
    GradeHouseEnum,
    JobStatusEnum,
    PriceAlertCondition,
    PriceSourceEnum,
    RawConditionEnum,
    ReportPeriodEnum,
    ReportStatusEnum,
    ScannerTransportEnum,
    ScanSourceEnum,
    ScanStatusEnum,
    SealedProductTypeEnum,
    TcgEnum,
    WaitlistStatusEnum,
)
from app.models.feature_flag import FeatureFlag
from app.models.fingerprint import Fingerprint
from app.models.grade import GradedCard
from app.models.identification import CardIdentification, IdentificationFeedback
from app.models.kv_cache import KvCacheEntry
from app.models.price import PriceSnapshot
from app.models.price_alert import PriceAlert
from app.models.push_token import PushToken
from app.models.scan import ScanJob
from app.models.scanner import Scanner
from app.models.sealed import SealedHolding, SealedProduct
from app.models.site_config import SiteConfig
from app.models.user import User, UserSettings
from app.models.user_recents import UserRecents
from app.models.user_report import UserReport
from app.models.waitlist import WaitlistEntry
from app.models.watchlist import WatchlistItem

__all__ = [
    "ApiKey",
    "ApplicationEvent",
    "ApplicationStatusEnum",
    "AuditLog",
    "BlogPost",
    "BlogStatusEnum",
    "Card",
    "CardExternalRef",
    "CardIdentification",
    "CardSet",
    "CatalogImageHash",
    "CatalogMirrorCard",
    "CatalogMirrorSet",
    "Collection",
    "CollectionItem",
    "EmailLog",
    "EmploymentTypeEnum",
    "FeatureFlag",
    "Fingerprint",
    "GradeHouseEnum",
    "GradedCard",
    "IdentificationFeedback",
    "JobApplication",
    "JobPosting",
    "JobStatusEnum",
    "KvCacheEntry",
    "PriceAlert",
    "PriceAlertCondition",
    "PriceSnapshot",
    "PriceSourceEnum",
    "PushToken",
    "RawConditionEnum",
    "ReportPeriodEnum",
    "ReportStatusEnum",
    "ScanJob",
    "ScanSourceEnum",
    "ScanStatusEnum",
    "Scanner",
    "ScannerTransportEnum",
    "SealedHolding",
    "SealedProduct",
    "SealedProductTypeEnum",
    "SiteConfig",
    "TcgEnum",
    "User",
    "UserRecents",
    "UserReport",
    "UserSettings",
    "WaitlistEntry",
    "WaitlistStatusEnum",
    "WatchlistItem",
]
