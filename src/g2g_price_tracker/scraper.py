import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from .errors import PriceNotFoundError
from .models import PriceObservation

G2G_API_URL = "https://sls.g2g.com/v3/offer/search"
API_SUCCESS_CODE = 2000
REQUEST_TIMEOUT_SECONDS = 20


@dataclass(frozen=True, slots=True)
class TrackerConfig:
    url: str
    seller: str


class CollectionError(RuntimeError):
    """Raised when G2G data cannot be collected, with retry guidance."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


def _search_parameters(source_url: str, seller: str | None) -> dict[str, str | int]:
    """Translate the filters in a public G2G category URL to its JSON request."""
    parsed = urlparse(source_url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (hostname == "g2g.com" or hostname.endswith(".g2g.com")):
        raise ValueError("The source must be a valid HTTPS G2G URL.")

    normalized_seller = seller.strip() if seller is not None else ""
    if seller is not None and not normalized_seller:
        raise ValueError("Seller name cannot be empty.")

    path_parts = [part for part in parsed.path.split("/") if part]
    try:
        seo_term = path_parts[path_parts.index("categories") + 1]
    except (ValueError, IndexError) as exc:
        raise ValueError("The G2G category could not be determined from this URL.") from exc

    query = parse_qs(parsed.query)
    parameters: dict[str, str | int] = {
        "seo_term": seo_term,
        "filter_attr": query.get("fa", [""])[0],
        "sort": query.get("sort", ["lowest_price"])[0],
        "page": 1,
        "page_size": 1,
        "group": "0",
        "currency": "USD",
        "country": "TR",
        "v": "v2",
    }
    if normalized_seller:
        parameters["seller"] = normalized_seller
    return {key: value for key, value in parameters.items() if value != ""}


def _request_json(url: str, timeout_seconds: int = REQUEST_TIMEOUT_SECONDS) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            raw_body = response.read()
    except HTTPError as exc:
        if exc.code == 429:
            retry_after_header = exc.headers.get("Retry-After") if exc.headers else None
            try:
                retry_after = float(retry_after_header) if retry_after_header else 60.0
            except ValueError:
                retry_after = 60.0
            raise CollectionError(
                "G2G reported too many requests (HTTP 429). "
                "Increase the tracking interval and try again later.",
                retryable=True,
                retry_after=retry_after,
            ) from exc
        if exc.code in {401, 403}:
            raise CollectionError(
                f"G2G rejected the data request (HTTP {exc.code}). "
                "Try again later without bypassing the access control."
            ) from exc
        raise CollectionError(
            f"The G2G data service returned HTTP {exc.code}.",
            retryable=500 <= exc.code < 600,
        ) from exc
    except (URLError, TimeoutError) as exc:
        raise CollectionError(
            f"Could not connect to the G2G data service: {exc}",
            retryable=True,
        ) from exc

    try:
        data = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CollectionError("The G2G data service did not return valid JSON.") from exc
    if not isinstance(data, dict):
        raise CollectionError("The G2G data response has an unexpected format.")
    return data


def _api_error_message(data: dict[str, Any]) -> str:
    messages = data.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict) and message.get("text"):
                return str(message["text"])
            if isinstance(message, str) and message:
                return message
    return f"unexpected response code: {data.get('code', 'unknown')}"


def _find_exact_seller_offer(data: dict[str, Any], seller: str) -> dict[str, Any]:
    if data.get("code") != API_SUCCESS_CODE:
        raise CollectionError(f"The G2G data service returned an error: {_api_error_message(data)}")

    payload = data.get("payload")
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        raise CollectionError("The G2G offer list format may have changed.")

    requested = seller.strip().casefold()
    for offer in results:
        if isinstance(offer, dict) and str(offer.get("username", "")).casefold() == requested:
            return offer
    raise PriceNotFoundError(
        f"No offer was found for seller '{seller}'. Check the seller name and G2G filters."
    )


def _offer_results(data: dict[str, Any]) -> list[dict[str, Any]]:
    if data.get("code") != API_SUCCESS_CODE:
        raise CollectionError(f"The G2G data service returned an error: {_api_error_message(data)}")
    payload = data.get("payload")
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        raise CollectionError("The G2G offer list format may have changed.")
    if not results:
        raise CollectionError("No market offer was found for the selected G2G filters.")
    offers = [offer for offer in results if isinstance(offer, dict)]
    if not offers:
        raise CollectionError("No readable market offer was found for the selected filters.")
    return offers


def _first_offer(data: dict[str, Any]) -> dict[str, Any]:
    return _offer_results(data)[0]


def _offer_price(offer: dict[str, Any], *, label: str) -> Decimal:
    try:
        return Decimal(str(offer["display_price"]))
    except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
        raise CollectionError(f"The {label} price value could not be read.") from exc


def collect_price(config: TrackerConfig) -> PriceObservation:
    """Collect one seller observation through G2G's public frontend JSON endpoint."""
    seller_parameters = _search_parameters(config.url, config.seller)
    seller_api_url = f"{G2G_API_URL}?{urlencode(seller_parameters)}"
    offer = _find_exact_seller_offer(_request_json(seller_api_url), config.seller)
    price = _offer_price(offer, label="seller")

    market_parameters = _search_parameters(config.url, None)
    market_parameters["sort"] = "lowest_price"
    market_parameters["page_size"] = 5
    market_api_url = f"{G2G_API_URL}?{urlencode(market_parameters)}"
    market_offers = _offer_results(_request_json(market_api_url))[:5]
    market_offer = market_offers[0]
    market_lowest_price = _offer_price(market_offer, label="market-lowest")
    market_prices = [_offer_price(item, label="market-average") for item in market_offers]
    market_average_price = sum(market_prices, Decimal(0)) / len(market_prices)

    currency = str(offer.get("display_currency", "USD")).upper().strip()
    if not currency:
        raise CollectionError("The currency in the G2G offer could not be read.")

    title = str(offer.get("title") or offer.get("brand_name") or "G2G listing").strip()
    category = str(offer.get("seo_term") or seller_parameters["seo_term"]).strip()

    return PriceObservation.now(
        seller=str(offer.get("username") or config.seller),
        listing_title=title,
        category=category,
        unit_price=price,
        currency=currency,
        source_url=config.url,
        market_lowest_price=market_lowest_price,
        market_lowest_seller=str(market_offer.get("username") or "Unknown seller").strip(),
        market_average_price=market_average_price,
    )
