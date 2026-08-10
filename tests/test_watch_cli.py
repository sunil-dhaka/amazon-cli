"""``amz watch`` end-to-end through click's CliRunner.

The network is never touched: :func:`amazon_cli.watch.service.get_product` and
the client that wraps it are both replaced, and the polite inter-product gap is
patched to a no-op so a three-product sweep costs microseconds, not 12 seconds.
Every test writes to a ``$AMZ_WATCH_DB`` under ``tmp_path``.
"""

import json

import pytest
from click.testing import CliRunner

from amazon_cli.client.types import ProductDetail
from amazon_cli.commands import watch as watch_cmd
from amazon_cli.commands.watch import watch
from amazon_cli.watch import service
from amazon_cli.watch.store import WatchStore

ASIN = "B0BZP2H373"
OTHER = "B0C3ZYFZ77"
BAD = "NOTANASIN!"
UNWATCHED = "1847941834"


class FakeClient:
    """Stands in for :class:`AmazonClient`; never opens a socket."""

    instances = 0

    def __init__(self, *a, **kw):
        FakeClient.instances += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def responses():
    """ASIN -> ProductDetail to return, or an Exception to raise."""
    return {}


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "watch.db"
    monkeypatch.setenv("AMZ_WATCH_DB", str(path))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    return path


@pytest.fixture(autouse=True)
def offline(monkeypatch, responses, db_path):
    """No network, no sleeping, no touching the real watchlist."""
    FakeClient.instances = 0
    monkeypatch.setattr(watch_cmd, "AmazonClient", FakeClient)

    async def fake_get_product(client, asin, **kw):
        assert isinstance(client, FakeClient), "a real client escaped into a test"
        reply = responses.get(asin)
        if reply is None:
            return ProductDetail(asin=asin)
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def no_sleep(seconds):
        return None

    monkeypatch.setattr(service, "get_product", fake_get_product)
    monkeypatch.setattr(service, "_sleep", no_sleep)


@pytest.fixture
def run():
    runner = CliRunner()

    def invoke(*args, **kw):
        return runner.invoke(watch, list(args), env={"COLUMNS": "200"}, **kw)

    return invoke


@pytest.fixture
def store(db_path):
    """A store for arranging/asserting, opened per use so the CLI can write."""

    class Opener:
        def __enter__(self):
            self.s = WatchStore()
            return self.s

        def __exit__(self, *exc):
            self.s.close()

    return Opener


def product(asin=ASIN, price=2_499_00, **kw):
    kw.setdefault("title", "Sony WH-1000XM5 Wireless Headphones")
    kw.setdefault("brand", "Sony")
    kw.setdefault("availability", "In stock")
    return ProductDetail(asin=asin, price=price, **kw)


def add(run, responses, asin=ASIN, target="2500", price=2_499_00, **kw):
    responses[asin] = product(asin=asin, price=price, **kw)
    result = run("add", asin, "--target", target)
    assert result.exit_code == 0, result.output
    return result


# ===========================================================================
# add
# ===========================================================================


def test_add_creates_a_row_and_seeds_the_price(run, responses, store):
    result = add(run, responses, target="2500", price=2_499_00)
    assert "Watching" in result.output
    assert ASIN in result.output

    with store() as s:
        entry = s.require(ASIN)
        assert entry.target_paise == 2_500_00
        assert entry.current_paise == 2_499_00
        assert entry.lowest_paise == 2_499_00
        assert entry.highest_paise == 2_499_00
        assert entry.notified_at_paise == 2_499_00, "seeding below target arms the memory"
        assert [p.paise for p in s.history(ASIN)] == [2_499_00]


def test_add_lowercases_and_accepts_a_note(run, responses, store):
    responses[ASIN] = product(price=3_000_00)
    result = run("add", ASIN.lower(), "--target", "2500", "--note", "for the trip")
    assert result.exit_code == 0
    with store() as s:
        entry = s.require(ASIN)
        assert entry.note == "for the trip"
        assert entry.current_paise == 3_000_00


def test_add_reports_the_gap_when_still_above_target(run, responses):
    responses[ASIN] = product(price=3_000_00)
    result = run("add", ASIN, "--target", "2500")
    assert result.exit_code == 0
    assert "to go" in result.output


def test_add_twice_updates_instead_of_duplicating(run, responses, store):
    add(run, responses, target="2500")
    with store() as s:
        added_at = s.require(ASIN).added_at

    result = run("add", ASIN, "--target", "2000")
    assert result.exit_code == 0
    assert "Updated" in result.output
    with store() as s:
        assert s.count() == 1
        entry = s.require(ASIN)
        assert entry.target_paise == 2_000_00
        assert entry.added_at == added_at


@pytest.mark.parametrize("bad", [BAD, "SHORT", "B0BZP2H3730", ""])
def test_add_rejects_a_malformed_asin(run, bad, store):
    result = run("add", bad, "--target", "2500")
    assert result.exit_code == 2
    with store() as s:
        assert s.count() == 0


@pytest.mark.parametrize("target", ["0", "-1", "abc", "1e9", "99999999", "", "1999rupees"])
def test_add_rejects_an_invalid_target(run, target, store):
    result = run("add", ASIN, "--target", target)
    assert result.exit_code == 2
    with store() as s:
        assert s.count() == 0, "a rejected target must not leave a row behind"


def test_add_requires_a_target(run):
    result = run("add", ASIN)
    assert result.exit_code == 2


def test_add_of_a_nonexistent_product_leaves_no_phantom_row(run, responses, store):
    from amazon_cli.errors import NotFoundError

    responses[ASIN] = NotFoundError("Product page returned 404")
    result = run("add", ASIN, "--target", "2500")
    assert result.exit_code == 4
    with store() as s:
        assert s.count() == 0


def test_add_that_404s_keeps_an_existing_row(run, responses, store):
    from amazon_cli.errors import NotFoundError

    add(run, responses)
    responses[ASIN] = NotFoundError("Product page returned 404")
    result = run("add", ASIN, "--target", "2000")
    assert result.exit_code == 4
    with store() as s:
        assert s.count() == 1, "an existing row is not collateral damage"


def test_add_survives_a_network_failure(run, responses, store):
    from amazon_cli.errors import NetworkError

    responses[ASIN] = NetworkError("connection reset by peer")
    result = run("add", ASIN, "--target", "2500")
    assert result.exit_code == 0
    assert "Could not fetch the price" in result.output
    with store() as s:
        entry = s.require(ASIN)
        assert entry.target_paise == 2_500_00
        assert entry.current_paise == 0
        assert entry.last_error == "connection reset by peer"


# ===========================================================================
# list
# ===========================================================================


def test_list_on_an_empty_watchlist(run):
    result = run("list")
    assert result.exit_code == 0
    assert "Nothing on the watchlist yet" in result.output


def test_list_json_on_an_empty_watchlist(run):
    result = run("list", "--json")
    assert result.exit_code == 0
    assert json.loads(result.stdout) == []


def test_list_renders_a_table(run, responses):
    add(run, responses)
    result = run("list")
    assert result.exit_code == 0
    assert ASIN in result.output
    assert "Watchlist (1)" in result.output


def test_list_json_carries_paise_and_a_sparkline(run, responses):
    add(run, responses, price=2_499_00)
    run("check")
    result = run("list", "--json")
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert len(data) == 1
    row = data[0]
    assert row["asin"] == ASIN
    assert row["price_paise"] == 2_499_00
    assert row["target_paise"] == 2_500_00
    assert row["lowest_paise"] == 2_499_00
    assert row["highest_paise"] == 2_499_00
    assert row["below_target"] is True
    assert row["points"] == 1
    assert isinstance(row["sparkline"], str)
    assert all(isinstance(row[k], int) for k in ("price_paise", "target_paise", "lowest_paise"))


def test_list_csv_and_plain_have_the_same_headers(run, responses):
    add(run, responses)
    csv_out = run("list", "--csv")
    plain_out = run("list", "--plain")
    assert csv_out.exit_code == plain_out.exit_code == 0
    assert csv_out.output.splitlines()[0].split(",") == plain_out.output.splitlines()[0].split("\t")
    assert "price_paise" in csv_out.output.splitlines()[0]
    assert "2499" in csv_out.output.splitlines()[1]


def test_list_table_shows_gap_error_and_missing_price(run, responses):
    from amazon_cli.errors import NetworkError

    add(run, responses, asin=ASIN, target="2500", price=3_000_00)  # above target
    responses[OTHER] = NetworkError("connection reset")
    run("add", OTHER, "--target", "2500")  # never priced, carries an error

    result = run("list")
    assert result.exit_code == 0
    assert "to go" in result.output, "an above-target product shows the remaining gap"
    assert "no price" in result.output
    assert "error" in result.output
    assert "HIT" not in result.output


def test_list_table_marks_a_hit(run, responses):
    add(run, responses, target="2500", price=2_400_00)
    assert "HIT" in run("list").output


def test_history_of_a_single_flat_series_renders(run, responses):
    add(run, responses, target="2500", price=2_400_00)
    run("check")
    run("check")
    result = run("history", ASIN)
    assert result.exit_code == 0
    assert "▄" in result.output


def test_list_sort_by_price(run, responses):
    add(run, responses, asin=ASIN, target="3000", price=2_499_00)
    add(run, responses, asin=OTHER, target="3000", price=999_00)
    result = run("list", "--json", "--sort", "price")
    assert [row["asin"] for row in json.loads(result.stdout)] == [OTHER, ASIN]


def test_list_rejects_an_unknown_sort_key(run, responses):
    add(run, responses)
    result = run("list", "--sort", "cheapest")
    assert result.exit_code == 2


# ===========================================================================
# check
# ===========================================================================


def test_check_on_an_empty_watchlist(run):
    result = run("check")
    assert result.exit_code == 0
    assert "Nothing on the watchlist yet" in result.output


def test_check_json_on_an_empty_watchlist(run):
    result = run("check", "--json")
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"checked": 0, "alerts": 0, "failed": 0, "results": []}


def test_check_quiet_on_an_empty_watchlist_says_nothing(run):
    result = run("check", "--quiet")
    assert result.exit_code == 0
    assert result.output.strip() == ""


def test_lowering_the_target_re_alerts_at_the_unchanged_price(run, responses):
    """set-target re-arms, so the same price is news again -- with no delta."""
    add(run, responses, target="3000", price=2_400_00)
    assert "ALERT" not in run("check").output

    assert run("set-target", ASIN, "2500").exit_code == 0
    result = run("check")
    assert result.exit_code == 0
    assert "ALERT" in result.output
    assert "was Rs." not in result.output, "nothing moved, so do not claim it did"


def test_check_alerts_on_a_drop(run, responses, store):
    add(run, responses, target="2500", price=3_000_00)
    responses[ASIN] = product(price=2_400_00)

    result = run("check")
    assert result.exit_code == 0
    assert "ALERT" in result.output
    with store() as s:
        entry = s.require(ASIN)
        assert entry.current_paise == 2_400_00
        assert entry.notified_at_paise == 2_400_00
        assert [p.paise for p in s.history(ASIN)] == [3_000_00, 2_400_00]


def test_check_does_not_re_alert_at_the_same_price(run, responses):
    add(run, responses, target="2500", price=3_000_00)
    responses[ASIN] = product(price=2_400_00)
    assert "ALERT" in run("check").output
    assert "ALERT" not in run("check").output
    assert "ALERT" not in run("check").output


def test_check_quiet_prints_only_alerts(run, responses):
    add(run, responses, target="2500", price=3_000_00)
    responses[ASIN] = product(price=2_900_00)
    quiet = run("check", "--quiet")
    assert quiet.exit_code == 0
    assert quiet.output.strip() == ""

    responses[ASIN] = product(price=2_400_00)
    loud = run("check", "-q")
    assert "ALERT" in loud.output


def test_check_json_shape(run, responses):
    add(run, responses, target="2500", price=3_000_00)
    responses[ASIN] = product(price=2_400_00)
    result = run("check", "--json")
    data = json.loads(result.stdout)
    assert data["checked"] == 1
    assert data["alerts"] == 1
    assert data["failed"] == 0
    row = data["results"][0]
    assert row["asin"] == ASIN
    assert row["price_paise"] == 2_400_00
    assert row["previous_paise"] == 3_000_00
    assert row["target_paise"] == 2_500_00
    assert row["change_paise"] == -600_00
    assert row["change_percent"] == -20
    assert row["alert"] is True
    assert row["reason"] == "first-hit"
    assert row["recorded"] is True


def test_check_only_one_product(run, responses):
    add(run, responses, asin=ASIN, target="3000", price=2_900_00)
    add(run, responses, asin=OTHER, target="3000", price=2_800_00)
    responses[ASIN] = product(asin=ASIN, price=1_000_00)
    responses[OTHER] = product(asin=OTHER, price=1_000_00)

    result = run("check", "--only", ASIN.lower(), "--json")
    data = json.loads(result.stdout)
    assert data["checked"] == 1
    assert data["results"][0]["asin"] == ASIN


def test_check_only_an_unwatched_asin_is_not_found(run, responses):
    add(run, responses)
    result = run("check", "--only", UNWATCHED)
    assert result.exit_code == 4


def test_check_only_a_malformed_asin_is_an_input_error(run, responses):
    add(run, responses)
    result = run("check", "--only", BAD)
    assert result.exit_code == 2


def test_check_survives_one_failing_product(run, responses, store):
    from amazon_cli.errors import BotCheckError

    add(run, responses, asin=ASIN, target="3000", price=2_900_00)
    add(run, responses, asin=OTHER, target="3000", price=2_800_00)
    responses[ASIN] = BotCheckError()
    responses[OTHER] = product(asin=OTHER, price=1_000_00)

    result = run("check", "--json")
    assert result.exit_code == 0, "one flaky page must not fail a cron job"
    data = json.loads(result.stdout)
    assert data["checked"] == 2
    assert data["failed"] == 1
    assert data["alerts"] == 1

    with store() as s:
        broken = s.require(ASIN)
        assert broken.current_paise == 2_900_00, "a bot check is not a price move"
        assert broken.last_error
        assert s.require(OTHER).current_paise == 1_000_00


@pytest.mark.parametrize(
    "exc, code",
    [
        ("NetworkError", 3),
        ("NotFoundError", 4),
        ("RateLimitedError", 5),
        ("ParseError", 6),
    ],
)
def test_check_exits_non_zero_only_when_everything_failed(run, responses, exc, code):
    import amazon_cli.errors as errors

    add(run, responses, asin=ASIN, target="3000", price=2_900_00)
    add(run, responses, asin=OTHER, target="3000", price=2_800_00)
    failure = getattr(errors, exc)("everything is on fire")
    responses[ASIN] = failure
    responses[OTHER] = failure

    result = run("check")
    assert result.exit_code == code


def test_check_all_failed_still_emits_valid_json(run, responses):
    from amazon_cli.errors import NetworkError

    add(run, responses, asin=ASIN, target="3000", price=2_900_00)
    responses[ASIN] = NetworkError("down")
    result = run("check", "--json")
    assert result.exit_code == 3
    data = json.loads(result.stdout)
    assert data["failed"] == 1
    assert data["results"][0]["error"] == "down"


def test_check_reports_an_unavailable_product_without_alerting(run, responses, store):
    add(run, responses, target="3000", price=2_900_00)
    responses[ASIN] = product(price=0, availability="Currently unavailable")

    result = run("check", "--json")
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["alerts"] == 0
    row = data["results"][0]
    assert row["reason"] == "no-price"
    assert row["recorded"] is False
    assert row["price_paise"] == 0, "this check saw no price -- do not report a stale one"
    assert row["previous_paise"] == 2_900_00
    assert row["availability"] == "Currently unavailable"
    with store() as s:
        assert s.require(ASIN).current_paise == 2_900_00, "the row still remembers it"


def test_check_prints_the_no_price_line_for_an_unavailable_product(run, responses):
    add(run, responses, target="3000", price=2_900_00)
    responses[ASIN] = product(price=0, availability="Currently unavailable")
    result = run("check")
    assert result.exit_code == 0
    assert "no price" in result.output
    assert "Currently unavailable" in result.output


def test_check_is_polite_between_products(run, responses, monkeypatch):
    """The CLI must not pass a zero delay: cron sweeps stay randomised 2-6s."""
    gaps = []

    async def recording_sleep(seconds):
        gaps.append(seconds)

    monkeypatch.setattr(service, "_sleep", recording_sleep)

    add(run, responses, asin=ASIN, target="3000", price=2_900_00)
    add(run, responses, asin=OTHER, target="3000", price=2_800_00)
    gaps.clear()

    assert run("check").exit_code == 0
    assert len(gaps) == 1, "n-1 gaps for n products"
    low, high = service.DELAY_RANGE
    assert low <= gaps[0] <= high


def test_check_fetches_products_one_at_a_time(run, responses, monkeypatch):
    inflight = {"now": 0, "max": 0}
    original = service.get_product

    async def counting(client, asin, **kw):
        inflight["now"] += 1
        inflight["max"] = max(inflight["max"], inflight["now"])
        try:
            return await original(client, asin, **kw)
        finally:
            inflight["now"] -= 1

    add(run, responses, asin=ASIN, target="3000", price=2_900_00)
    add(run, responses, asin=OTHER, target="3000", price=2_800_00)
    monkeypatch.setattr(service, "get_product", counting)

    assert run("check").exit_code == 0
    assert inflight["max"] == 1, "sequential on purpose -- parallel fetches earn a bot check"


def test_all_failed_reports_to_stderr_leaving_stdout_parseable(run, responses):
    from amazon_cli.errors import NetworkError

    add(run, responses, asin=ASIN, target="3000", price=2_900_00)
    responses[ASIN] = NetworkError("down")
    result = run("check", "--json")
    assert result.exit_code == 3
    assert "Error:" in result.stderr
    assert "Error:" not in result.stdout
    assert json.loads(result.stdout)["failed"] == 1


def test_check_opens_exactly_one_client_for_the_whole_sweep(run, responses):
    add(run, responses, asin=ASIN, target="3000", price=2_900_00)
    add(run, responses, asin=OTHER, target="3000", price=2_800_00)
    FakeClient.instances = 0
    run("check")
    assert FakeClient.instances == 1


# ===========================================================================
# remove
# ===========================================================================


def test_remove_deletes_the_product_and_its_history(run, responses, store):
    add(run, responses)
    result = run("remove", ASIN.lower())
    assert result.exit_code == 0
    assert "Removed" in result.output
    with store() as s:
        assert s.count() == 0
        assert s.connection.execute("SELECT COUNT(*) FROM price_points").fetchone()[0] == 0


def test_remove_unknown_asin(run):
    result = run("remove", UNWATCHED)
    assert result.exit_code == 4


def test_remove_malformed_asin(run):
    assert run("remove", BAD).exit_code == 2


# ===========================================================================
# set-target
# ===========================================================================


def test_set_target_changes_the_target_and_rearms(run, responses, store):
    add(run, responses, target="2500", price=2_499_00)
    with store() as s:
        assert s.require(ASIN).notified_at_paise == 2_499_00

    result = run("set-target", ASIN, "2000")
    assert result.exit_code == 0
    assert "Target" in result.output
    with store() as s:
        entry = s.require(ASIN)
        assert entry.target_paise == 2_000_00
        assert entry.notified_at_paise == 0


def test_set_target_unknown_asin(run):
    assert run("set-target", UNWATCHED, "2000").exit_code == 4


@pytest.mark.parametrize("target", ["0", "-5", "nope", "10000001"])
def test_set_target_invalid(run, responses, target):
    add(run, responses)
    result = run("set-target", ASIN, target)
    assert result.exit_code == 2


def test_set_target_malformed_asin(run):
    assert run("set-target", BAD, "2000").exit_code == 2


# ===========================================================================
# mute / unmute
# ===========================================================================


def test_mute_then_unmute(run, responses, store):
    add(run, responses)
    assert run("mute", ASIN.lower()).exit_code == 0
    with store() as s:
        assert s.require(ASIN).alerts_enabled is False
    assert run("unmute", ASIN.lower()).exit_code == 0
    with store() as s:
        assert s.require(ASIN).alerts_enabled is True


def test_muted_product_never_alerts_but_still_tracks(run, responses, store):
    add(run, responses, target="2500", price=3_000_00)
    run("mute", ASIN)
    responses[ASIN] = product(price=2_000_00)

    result = run("check", "--json")
    data = json.loads(result.stdout)
    assert data["alerts"] == 0
    assert data["results"][0]["reason"] == "muted"
    with store() as s:
        entry = s.require(ASIN)
        assert entry.current_paise == 2_000_00, "muted still means tracked"
        assert entry.notified_at_paise == 0

    run("unmute", ASIN)
    after = json.loads(run("check", "--json").stdout)
    assert after["alerts"] == 1, "unmuting at an untold price must alert"


@pytest.mark.parametrize("cmd", ["mute", "unmute"])
def test_mute_commands_on_unknown_and_malformed_asins(run, cmd):
    assert run(cmd, UNWATCHED).exit_code == 4
    assert run(cmd, BAD).exit_code == 2


def test_list_marks_a_muted_product(run, responses):
    add(run, responses)
    run("mute", ASIN)
    assert json.loads(run("list", "--json").stdout)[0]["alerts_enabled"] is False
    assert "muted" in run("list").output


# ===========================================================================
# history
# ===========================================================================


def test_history_with_no_points(run, responses):
    from amazon_cli.errors import NetworkError

    responses[ASIN] = NetworkError("nope")
    run("add", ASIN, "--target", "2500")
    result = run("history", ASIN)
    assert result.exit_code == 0
    assert "No price recorded yet" in result.output


def test_history_renders_a_table_and_a_sparkline(run, responses):
    add(run, responses, target="2500", price=3_000_00)
    for price in (2_800_00, 2_600_00, 2_400_00):
        responses[ASIN] = product(price=price)
        run("check")

    result = run("history", ASIN.lower())
    assert result.exit_code == 0
    assert "low" in result.output and "high" in result.output
    assert "-7%" in result.output


def test_history_json(run, responses):
    add(run, responses, target="2500", price=3_000_00)
    responses[ASIN] = product(price=2_400_00)
    run("check")

    result = run("history", ASIN, "--json")
    data = json.loads(result.stdout)
    assert data["asin"] == ASIN
    assert data["price_paise"] == 2_400_00
    assert data["target_paise"] == 2_500_00
    assert data["lowest_paise"] == 2_400_00
    assert data["highest_paise"] == 3_000_00
    assert [p["price_paise"] for p in data["points"]] == [3_000_00, 2_400_00]
    assert isinstance(data["average_paise"], int)
    assert 2_400_00 <= data["average_paise"] <= 3_000_00


def test_history_unknown_and_malformed_asin(run):
    assert run("history", UNWATCHED).exit_code == 4
    assert run("history", BAD).exit_code == 2


# ===========================================================================
# clear
# ===========================================================================


def test_clear_on_an_empty_watchlist(run):
    result = run("clear")
    assert result.exit_code == 0
    assert "Nothing on the watchlist yet" in result.output


def test_clear_with_yes(run, responses, store):
    add(run, responses, asin=ASIN)
    add(run, responses, asin=OTHER)
    result = run("clear", "--yes")
    assert result.exit_code == 0
    assert "Cleared" in result.output and "2" in result.output
    with store() as s:
        assert s.count() == 0
        assert s.connection.execute("SELECT COUNT(*) FROM price_points").fetchone()[0] == 0


def test_clear_asks_before_deleting(run, responses, store):
    add(run, responses)
    result = run("clear", input="n\n")
    assert result.exit_code == 1
    with store() as s:
        assert s.count() == 1


def test_clear_accepts_a_yes_at_the_prompt(run, responses, store):
    add(run, responses)
    result = run("clear", input="y\n")
    assert result.exit_code == 0
    with store() as s:
        assert s.count() == 0


# ===========================================================================
# database plumbing
# ===========================================================================


def test_every_subcommand_writes_only_to_the_env_var_database(run, responses, db_path, tmp_path):
    add(run, responses)
    assert db_path.exists()
    strays = [p.name for p in tmp_path.iterdir() if p.name.startswith("watch.db")]
    assert set(strays) <= {"watch.db", "watch.db-wal", "watch.db-shm"}


def test_a_corrupt_database_is_a_message_not_a_traceback(run, db_path):
    db_path.write_bytes(b"this is not a sqlite file" * 200)
    result = run("list")
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "not a usable watchlist database" in (result.stderr or result.output)


def test_the_watch_group_lists_every_subcommand(run):
    result = run("--help")
    assert result.exit_code == 0
    for name in ("add", "list", "check", "remove", "set-target", "mute", "unmute", "history", "clear"):
        assert name in result.output
