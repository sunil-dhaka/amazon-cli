"""Storage-layer tests for the price watchlist.

Everything here is exact-value: row counts, ``int`` paise, timestamps. A test
that only asserts "it did not raise" would have passed against every bug this
file actually caught.
"""

import dataclasses
import random
import sqlite3

import pytest

from amazon_cli.errors import AmzError, NotFoundError
from amazon_cli.watch.store import (
    SCHEMA_VERSION,
    PricePoint,
    Watched,
    WatchStore,
    default_db_path,
)

ASIN = "B0BZP2H373"
OTHER = "B0C3ZYFZ77"

#: Money fields that must round-trip as exact ``int`` paise, never floats.
PAISE_FIELDS = (
    "target_paise",
    "current_paise",
    "mrp_paise",
    "lowest_paise",
    "highest_paise",
    "notified_at_paise",
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Point ``$AMZ_WATCH_DB`` at a throwaway file for the whole test."""
    path = tmp_path / "watch" / "watch.db"
    monkeypatch.setenv("AMZ_WATCH_DB", str(path))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    return path


@pytest.fixture
def store(db):
    with WatchStore() as s:
        yield s


def seeded(store, asin=ASIN, *, target=2_000_00, now=1_000_000):
    store.add(asin, target_paise=target, now=now)
    return store.require(asin)


# ---------------------------------------------------------------------------
# where the database lives
# ---------------------------------------------------------------------------


def test_amz_watch_db_env_var_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("AMZ_WATCH_DB", str(tmp_path / "explicit.db"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert default_db_path() == tmp_path / "explicit.db"


def test_env_var_is_expanded(monkeypatch):
    monkeypatch.setenv("AMZ_WATCH_DB", "~/somewhere/watch.db")
    assert "~" not in str(default_db_path())
    assert str(default_db_path()).endswith("somewhere/watch.db")


def test_xdg_data_home_is_second_choice(tmp_path, monkeypatch):
    monkeypatch.delenv("AMZ_WATCH_DB", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert default_db_path() == tmp_path / "xdg" / "amz" / "watch.db"


def test_falls_back_to_local_share(monkeypatch):
    monkeypatch.delenv("AMZ_WATCH_DB", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert default_db_path().parts[-4:] == (".local", "share", "amz", "watch.db")


def test_store_with_no_argument_honours_the_env_var(db):
    """The CLI always constructs ``WatchStore()`` bare -- so this must hold."""
    with WatchStore() as s:
        assert s.path == db
        s.add(ASIN, target_paise=100_00)
    assert db.exists()


def test_parent_directory_is_created(tmp_path, monkeypatch):
    nested = tmp_path / "a" / "b" / "c" / "watch.db"
    monkeypatch.setenv("AMZ_WATCH_DB", str(nested))
    with WatchStore() as s:
        s.add(ASIN, target_paise=1_00)
    assert nested.exists()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def test_add_returns_created_true_then_false(store):
    entry, created = store.add(ASIN, target_paise=2_000_00, now=1_000)
    assert created is True
    assert entry.asin == ASIN
    assert entry.target_paise == 2_000_00
    assert entry.added_at == 1_000
    assert entry.alerts_enabled is True

    entry, created = store.add(ASIN, target_paise=1_500_00, now=9_999)
    assert created is False
    assert entry.target_paise == 1_500_00


def test_crud_round_trip(store):
    store.add(ASIN, target_paise=2_499_00, note="birthday", now=111)
    store.update_success(
        ASIN,
        price_paise=2_699_00,
        mrp_paise=3_499_00,
        title="Sony WH-1000XM5",
        brand="Sony",
        image_url="https://example.invalid/a.jpg",
        rating=4.4,
        review_count=1234,
        availability="In stock",
        now=222,
    )
    got = store.require(ASIN)
    assert got.title == "Sony WH-1000XM5"
    assert got.brand == "Sony"
    assert got.image_url == "https://example.invalid/a.jpg"
    assert got.rating == pytest.approx(4.4)
    assert got.review_count == 1234
    assert got.availability == "In stock"
    assert got.note == "birthday"
    assert got.added_at == 111
    assert got.last_checked_at == 222
    assert got.last_success_at == 222
    assert got.last_error == ""
    assert (got.current_paise, got.mrp_paise, got.lowest_paise, got.highest_paise) == (
        2_699_00,
        3_499_00,
        2_699_00,
        2_699_00,
    )


def test_get_returns_none_and_require_raises(store):
    assert store.get("NOTTHERE1") is None
    with pytest.raises(NotFoundError) as exc:
        store.require("NOTTHERE1")
    assert exc.value.exit_code == 4
    assert "NOTTHERE1" in str(exc.value)


def test_list_all_is_ordered_by_added_then_asin(store):
    store.add("BBBBBBBBBB", target_paise=1_00, now=5)
    store.add("AAAAAAAAAA", target_paise=1_00, now=5)
    store.add("CCCCCCCCCC", target_paise=1_00, now=1)
    assert [e.asin for e in store.list_all()] == [
        "CCCCCCCCCC",
        "AAAAAAAAAA",
        "BBBBBBBBBB",
    ]
    assert store.count() == 3


def test_remove_reports_whether_it_removed_anything(store):
    seeded(store)
    assert store.remove(ASIN) is True
    assert store.remove(ASIN) is False
    assert store.count() == 0


def test_clear_returns_the_number_removed(store):
    seeded(store, ASIN)
    seeded(store, OTHER)
    assert store.clear() == 2
    assert store.clear() == 0
    assert store.list_all() == []


def test_set_note_and_set_alerts(store):
    seeded(store)
    assert store.set_note(ASIN, "wait for sale").note == "wait for sale"
    assert store.set_alerts(ASIN, False).alerts_enabled is False
    assert store.set_alerts(ASIN, True).alerts_enabled is True


def test_mutators_raise_not_found_for_unknown_asin(store):
    for call in (
        lambda: store.set_target("NOTTHERE1", 1_00),
        lambda: store.set_alerts("NOTTHERE1", False),
        lambda: store.set_note("NOTTHERE1", "x"),
        lambda: store.update_success("NOTTHERE1", price_paise=1_00),
        lambda: store.update_failure("NOTTHERE1", "boom"),
    ):
        with pytest.raises(NotFoundError):
            call()


# ---------------------------------------------------------------------------
# re-adding must not destroy anything
# ---------------------------------------------------------------------------


def test_readd_preserves_added_at_history_and_extremes(store):
    store.add(ASIN, target_paise=2_000_00, note="original", now=1_000)
    store.update_success(ASIN, price_paise=2_500_00, now=1_010)
    store.append_price_point(ASIN, 2_500_00, 1_010)
    store.update_success(ASIN, price_paise=1_900_00, now=1_020)
    store.append_price_point(ASIN, 1_900_00, 1_020)

    before = store.require(ASIN)
    assert (before.lowest_paise, before.highest_paise) == (1_900_00, 2_500_00)

    entry, created = store.add(ASIN, target_paise=1_800_00, now=9_999_999)
    assert created is False
    assert entry.added_at == 1_000, "re-adding must not reset added_at"
    assert entry.lowest_paise == 1_900_00
    assert entry.highest_paise == 2_500_00
    assert entry.current_paise == 2_500_00 or entry.current_paise == 1_900_00
    assert [p.paise for p in store.history(ASIN)] == [2_500_00, 1_900_00]
    assert entry.note == "original", "an empty note must not wipe the old one"


def test_readd_with_a_new_note_replaces_the_old_one(store):
    store.add(ASIN, target_paise=2_000_00, note="original", now=1)
    entry, _ = store.add(ASIN, target_paise=2_000_00, note="revised", now=2)
    assert entry.note == "revised"


def test_readd_with_a_changed_target_rearms_the_alert(store):
    seeded(store)
    store.set_notified(ASIN, 1_950_00)
    entry, _ = store.add(ASIN, target_paise=1_800_00)
    assert entry.notified_at_paise == 0


def test_readd_with_the_same_target_keeps_the_alert_memory(store):
    seeded(store, target=2_000_00)
    store.set_notified(ASIN, 1_950_00)
    entry, _ = store.add(ASIN, target_paise=2_000_00)
    assert entry.notified_at_paise == 1_950_00


def test_set_target_rearms_only_on_a_real_change(store):
    seeded(store, target=2_000_00)
    store.set_notified(ASIN, 1_950_00)
    assert store.set_target(ASIN, 2_000_00).notified_at_paise == 1_950_00
    assert store.set_target(ASIN, 2_100_00).notified_at_paise == 0


# ---------------------------------------------------------------------------
# foreign keys, cascade
# ---------------------------------------------------------------------------


def test_foreign_keys_pragma_is_actually_on(store):
    assert store.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_history_for_an_unknown_asin_is_rejected(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.append_price_point("GHOST12345", 1_00, 1)


def test_removing_a_product_cascades_its_history(store):
    seeded(store)
    seeded(store, OTHER)
    store.append_price_point(ASIN, 1_00, 1)
    store.append_price_point(ASIN, 2_00, 2)
    store.append_price_point(OTHER, 3_00, 3)

    store.remove(ASIN)
    assert store.history(ASIN) == []
    assert [p.paise for p in store.history(OTHER)] == [3_00]
    assert store.connection.execute("SELECT COUNT(*) FROM price_points").fetchone()[0] == 1


def test_clear_cascades_all_history(store):
    seeded(store)
    seeded(store, OTHER)
    store.append_price_point(ASIN, 1_00, 1)
    store.append_price_point(OTHER, 2_00, 2)
    store.clear()
    assert store.connection.execute("SELECT COUNT(*) FROM price_points").fetchone()[0] == 0


def test_foreign_keys_survive_reopening(db):
    with WatchStore() as s:
        seeded(s)
    with WatchStore() as s:
        assert s.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            s.append_price_point("GHOST12345", 1_00, 1)


# ---------------------------------------------------------------------------
# durability
# ---------------------------------------------------------------------------


def test_data_survives_close_and_reopen(db):
    with WatchStore() as s:
        s.add(ASIN, target_paise=2_000_00, note="keep me", now=42)
        s.update_success(ASIN, price_paise=1_999_00, title="Thing", now=43)
        s.append_price_point(ASIN, 1_999_00, 43)
        s.set_notified(ASIN, 1_999_00)

    with WatchStore() as s:
        entry = s.require(ASIN)
        assert entry.note == "keep me"
        assert entry.added_at == 42
        assert entry.current_paise == 1_999_00
        assert entry.notified_at_paise == 1_999_00
        assert entry.title == "Thing"
        assert [p.paise for p in s.history(ASIN)] == [1_999_00]


def test_two_connections_can_read_and_write_at_once(db):
    """WAL is what stops a `watch check` from locking out a `watch list`."""
    with WatchStore() as writer, WatchStore() as reader:
        assert reader.count() == 0
        seeded(writer, ASIN)
        writer.append_price_point(ASIN, 1_999_00, 1)
        assert reader.count() == 1
        assert [p.paise for p in reader.history(ASIN)] == [1_999_00]

        writer.update_success(ASIN, price_paise=1_899_00, now=2)
        assert reader.require(ASIN).current_paise == 1_899_00


def test_reopening_does_not_re_run_the_migration(db):
    with WatchStore() as s:
        seeded(s)
        s.append_price_point(ASIN, 1_00, 1)
        rowid = s.connection.execute("SELECT id FROM price_points").fetchone()[0]
    with WatchStore() as s:
        assert s.connection.execute("SELECT id FROM price_points").fetchone()[0] == rowid
        assert s.count() == 1


def test_in_memory_store_needs_no_files(tmp_path, monkeypatch):
    monkeypatch.setenv("AMZ_WATCH_DB", str(tmp_path / "unused.db"))
    with WatchStore(":memory:") as s:
        assert s.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        seeded(s)
        s.append_price_point(ASIN, 1_00, 1)
        assert s.count() == 1
        with pytest.raises(sqlite3.IntegrityError):
            s.append_price_point("GHOST12345", 1_00, 1)
    assert list(tmp_path.iterdir()) == []


def test_schema_version_is_stamped(store):
    assert store.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_a_newer_schema_is_refused_cleanly(tmp_path, monkeypatch):
    path = tmp_path / "future.db"
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 7}")
    conn.commit()
    conn.close()
    monkeypatch.setenv("AMZ_WATCH_DB", str(path))
    with pytest.raises(AmzError) as exc:
        WatchStore()
    assert "newer amz" in str(exc.value)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"not a database at all, not even close" * 40, id="garbage"),
        pytest.param(b"", id="empty-but-nonempty-name"),
    ],
)
def test_a_corrupt_database_file_is_a_clean_error(tmp_path, monkeypatch, payload):
    """A hand-mangled file must not reach the user as a sqlite3 traceback."""
    path = tmp_path / "corrupt.db"
    path.write_bytes(payload)
    monkeypatch.setenv("AMZ_WATCH_DB", str(path))
    if not payload:
        # A zero-byte file is a legitimately empty SQLite database.
        with WatchStore() as s:
            assert s.count() == 0
        return
    with pytest.raises(AmzError) as exc:
        WatchStore()
    assert str(path) in str(exc.value)
    assert exc.value.exit_code == 1


def test_a_truncated_database_file_is_a_clean_error(tmp_path, monkeypatch):
    good = tmp_path / "good.db"
    monkeypatch.setenv("AMZ_WATCH_DB", str(good))
    with WatchStore() as s:
        seeded(s)
        for i in range(50):
            s.append_price_point(ASIN, 1_00 + i, i)
    raw = good.read_bytes()
    assert len(raw) > 4096

    broken = tmp_path / "broken.db"
    broken.write_bytes(raw[:2048] + b"\x00" * 2048)
    monkeypatch.setenv("AMZ_WATCH_DB", str(broken))
    with pytest.raises(AmzError):
        WatchStore()


def test_a_corrupt_database_does_not_leak_a_connection(tmp_path, monkeypatch):
    path = tmp_path / "corrupt.db"
    path.write_bytes(b"definitely not sqlite" * 100)
    monkeypatch.setenv("AMZ_WATCH_DB", str(path))
    for _ in range(30):
        with pytest.raises(AmzError):
            WatchStore()
    # The file must be untouched -- we do not "repair" by truncation.
    assert path.read_bytes().startswith(b"definitely not sqlite")


# ---------------------------------------------------------------------------
# history is append-on-change-only
# ---------------------------------------------------------------------------


def _rows(store):
    return store.connection.execute("SELECT COUNT(*) FROM price_points").fetchone()[0]


def test_unchanged_price_appends_no_row(store):
    seeded(store)
    assert store.append_price_point(ASIN, 1_999_00, 100) is True
    assert _rows(store) == 1
    for stamp in range(101, 121):
        assert store.append_price_point(ASIN, 1_999_00, stamp) is False
    assert _rows(store) == 1


def test_changed_price_appends_exactly_one_row(store):
    seeded(store)
    store.append_price_point(ASIN, 1_999_00, 100)
    assert store.append_price_point(ASIN, 1_899_00, 101) is True
    assert _rows(store) == 2
    assert [p.paise for p in store.history(ASIN)] == [1_999_00, 1_899_00]


def test_returning_to_a_previous_price_does_append(store):
    """Only the *immediately* previous price suppresses a row."""
    seeded(store)
    for i, paise in enumerate([1_00, 2_00, 1_00, 1_00, 2_00]):
        store.append_price_point(ASIN, paise, i)
    assert [p.paise for p in store.history(ASIN)] == [1_00, 2_00, 1_00, 2_00]


def test_zero_and_negative_prices_are_never_history(store):
    seeded(store)
    assert store.append_price_point(ASIN, 0, 1) is False
    assert store.append_price_point(ASIN, -5_00, 2) is False
    assert _rows(store) == 0


def test_history_limit_keeps_the_newest_in_oldest_first_order(store):
    seeded(store)
    for i in range(10):
        store.append_price_point(ASIN, (i + 1) * 1_00, 1_000 + i)
    assert [p.paise for p in store.history(ASIN, limit=3)] == [8_00, 9_00, 10_00]
    assert len(store.history(ASIN)) == 10
    assert store.history(ASIN, limit=0) == []


def test_all_histories_groups_by_asin_in_time_order(store):
    seeded(store, ASIN)
    seeded(store, OTHER)
    store.append_price_point(ASIN, 3_00, 3)
    store.append_price_point(OTHER, 9_00, 1)
    store.append_price_point(ASIN, 1_00, 4)
    store.append_price_point(OTHER, 8_00, 2)
    assert store.all_histories() == {ASIN: [3_00, 1_00], OTHER: [9_00, 8_00]}


def test_price_point_to_dict_carries_paise(store):
    point = PricePoint(asin=ASIN, paise=1_999_50, recorded_at=7)
    assert point.to_dict() == {
        "recorded_at": 7,
        "price": 1999.5,
        "price_paise": 1_999_50,
    }


# ---------------------------------------------------------------------------
# numeric integrity
# ---------------------------------------------------------------------------


def test_lowest_and_highest_track_a_long_seeded_walk(store):
    """400 checks; the extremes must equal an independently computed min/max."""
    rng = random.Random(20260810)
    seeded(store)

    price = 24_990_00
    observed = []
    for step in range(400):
        price = max(1_00, price + rng.randint(-500_00, 500_00))
        observed.append(price)
        store.update_success(ASIN, price_paise=price, now=1_000 + step)
        store.append_price_point(ASIN, price, 1_000 + step)

    entry = store.require(ASIN)
    assert entry.lowest_paise == min(observed)
    assert entry.highest_paise == max(observed)
    assert entry.current_paise == observed[-1]
    for field in PAISE_FIELDS:
        value = getattr(entry, field)
        assert isinstance(value, int) and not isinstance(value, bool)
    assert all(isinstance(p.paise, int) for p in store.history(ASIN))


def test_first_price_seeds_both_extremes(store):
    seeded(store)
    entry = store.update_success(ASIN, price_paise=1_234_56, now=1)
    assert entry.lowest_paise == 1_234_56
    assert entry.highest_paise == 1_234_56


def test_extremes_never_widen_on_a_price_between_them(store):
    seeded(store)
    store.update_success(ASIN, price_paise=1_000_00, now=1)
    store.update_success(ASIN, price_paise=3_000_00, now=2)
    entry = store.update_success(ASIN, price_paise=2_000_00, now=3)
    assert (entry.lowest_paise, entry.highest_paise) == (1_000_00, 3_000_00)


def test_a_float_price_is_stored_as_an_int(store):
    seeded(store)
    entry = store.update_success(ASIN, price_paise=1_999.0, mrp_paise=2_999.0, now=1)
    assert entry.current_paise == 1999 and isinstance(entry.current_paise, int)
    assert entry.mrp_paise == 2999 and isinstance(entry.mrp_paise, int)


# ---------------------------------------------------------------------------
# failure and no-price folds
# ---------------------------------------------------------------------------


def _money_snapshot(entry):
    return {f: getattr(entry, f) for f in PAISE_FIELDS}


def test_update_failure_touches_nothing_but_the_error(store):
    seeded(store)
    store.update_success(ASIN, price_paise=2_500_00, mrp_paise=3_000_00, title="T", now=10)
    store.set_notified(ASIN, 2_500_00)
    before = store.require(ASIN)

    after = store.update_failure(ASIN, "Connection refused", now=99)
    assert _money_snapshot(after) == _money_snapshot(before)
    assert after.last_error == "Connection refused"
    assert after.last_checked_at == 99
    assert after.last_success_at == before.last_success_at == 10
    assert after.title == "T"
    assert _rows(store) == 0

    unchanged = dataclasses.asdict(before)
    unchanged.pop("last_error")
    unchanged.pop("last_checked_at")
    got = dataclasses.asdict(after)
    got.pop("last_error")
    got.pop("last_checked_at")
    assert got == unchanged


def test_zero_price_clears_the_error_and_freezes_the_money(store):
    seeded(store)
    store.update_success(ASIN, price_paise=2_500_00, mrp_paise=3_000_00, now=10)
    store.append_price_point(ASIN, 2_500_00, 10)
    store.update_failure(ASIN, "boom", now=11)
    before = store.require(ASIN)
    assert before.last_error == "boom"

    after = store.update_success(
        ASIN, price_paise=0, mrp_paise=0, availability="Currently unavailable", now=12
    )
    assert after.last_error == ""
    assert after.last_success_at == 12
    assert after.availability == "Currently unavailable"
    assert _money_snapshot(after) == _money_snapshot(before)
    assert _rows(store) == 1


def test_partial_metadata_never_blanks_good_metadata(store):
    seeded(store)
    store.update_success(
        ASIN,
        price_paise=1_00,
        title="Full Title",
        brand="Sony",
        image_url="https://example.invalid/i.jpg",
        rating=4.5,
        review_count=99,
        now=1,
    )
    after = store.update_success(ASIN, price_paise=1_00, now=2)
    assert after.title == "Full Title"
    assert after.brand == "Sony"
    assert after.image_url == "https://example.invalid/i.jpg"
    assert after.rating == pytest.approx(4.5)
    assert after.review_count == 99


# ---------------------------------------------------------------------------
# the Watched value object
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "current, target, expected",
    [
        (0, 2_000_00, False),
        (2_000_01, 2_000_00, False),
        (2_000_00, 2_000_00, True),
        (1_999_99, 2_000_00, True),
        (1_00, 0, False),
    ],
)
def test_below_target(current, target, expected):
    assert Watched(ASIN, current_paise=current, target_paise=target).below_target is expected


@pytest.mark.parametrize(
    "current, target, expected",
    [
        (2_500_00, 2_000_00, 500_00),
        (2_000_00, 2_000_00, 0),
        (1_000_00, 2_000_00, 0),
        (0, 2_000_00, 0),
        (2_500_00, 0, 0),
    ],
)
def test_gap_paise(current, target, expected):
    assert Watched(ASIN, current_paise=current, target_paise=target).gap_paise == expected


def test_to_dict_exposes_paise_and_rupees(store):
    seeded(store, target=2_000_00, now=5)
    store.update_success(ASIN, price_paise=1_999_50, mrp_paise=2_999_00, now=6)
    data = store.require(ASIN).to_dict()
    assert data["price_paise"] == 1_999_50
    assert data["price"] == 1999.5
    assert data["target_paise"] == 2_000_00
    assert data["target"] == 2000
    assert data["below_target"] is True
    assert data["gap_paise"] == 0
    assert data["lowest_paise"] == 1_999_50
    assert data["highest_paise"] == 1_999_50
    assert data["alerts_enabled"] is True


def test_drop_and_discount_percent_are_ints(store):
    seeded(store)
    store.update_success(ASIN, price_paise=3_000_00, mrp_paise=4_000_00, now=1)
    store.update_success(ASIN, price_paise=2_400_00, mrp_paise=4_000_00, now=2)
    entry = store.require(ASIN)
    assert entry.highest_paise == 3_000_00
    assert entry.drop_percent == -20
    assert entry.discount_percent == 40
    assert isinstance(entry.drop_percent, int)
    assert isinstance(entry.discount_percent, int)
