"""Enum types shared by the ORM models."""

from __future__ import annotations

import enum


class TcgEnum(str, enum.Enum):
    """Trading-card games supported by the catalog."""

    pokemon = "pokemon"
    magic = "magic"
    yugioh = "yugioh"
    onepiece = "onepiece"
    lorcana = "lorcana"
    sports = "sports"


class GradeHouseEnum(str, enum.Enum):
    """Recognised grading houses (Loupe is our first-party grade)."""

    psa = "psa"
    cgc = "cgc"
    bgs = "bgs"
    sgc = "sgc"
    tag = "tag"
    loupe = "loupe"


class RawConditionEnum(str, enum.Enum):
    """PSA-style condition grades for RAW (ungraded) cards.

    Optional on every :class:`~app.models.grade.GradedCard` row — only
    meaningful when the card has not been slabbed by a third-party grading
    house. Mirrors the standard TCG/sports vocabulary collectors and
    eBay use so per-condition pricing maps directly.
    """

    nm = "nm"  # Near Mint
    lp = "lp"  # Lightly Played
    mp = "mp"  # Moderately Played
    hp = "hp"  # Heavily Played
    dmg = "dmg"  # Damaged


class ScanStatusEnum(str, enum.Enum):
    """Lifecycle states of a :class:`~app.models.scan.ScanJob`."""

    queued = "queued"
    uploading = "uploading"
    processing = "processing"
    complete = "complete"
    failed = "failed"


class ScanSourceEnum(str, enum.Enum):
    """Origin of a scan job."""

    scanner = "scanner"
    phone = "phone"


class ScannerTransportEnum(str, enum.Enum):
    """Transport protocol used by a scanner device."""

    ble = "ble"
    wifi = "wifi"
    offline = "offline"


class PriceSourceEnum(str, enum.Enum):
    """Origin of a price snapshot."""

    ebay = "ebay"
    tcgplayer = "tcgplayer"
    pricecharting = "pricecharting"
    sci = "sci"
    manual = "manual"


class PriceAlertCondition(str, enum.Enum):
    """Trigger condition for a :class:`~app.models.price_alert.PriceAlert`."""

    above = "above"
    below = "below"


class ReportPeriodEnum(str, enum.Enum):
    """Window covered by a :class:`~app.models.user_report.UserReport`."""

    monthly = "monthly"
    yearly = "yearly"


class ReportStatusEnum(str, enum.Enum):
    """Lifecycle of a generated portfolio statement PDF."""

    pending = "pending"
    ready = "ready"
    failed = "failed"


__all__ = [
    "GradeHouseEnum",
    "PriceAlertCondition",
    "PriceSourceEnum",
    "ReportPeriodEnum",
    "ReportStatusEnum",
    "ScanSourceEnum",
    "ScanStatusEnum",
    "ScannerTransportEnum",
    "TcgEnum",
]
