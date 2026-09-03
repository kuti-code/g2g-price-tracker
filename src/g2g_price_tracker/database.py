import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from .models import PriceObservation, PriceStatistics

CHART_ROW_LIMIT = 300
TABLE_ROW_LIMIT = 100
_CHART_COLUMNS = (
    "id, observed_at, unit_price, market_lowest_price, market_average_price, "
    "currency, listing_title, market_lowest_seller"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS price_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_key TEXT NOT NULL,
    seller TEXT NOT NULL,
    listing_title TEXT NOT NULL,
    category TEXT NOT NULL,
    unit_price TEXT NOT NULL,
    currency TEXT NOT NULL,
    source_url TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    market_lowest_price TEXT,
    market_lowest_seller TEXT,
    market_average_price TEXT
);
CREATE INDEX IF NOT EXISTS idx_price_target_time
ON price_observations(target_key, observed_at);
"""


class PriceRepository:
    def __init__(self, database_path: str | Path) -> None:
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._schema_ready = False

    def initialize(self) -> None:
        if self._schema_ready:
            return
        with self._connection(initialize=False) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.executescript(SCHEMA)
            existing_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(price_observations)")
            }
            if "market_lowest_price" not in existing_columns:
                connection.execute(
                    "ALTER TABLE price_observations ADD COLUMN market_lowest_price TEXT"
                )
            if "market_lowest_seller" not in existing_columns:
                connection.execute(
                    "ALTER TABLE price_observations ADD COLUMN market_lowest_seller TEXT"
                )
            if "market_average_price" not in existing_columns:
                connection.execute(
                    "ALTER TABLE price_observations ADD COLUMN market_average_price TEXT"
                )
        self._schema_ready = True

    @contextmanager
    def _connection(self, *, initialize: bool = True) -> Iterator[sqlite3.Connection]:
        if initialize:
            self.initialize()
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def add(self, observation: PriceObservation) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO price_observations
                (target_key, seller, listing_title, category, unit_price, currency,
                 source_url, observed_at, market_lowest_price, market_lowest_seller,
                 market_average_price)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.target_key,
                    observation.seller,
                    observation.listing_title,
                    observation.category,
                    str(observation.unit_price),
                    observation.currency,
                    observation.source_url,
                    observation.observed_at.isoformat(),
                    (
                        str(observation.market_lowest_price)
                        if observation.market_lowest_price is not None
                        else None
                    ),
                    observation.market_lowest_seller,
                    (
                        str(observation.market_average_price)
                        if observation.market_average_price is not None
                        else None
                    ),
                ),
            )
            return int(cursor.lastrowid)

    def rows(self, target_key: str | None = None) -> list[sqlite3.Row]:
        query = "SELECT * FROM price_observations"
        parameters: list[object] = []
        if target_key:
            query += " WHERE target_key = ?"
            parameters.append(target_key)
        query += " ORDER BY observed_at ASC, id ASC"
        with self._connection() as connection:
            return list(connection.execute(query, parameters).fetchall())

    def count(self, target_key: str) -> int:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM price_observations WHERE target_key = ?",
                (target_key,),
            ).fetchone()
            return int(row[0]) if row else 0

    def recent_rows(self, target_key: str, limit: int = TABLE_ROW_LIMIT) -> list[sqlite3.Row]:
        """Newest first, capped for the on-screen table."""
        with self._connection() as connection:
            return list(
                connection.execute(
                    f"SELECT {_CHART_COLUMNS} FROM price_observations "
                    "WHERE target_key = ? ORDER BY observed_at DESC, id DESC LIMIT ?",
                    (target_key, limit),
                ).fetchall()
            )

    def target_summary(self, target_key: str) -> dict[str, object] | None:
        with self._connection() as connection:
            aggregates = connection.execute(
                """
                SELECT COUNT(*) AS count,
                       MIN(CAST(unit_price AS REAL)) AS lowest,
                       MAX(CAST(unit_price AS REAL)) AS highest,
                       AVG(CAST(unit_price AS REAL)) AS average
                FROM price_observations
                WHERE target_key = ?
                """,
                (target_key,),
            ).fetchone()
            if not aggregates or int(aggregates["count"]) == 0:
                return None
            latest_rows = connection.execute(
                f"SELECT {_CHART_COLUMNS} FROM price_observations "
                "WHERE target_key = ? ORDER BY observed_at DESC, id DESC LIMIT 2",
                (target_key,),
            ).fetchall()
        latest = latest_rows[0]
        previous = latest_rows[1] if len(latest_rows) > 1 else None
        latest_price = Decimal(str(latest["unit_price"]))
        previous_price = Decimal(str(previous["unit_price"])) if previous is not None else None
        change = (
            (latest_price - previous_price) / previous_price * 100
            if previous_price not in (None, 0)
            else None
        )
        return {
            "count": int(aggregates["count"]),
            "statistics": PriceStatistics(
                latest=latest_price,
                lowest=Decimal(str(aggregates["lowest"])),
                highest=Decimal(str(aggregates["highest"])),
                all_time_average=Decimal(str(aggregates["average"])),
                latest_change_percent=change,
            ),
            "currency": latest["currency"],
            "listing_title": latest["listing_title"],
            "market_lowest_price": latest["market_lowest_price"],
            "market_lowest_seller": latest["market_lowest_seller"],
            "market_average_price": latest["market_average_price"],
        }

    def rows_for_chart(
        self,
        target_key: str,
        range_name: str,
        *,
        now: datetime | None = None,
        limit: int = CHART_ROW_LIMIT,
    ) -> list[sqlite3.Row]:
        current = now or datetime.now(UTC)
        with self._connection() as connection:
            if range_name == "Last 100 Checks":
                rows = list(
                    connection.execute(
                        f"SELECT {_CHART_COLUMNS} FROM price_observations "
                        "WHERE target_key = ? ORDER BY observed_at DESC, id DESC LIMIT 100",
                        (target_key,),
                    ).fetchall()
                )
                rows.reverse()
                return rows
            query = "SELECT id FROM price_observations WHERE target_key = ?"
            parameters: list[object] = [target_key]
            periods = {
                "Last 24 Hours": timedelta(hours=24),
                "Last 7 Days": timedelta(days=7),
                "Last 30 Days": timedelta(days=30),
            }
            period = periods.get(range_name)
            if period is not None:
                query += " AND observed_at >= ?"
                parameters.append((current - period).isoformat())
            query += " ORDER BY observed_at ASC, id ASC"
            ids = [int(row[0]) for row in connection.execute(query, parameters).fetchall()]
            if not ids:
                return []
            if len(ids) > limit:
                indexes = {round(index * (len(ids) - 1) / (limit - 1)) for index in range(limit)}
                ids = [ids[index] for index in sorted(indexes)]
            placeholders = ",".join("?" * len(ids))
            return list(
                connection.execute(
                    f"SELECT {_CHART_COLUMNS} FROM price_observations "
                    f"WHERE id IN ({placeholders}) ORDER BY observed_at ASC, id ASC",
                    ids,
                ).fetchall()
            )

    def delete_target(self, target_key: str) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM price_observations WHERE target_key = ?",
                (target_key,),
            )
            return int(cursor.rowcount)
