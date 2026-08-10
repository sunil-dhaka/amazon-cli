"""SQLite storage for the price watchlist.

Design notes that matter:

* **Every price is ``int`` paise.** Nothing here ever sees a float rupee, so
  ``lowest_paise`` after a thousand checks is exactly the smallest price seen --
  no drift, no ``24989.999999999996``.
* **Timestamps are epoch seconds (``int``).** sqlite3's datetime adapters are
  deprecated in 3.12 and lossy across timezones; an int is neither.
* **``PRAGMA foreign_keys`` is ON for every connection.** SQLite defaults it
  *off*, which silently turns ``ON DELETE CASCADE`` into a no-op and leaves
  orphaned history behind every removed product. It is re-asserted per
  connection because the pragma is a connection property, not a schema one.
* **A price point is appended only when the price actually changed.** A watchlist
  checked hourly for a year is ~8,700 rows per product if you store every check,
  and ~30 if you store transitions. The transition log is also the only form in
  which a time-weighted average means anything.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from amazon_cli import money
from amazon_cli.errors import AmzError, NotFoundError

#: Bumped whenever the schema changes; stored in ``PRAGMA user_version``.
SCHEMA_VERSION = 1

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS watched (
        asin              TEXT PRIMARY KEY,
        title             TEXT    NOT NULL DEFAULT '',
        brand             TEXT    NOT NULL DEFAULT '',
        image_url         TEXT    NOT NULL DEFAULT '',
        target_paise      INTEGER NOT NULL DEFAULT 0,
        current_paise     INTEGER NOT NULL DEFAULT 0,
        mrp_paise         INTEGER NOT NULL DEFAULT 0,
        lowest_paise      INTEGER NOT NULL DEFAULT 0,
        highest_paise     INTEGER NOT NULL DEFAULT 0,
        rating            REAL    NOT NULL DEFAULT 0,
        review_count      INTEGER NOT NULL DEFAULT 0,
        availability      TEXT    NOT NULL DEFAULT '',
        added_at          INTEGER NOT NULL,
        last_checked_at   INTEGER NOT NULL DEFAULT 0,
        last_success_at   INTEGER NOT NULL DEFAULT 0,
        last_error        TEXT    NOT NULL DEFAULT '',
        alerts_enabled    INTEGER NOT NULL DEFAULT 1,
        notified_at_paise INTEGER NOT NULL DEFAULT 0,
        note              TEXT    NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS price_points (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        asin        TEXT    NOT NULL,
        paise       INTEGER NOT NULL,
        recorded_at INTEGER NOT NULL,
        FOREIGN KEY(asin) REFERENCES watched(asin) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_price_points_asin_time
        ON price_points(asin, recorded_at)
    """,
)


def default_db_path() -> Path:
    """Where the watchlist lives.

    ``$AMZ_WATCH_DB`` wins outright (tests and one-off experiments depend on
    it), then ``$XDG_DATA_HOME/amz/watch.db``, then
    ``~/.local/share/amz/watch.db``.
    """
    override = os.environ.get("AMZ_WATCH_DB")
    if override:
        return Path(override).expanduser()
    root = os.environ.get("XDG_DATA_HOME")
    base = Path(root).expanduser() if root else Path.home() / ".local" / "share"
    return base / "amz" / "watch.db"


@dataclass(frozen=True)
class PricePoint:
    """One observed price transition."""

    asin: str
    paise: int
    recorded_at: int
    id: int = 0

    def to_dict(self) -> dict:
        return {
            "recorded_at": self.recorded_at,
            "price": money.rupees(self.paise),
            "price_paise": self.paise,
        }


@dataclass
class Watched:
    """A watched product. All money fields are paise."""

    asin: str
    title: str = ""
    brand: str = ""
    image_url: str = ""
    target_paise: int = 0
    current_paise: int = 0
    mrp_paise: int = 0
    lowest_paise: int = 0
    highest_paise: int = 0
    rating: float = 0.0
    review_count: int = 0
    availability: str = ""
    added_at: int = 0
    last_checked_at: int = 0
    last_success_at: int = 0
    last_error: str = ""
    alerts_enabled: bool = True
    notified_at_paise: int = 0
    note: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Watched":
        return cls(
            asin=row["asin"],
            title=row["title"],
            brand=row["brand"],
            image_url=row["image_url"],
            target_paise=row["target_paise"],
            current_paise=row["current_paise"],
            mrp_paise=row["mrp_paise"],
            lowest_paise=row["lowest_paise"],
            highest_paise=row["highest_paise"],
            rating=row["rating"],
            review_count=row["review_count"],
            availability=row["availability"],
            added_at=row["added_at"],
            last_checked_at=row["last_checked_at"],
            last_success_at=row["last_success_at"],
            last_error=row["last_error"],
            alerts_enabled=bool(row["alerts_enabled"]),
            notified_at_paise=row["notified_at_paise"],
            note=row["note"],
        )

    @property
    def below_target(self) -> bool:
        """True when the price is known and has met the target."""
        return bool(self.current_paise) and 0 < self.target_paise and self.current_paise <= self.target_paise

    @property
    def gap_paise(self) -> int:
        """How far above the target the price still is; 0 once it is met."""
        if not self.current_paise or not self.target_paise:
            return 0
        return max(0, self.current_paise - self.target_paise)

    @property
    def drop_percent(self) -> int:
        """Signed change from the highest price ever seen to the current one."""
        return money.change_percent(self.highest_paise, self.current_paise)

    @property
    def discount_percent(self) -> int:
        return money.discount_percent(self.current_paise, self.mrp_paise)

    def to_dict(self) -> dict:
        return {
            "asin": self.asin,
            "title": self.title,
            "brand": self.brand,
            "image_url": self.image_url,
            "target": money.rupees(self.target_paise),
            "target_paise": self.target_paise,
            "price": money.rupees(self.current_paise),
            "price_paise": self.current_paise,
            "mrp": money.rupees(self.mrp_paise),
            "mrp_paise": self.mrp_paise,
            "lowest": money.rupees(self.lowest_paise),
            "lowest_paise": self.lowest_paise,
            "highest": money.rupees(self.highest_paise),
            "highest_paise": self.highest_paise,
            "discount_percent": self.discount_percent,
            "drop_percent": self.drop_percent,
            "below_target": self.below_target,
            "gap": money.rupees(self.gap_paise),
            "gap_paise": self.gap_paise,
            "rating": self.rating,
            "review_count": self.review_count,
            "availability": self.availability,
            "added_at": self.added_at,
            "last_checked_at": self.last_checked_at,
            "last_success_at": self.last_success_at,
            "last_error": self.last_error,
            "alerts_enabled": self.alerts_enabled,
            "notified_at_paise": self.notified_at_paise,
            "note": self.note,
        }


class WatchStore:
    """The watchlist database.

    Usable as a context manager::

        with WatchStore() as store:
            store.add("B0BZP2H373", target_paise=2_499_00)
    """

    def __init__(self, path: str | Path | None = None, *, timeout: float = 10.0):
        self.path = Path(path) if path is not None else default_db_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # sqlite3.connect() is lazy: a file that is not a database only blows up
        # on the first statement. Everything up to and including the migration
        # therefore has to be inside the guard, or a hand-mangled watch.db
        # reaches the user as a raw sqlite3 traceback instead of a message.
        self._conn = sqlite3.connect(str(self.path), timeout=timeout)
        try:
            self._conn.row_factory = sqlite3.Row
            # Order matters: both pragmas must run outside any transaction, and
            # foreign_keys is a no-op if set while one is open.
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA busy_timeout = %d" % int(timeout * 1000))
            if str(self.path) != ":memory:":
                # WAL lets a `watch check` writing prices coexist with a `watch
                # list` reading them instead of one erroring out with "database
                # is locked".
                self._conn.execute("PRAGMA journal_mode = WAL")
            self._migrate()
        except sqlite3.DatabaseError as exc:
            # Never delete or truncate it -- it may be a database this build is
            # simply too old to read, or the only copy of someone's history.
            self._conn.close()
            raise AmzError(
                f"{self.path} is not a usable watchlist database ({exc}). "
                f"Move it aside and amz will create a fresh one."
            ) from exc
        except BaseException:
            self._conn.close()
            raise

    # -- lifecycle ---------------------------------------------------------

    def _migrate(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            return
        if version > SCHEMA_VERSION:
            raise AmzError(
                f"{self.path} was written by a newer amz (schema v{version}, "
                f"this build understands v{SCHEMA_VERSION}). Upgrade amz."
            )
        with self._conn:
            for statement in _SCHEMA:
                self._conn.execute(statement)
            # PRAGMA does not take bound parameters; the value is a constant.
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "WatchStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def connection(self) -> sqlite3.Connection:
        """Escape hatch for tests that need to assert on raw SQL state."""
        return self._conn

    # -- reads -------------------------------------------------------------

    def get(self, asin: str) -> Watched | None:
        row = self._conn.execute("SELECT * FROM watched WHERE asin = ?", (asin,)).fetchone()
        return Watched.from_row(row) if row else None

    def require(self, asin: str) -> Watched:
        """:meth:`get`, but raises :class:`NotFoundError` instead of returning None."""
        found = self.get(asin)
        if found is None:
            raise NotFoundError(f"{asin} is not on your watchlist. Add it with: amz watch add {asin} --target <RUPEES>")
        return found

    def list_all(self) -> list[Watched]:
        rows = self._conn.execute("SELECT * FROM watched ORDER BY added_at, asin").fetchall()
        return [Watched.from_row(r) for r in rows]

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM watched").fetchone()[0]

    def history(self, asin: str, limit: int | None = None) -> list[PricePoint]:
        """Price points oldest-first. ``limit`` keeps the *newest* N."""
        if limit is None:
            rows = self._conn.execute(
                "SELECT * FROM price_points WHERE asin = ? ORDER BY recorded_at, id",
                (asin,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM price_points WHERE asin = ? ORDER BY recorded_at DESC, id DESC LIMIT ?",
                (asin, limit),
            ).fetchall()
            rows = list(reversed(rows))
        return [
            PricePoint(asin=r["asin"], paise=r["paise"], recorded_at=r["recorded_at"], id=r["id"])
            for r in rows
        ]

    def all_histories(self) -> dict[str, list[int]]:
        """Every product's price series in one query -- avoids an N+1 for ``list``."""
        out: dict[str, list[int]] = {}
        for row in self._conn.execute(
            "SELECT asin, paise FROM price_points ORDER BY asin, recorded_at, id"
        ):
            out.setdefault(row["asin"], []).append(row["paise"])
        return out

    # -- writes ------------------------------------------------------------

    def add(
        self,
        asin: str,
        *,
        target_paise: int,
        note: str = "",
        now: int | None = None,
    ) -> tuple[Watched, bool]:
        """Insert or re-target a product. Returns ``(row, created)``.

        Re-adding an existing ASIN deliberately keeps ``added_at``, the price
        history and the lowest/highest marks: the user is adjusting a target,
        not starting over, and throwing away months of history because someone
        retyped a command would be indefensible. As with ``set-target``, a
        *changed* target re-arms the alert.
        """
        now = int(time.time()) if now is None else int(now)
        existing = self.get(asin)
        with self._conn:
            if existing is None:
                self._conn.execute(
                    "INSERT INTO watched (asin, target_paise, note, added_at, alerts_enabled) "
                    "VALUES (?, ?, ?, ?, 1)",
                    (asin, int(target_paise), note, now),
                )
            else:
                self._conn.execute(
                    "UPDATE watched SET target_paise = ?, note = ?, "
                    "notified_at_paise = CASE WHEN ? != target_paise THEN 0 ELSE notified_at_paise END "
                    "WHERE asin = ?",
                    (
                        int(target_paise),
                        note if note else existing.note,
                        int(target_paise),
                        asin,
                    ),
                )
        return self.require(asin), existing is None

    def remove(self, asin: str) -> bool:
        """Delete a product and (via the FK cascade) its history."""
        with self._conn:
            cur = self._conn.execute("DELETE FROM watched WHERE asin = ?", (asin,))
        return cur.rowcount > 0

    def clear(self) -> int:
        """Delete everything. Returns how many products were removed."""
        with self._conn:
            cur = self._conn.execute("DELETE FROM watched")
        return cur.rowcount

    def set_target(self, asin: str, target_paise: int) -> Watched:
        """Set the target, re-arming the alert when the value actually changes."""
        current = self.require(asin)
        target_paise = int(target_paise)
        if target_paise == current.target_paise:
            return current
        with self._conn:
            self._conn.execute(
                "UPDATE watched SET target_paise = ?, notified_at_paise = 0 WHERE asin = ?",
                (target_paise, asin),
            )
        return self.require(asin)

    def set_alerts(self, asin: str, enabled: bool) -> Watched:
        self.require(asin)
        with self._conn:
            self._conn.execute(
                "UPDATE watched SET alerts_enabled = ? WHERE asin = ?",
                (1 if enabled else 0, asin),
            )
        return self.require(asin)

    def set_note(self, asin: str, note: str) -> Watched:
        self.require(asin)
        with self._conn:
            self._conn.execute("UPDATE watched SET note = ? WHERE asin = ?", (note, asin))
        return self.require(asin)

    def set_notified(self, asin: str, paise: int) -> None:
        """Record the price we last alerted at (0 == re-armed)."""
        with self._conn:
            self._conn.execute(
                "UPDATE watched SET notified_at_paise = ? WHERE asin = ?",
                (int(paise), asin),
            )

    def append_price_point(self, asin: str, paise: int, recorded_at: int | None = None) -> bool:
        """Append a price point *only if the price moved*. Returns whether it did.

        A zero price means "no usable price on the page" and is never history --
        recording it would put a fake Rs.0 low into every chart.

        Deliberately not silent about an unknown ASIN: the insert hits the
        foreign key and raises ``sqlite3.IntegrityError``, which is how we know
        the cascade is really armed.
        """
        paise = int(paise)
        if paise <= 0:
            return False
        recorded_at = int(time.time()) if recorded_at is None else int(recorded_at)
        last = self._conn.execute(
            "SELECT paise FROM price_points WHERE asin = ? ORDER BY recorded_at DESC, id DESC LIMIT 1",
            (asin,),
        ).fetchone()
        if last is not None and last["paise"] == paise:
            return False
        with self._conn:
            self._conn.execute(
                "INSERT INTO price_points (asin, paise, recorded_at) VALUES (?, ?, ?)",
                (asin, paise, recorded_at),
            )
        return True

    def update_success(
        self,
        asin: str,
        *,
        price_paise: int,
        mrp_paise: int = 0,
        title: str = "",
        brand: str = "",
        image_url: str = "",
        rating: float = 0.0,
        review_count: int = 0,
        availability: str = "",
        now: int | None = None,
    ) -> Watched:
        """Fold a successful fetch into the row.

        A **zero price** (product unavailable) updates the metadata and clears
        the error, but leaves ``current_paise``/``mrp``/``lowest``/``highest``
        exactly as they were. Letting a temporary "currently unavailable" page
        rewrite ``lowest_paise`` to 0 would poison the record permanently.
        """
        current = self.require(asin)
        now = int(time.time()) if now is None else int(now)
        price_paise = int(price_paise)

        fields: dict[str, object] = {
            "last_checked_at": now,
            "last_success_at": now,
            "last_error": "",
            "availability": availability,
        }
        # Never let a partial parse blank out good metadata.
        if title:
            fields["title"] = title
        if brand:
            fields["brand"] = brand
        if image_url:
            fields["image_url"] = image_url
        if rating:
            fields["rating"] = float(rating)
        if review_count:
            fields["review_count"] = int(review_count)

        if price_paise > 0:
            fields["current_paise"] = price_paise
            fields["mrp_paise"] = int(mrp_paise)
            lowest = current.lowest_paise
            fields["lowest_paise"] = price_paise if lowest <= 0 else min(lowest, price_paise)
            fields["highest_paise"] = max(current.highest_paise, price_paise)

        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self._conn:
            self._conn.execute(
                f"UPDATE watched SET {assignments} WHERE asin = ?",
                (*fields.values(), asin),
            )
        return self.require(asin)

    def update_failure(self, asin: str, error: str, now: int | None = None) -> Watched:
        """Record a failed fetch.

        Touches ``last_checked_at`` and ``last_error`` and nothing else -- a
        network blip must never be mistaken for a price movement.
        """
        self.require(asin)
        now = int(time.time()) if now is None else int(now)
        with self._conn:
            self._conn.execute(
                "UPDATE watched SET last_checked_at = ?, last_error = ? WHERE asin = ?",
                (now, str(error), asin),
            )
        return self.require(asin)
