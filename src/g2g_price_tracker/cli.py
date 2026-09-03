import argparse
import os
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

from .config import DEFAULT_G2G_URL, DEFAULT_SELLER
from .database import PriceRepository
from .exporting import export_price_history_xlsx
from .models import PriceObservation
from .scraper import TrackerConfig, collect_price


def _settings() -> tuple[TrackerConfig, PriceRepository]:
    load_dotenv()
    config = TrackerConfig(
        url=os.getenv("G2G_URL", DEFAULT_G2G_URL),
        seller=os.getenv("G2G_SELLER", DEFAULT_SELLER),
    )
    repository = PriceRepository(os.getenv("TRACKER_DATABASE", "data/prices.db"))
    return config, repository


def _collect_once(config: TrackerConfig, repository: PriceRepository) -> None:
    observation = collect_price(config)
    row_id = repository.add(observation)
    print(
        f"Saved #{row_id}: {observation.seller} {observation.unit_price} "
        f"{observation.currency} at {observation.observed_at.isoformat()}"
    )


def _seed(repository: PriceRepository) -> None:
    now = datetime.now(UTC)
    sample_prices = ["0.041", "0.039", "0.036", "0.034", "0.032"]
    for days_ago, price in reversed(list(enumerate(sample_prices))):
        repository.add(
            PriceObservation(
                target_key="demo-target",
                seller=DEFAULT_SELLER,
                listing_title="Demo listing",
                category="demo",
                unit_price=Decimal(price),
                currency="USD",
                source_url="demo://sample-data",
                observed_at=now - timedelta(days=days_ago),
            )
        )
    print(f"Added {len(sample_prices)} demo observations.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Track a seller price from a G2G category")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("collect", help="Collect and save one live price")
    watch = commands.add_parser("watch", help="Collect repeatedly")
    watch.add_argument("--interval-minutes", type=int, default=10, help="Minutes between checks")
    commands.add_parser("seed", help="Insert sample price history for development")
    export = commands.add_parser("export", help="Export price checks to Excel")
    export.add_argument("--output", type=Path, default=Path("data/prices.xlsx"))
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config, repository = _settings()
    if args.command == "collect":
        _collect_once(config, repository)
    elif args.command == "watch":
        if args.interval_minutes < 1:
            raise SystemExit("Interval must be at least one minute.")
        while True:
            _collect_once(config, repository)
            time.sleep(args.interval_minutes * 60)
    elif args.command == "seed":
        _seed(repository)
    elif args.command == "export":
        print(export_price_history_xlsx(args.output, repository.rows()))


if __name__ == "__main__":
    main()
