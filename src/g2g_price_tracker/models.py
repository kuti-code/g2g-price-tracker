from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


def canonical_source_url(source_url: str) -> str:
    """Return a stable representation so equivalent G2G URLs share one history."""
    parsed = urlparse(source_url.strip())
    normalized_query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/"),
            "",
            normalized_query,
            "",
        )
    )


def build_target_key(seller: str, source_url: str) -> str:
    """Build a case-insensitive identifier for one seller and one market URL."""
    identity = f"{seller.strip().casefold()}\n{canonical_source_url(source_url)}"
    return sha256(identity.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PriceStatistics:
    latest: Decimal
    lowest: Decimal
    highest: Decimal
    all_time_average: Decimal
    latest_change_percent: Decimal | None


def calculate_price_statistics(prices: list[Decimal]) -> PriceStatistics:
    if not prices:
        raise ValueError("At least one price is required.")

    previous = prices[-2] if len(prices) >= 2 else None
    change = (
        (prices[-1] - previous) / previous * 100 if previous is not None and previous != 0 else None
    )
    return PriceStatistics(
        latest=prices[-1],
        lowest=min(prices),
        highest=max(prices),
        all_time_average=sum(prices) / len(prices),
        latest_change_percent=change,
    )


@dataclass(frozen=True, slots=True)
class PriceObservation:
    target_key: str
    seller: str
    listing_title: str
    category: str
    unit_price: Decimal
    currency: str
    source_url: str
    observed_at: datetime
    market_lowest_price: Decimal | None = None
    market_lowest_seller: str | None = None
    market_average_price: Decimal | None = None

    @classmethod
    def now(
        cls,
        *,
        seller: str,
        listing_title: str,
        category: str,
        unit_price: Decimal,
        currency: str,
        source_url: str,
        market_lowest_price: Decimal | None = None,
        market_lowest_seller: str | None = None,
        market_average_price: Decimal | None = None,
    ) -> "PriceObservation":
        return cls(
            target_key=build_target_key(seller, source_url),
            seller=seller,
            listing_title=listing_title,
            category=category,
            unit_price=unit_price,
            currency=currency,
            source_url=canonical_source_url(source_url),
            observed_at=datetime.now(UTC),
            market_lowest_price=market_lowest_price,
            market_lowest_seller=market_lowest_seller,
            market_average_price=market_average_price,
        )
