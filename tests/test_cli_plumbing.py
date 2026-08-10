"""CLI plumbing: the error boundary, global options, CSV, and completions.

Exit codes are this tool's real API. A script that pipes `amz` into `jq` cannot
read a stack trace, but it can branch on 4 (no such product) versus 5 (we are
being throttled). So every failure has to arrive as one clean line on stderr
plus the documented status -- and `--debug` has to still give a developer the
traceback.

CSV gets the same treatment: Amazon titles are full of commas, quotes and the
occasional newline, and a formatter that corrupts them is worse than one that
refuses, because the corruption is silent.
"""

import csv
import io
import json
import os
import re

import click
import httpx
import pytest
import respx
from click.testing import CliRunner

from amazon_cli import __version__
from amazon_cli.cli import AmzGroup, cli
from amazon_cli.commands import compare as compare_cmd
from amazon_cli.commands import product as product_cmd
from amazon_cli.commands import search as search_cmd
from amazon_cli.errors import (
    AmzError,
    BotCheckError,
    InputError,
    NetworkError,
    NotFoundError,
    ParseError,
    RateLimitedError,
)
from amazon_cli.output import error, output_csv, output_json, output_plain

from conftest import load_fixture, load_product

BASE = "https://www.amazon.in"

#: Everything a naive CSV writer gets wrong at once.
NASTY = 'Acme "Pro" 2-in-1, Model X\nsecond line, "quoted", 1,000 units'


def run(*args, **kwargs):
    return CliRunner().invoke(cli, list(args), **kwargs)


def retitle_product(html: str, title: str) -> str:
    return re.sub(
        r'(<span id="productTitle"[^>]*>)(.*?)(</span>)',
        lambda m: m.group(1) + title + m.group(3),
        html, count=1, flags=re.S,
    )


def retitle_search(html: str, title: str) -> str:
    return re.sub(
        r"(<h2\b[^>]*><span>)(.*?)(</span></h2>)",
        lambda m: m.group(1) + title + m.group(3),
        html, flags=re.S,
    )


def add_review_text(html: str, title: str, body: str) -> str:
    """Give the captured review nodes the title/body hooks Amazon dropped."""
    marker = 'data-hook="review" class="a-section aok-relative">'
    inject = (
        f'<span data-hook="review-title"><span>{title}</span></span>'
        f'<span data-hook="review-body"><span>{body}</span></span>'
    )
    return html.replace(marker, marker + inject)


def mock_product(asin: str, html: str | None = None):
    return respx.get(f"{BASE}/dp/{asin}").mock(
        return_value=httpx.Response(200, text=html if html is not None else load_product(asin))
    )


def read_csv(text: str) -> tuple[list[str], list[list[str]]]:
    rows = list(csv.reader(io.StringIO(text)))
    assert rows, "no CSV was written at all"
    return rows[0], rows[1:]


# =========================================================== the error boundary

@pytest.mark.parametrize(
    "exc,code",
    [
        (InputError("bad input"), 2),
        (NetworkError("connection reset"), 3),
        (NotFoundError("no such product"), 4),
        (BotCheckError(), 5),
        (RateLimitedError("HTTP 429"), 5),
        (ParseError("markup moved"), 6),
        (AmzError("something generic"), 1),
    ],
)
def test_every_error_type_maps_to_its_documented_exit_code(exc, code):
    """Checked at the boundary itself, so an unraised type cannot rot unnoticed."""

    @click.group(cls=AmzGroup)
    @click.option("--debug", is_flag=True)
    def group(debug):
        pass

    @group.command("boom")
    def boom():
        raise exc

    result = CliRunner().invoke(group, ["boom"])
    assert result.exit_code == code


def test_the_boundary_prints_one_clean_line_with_no_traceback():
    @click.group(cls=AmzGroup)
    @click.option("--debug", is_flag=True)
    def group(debug):
        pass

    @group.command("boom")
    def boom():
        raise NotFoundError("no such product")

    result = CliRunner().invoke(group, ["boom"])
    assert result.exit_code == 4
    assert "Traceback" not in result.output
    assert "NotFoundError" not in result.output
    assert result.stderr.strip() == "Error: no such product"


def test_debug_re_raises_with_the_original_exception():
    @click.group(cls=AmzGroup)
    @click.option("--debug", is_flag=True)
    def group(debug):
        pass

    @group.command("boom")
    def boom():
        raise NotFoundError("no such product")

    result = CliRunner().invoke(group, ["--debug", "boom"])
    assert isinstance(result.exception, NotFoundError)
    assert result.exc_info is not None and result.exc_info[2] is not None


@respx.mock
def test_a_404_is_a_clean_exit_four():
    respx.get(f"{BASE}/dp/B0ZZZZZZZZ").mock(return_value=httpx.Response(404))
    result = run("product", "B0ZZZZZZZZ")
    assert result.exit_code == 4
    assert "Traceback" not in result.output
    assert result.stderr.startswith("Error:")
    assert result.stdout == ""


@respx.mock
def test_a_bot_check_is_a_clean_exit_five(botcheck_page):
    respx.get(f"{BASE}/s").mock(return_value=httpx.Response(200, text=botcheck_page))
    result = run("--retries", "0", "search", "headphones")
    assert result.exit_code == 5
    assert "Traceback" not in result.output
    assert "bot check" in result.stderr.lower()


@respx.mock
def test_throttling_is_a_clean_exit_five():
    respx.get(f"{BASE}/s").mock(return_value=httpx.Response(429))
    result = run("--retries", "0", "search", "headphones")
    assert result.exit_code == 5
    assert "Traceback" not in result.output


@respx.mock
def test_a_network_failure_is_a_clean_exit_three():
    respx.get(f"{BASE}/s").mock(side_effect=httpx.ConnectError("no route"))
    result = run("--retries", "0", "search", "headphones")
    assert result.exit_code == 3
    assert "Traceback" not in result.output


@respx.mock
def test_a_malformed_asin_is_a_clean_exit_two():
    result = run("product", "not-an-asin")
    assert result.exit_code == 2
    assert "Invalid ASIN" in result.stderr
    assert "Traceback" not in result.output


@respx.mock
def test_debug_on_the_real_cli_surfaces_the_typed_exception():
    respx.get(f"{BASE}/dp/B0ZZZZZZZZ").mock(return_value=httpx.Response(404))
    result = run("--debug", "product", "B0ZZZZZZZZ")
    assert isinstance(result.exception, NotFoundError)


@respx.mock
def test_without_debug_the_exception_is_only_a_systemexit():
    respx.get(f"{BASE}/dp/B0ZZZZZZZZ").mock(return_value=httpx.Response(404))
    result = run("product", "B0ZZZZZZZZ")
    assert isinstance(result.exception, SystemExit)
    assert result.exception.code == 4


# ======================================================= global option validation

@pytest.mark.parametrize(
    "args",
    [
        ["--timeout", "0"],
        ["--timeout", "-1"],
        ["--retries", "-1"],
        ["--min-interval", "-1"],
        ["--cache", "bogus"],
        ["--cache", ""],
        ["--cache", "1.5h"],
        ["--cache", "999999999999d"],
    ],
)
def test_a_bad_global_option_exits_two_cleanly(args):
    result = run(*args, "cache", "path")
    assert result.exit_code == 2, f"{args} should be a usage error"
    assert "Traceback" not in result.output
    assert "Error:" in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["--timeout", "0.001"],
        ["--retries", "0"],
        ["--min-interval", "0"],
        ["--cache", "30s"],
        ["--cache", "1d"],
        ["--no-cache"],
    ],
)
def test_legal_global_options_are_accepted(args):
    result = run(*args, "cache", "path")
    assert result.exit_code == 0, result.output


def test_no_cache_beats_cache_end_to_end(tmp_path):
    """`--no-cache` has to defeat a `--cache` inherited from a shell alias."""
    result = run(
        "--cache", "10m", "--no-cache", "--cache-dir", str(tmp_path), "cache", "stats"
    )
    assert result.exit_code == 0
    assert list(tmp_path.rglob("*")) == []


@respx.mock
def test_no_cache_actually_stops_entries_being_written(tmp_path):
    mock_product("B0BZP2H373")
    run("--cache", "10m", "--no-cache", "--cache-dir", str(tmp_path),
        "product", "B0BZP2H373", "--json")
    assert list(tmp_path.rglob("*.html.gz")) == []


@respx.mock
def test_cache_on_actually_writes_entries(tmp_path):
    mock_product("B0BZP2H373")
    run("--cache", "10m", "--cache-dir", str(tmp_path), "product", "B0BZP2H373", "--json")
    assert len(list(tmp_path.rglob("*.html.gz"))) == 1


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
@respx.mock
def test_an_unwritable_cache_dir_does_not_break_the_command(tmp_path):
    """A broken cache must cost speed, never correctness."""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o500)
    try:
        mock_product("B0BZP2H373")
        result = run("--cache", "10m", "--cache-dir", str(blocked),
                     "product", "B0BZP2H373", "--json")
        assert result.exit_code == 0
        assert json.loads(result.stdout)["asin"] == "B0BZP2H373"
        assert list(blocked.rglob("*")) == []
    finally:
        blocked.chmod(0o700)


@respx.mock
def test_a_cache_dir_pointing_at_a_file_is_a_usage_error(tmp_path):
    """Better to say so than to silently run uncached forever."""
    blocked = tmp_path / "blocked"
    blocked.write_text("this is a file, not a directory")
    result = run("--cache-dir", str(blocked), "cache", "path")
    assert result.exit_code == 2
    assert "Traceback" not in result.output


# ================================================================ version/help

def test_version_reports_the_package_version():
    result = run("--version")
    assert result.exit_code == 0
    assert __version__ in result.output
    assert result.output.strip() == f"amz, version {__version__}"


def test_help_lists_every_registered_command():
    result = run("--help")
    assert result.exit_code == 0
    for name in ("search", "product", "compare", "reviews", "cache", "completions"):
        assert name in result.output


def test_help_documents_every_global_option():
    result = run("--help")
    for flag in ("--cache", "--no-cache", "--cache-dir", "--timeout", "--retries",
                 "--min-interval", "--debug"):
        assert flag in result.output


# ================================================================ completions

@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_completions_emit_a_usable_script(shell):
    result = run("completions", shell)
    assert result.exit_code == 0
    assert result.output.strip(), "an empty completion script is worse than none"
    assert "_AMZ_COMPLETE" in result.output
    # The install hint has to be a comment, or sourcing the output fails.
    assert result.output.splitlines()[0].startswith("#")


@pytest.mark.parametrize(
    "shell,marker",
    [("bash", "complete "), ("zsh", "compdef"), ("fish", "function ")],
)
def test_each_completion_script_is_written_for_its_own_shell(shell, marker):
    assert marker in run("completions", shell).output


@pytest.mark.parametrize("shell", ["powershell", "csh", "", "BASH"])
def test_an_unsupported_shell_is_rejected_with_the_supported_list(shell):
    result = run("completions", shell)
    assert result.exit_code != 0
    assert "bash" in result.output and "zsh" in result.output and "fish" in result.output


def test_completions_without_a_shell_is_a_usage_error():
    result = run("completions")
    assert result.exit_code == 2


# ====================================================================== CSV

@respx.mock
def test_product_csv_round_trips_a_hostile_title():
    mock_product("B0BZP2H373", retitle_product(load_product("B0BZP2H373"), NASTY))
    result = run("product", "B0BZP2H373", "--csv")
    assert result.exit_code == 0

    header, rows = read_csv(result.stdout)
    assert header == product_cmd.CSV_HEADERS
    assert len(rows) == 1
    assert all(len(row) == len(header) for row in rows)
    assert dict(zip(header, rows[0]))["title"] == NASTY


@respx.mock
def test_product_csv_has_paise_alongside_rupees():
    mock_product("B0BZP2H373")
    result = run("product", "B0BZP2H373", "--csv")
    header, rows = read_csv(result.stdout)
    cells = dict(zip(header, rows[0]))

    assert "price_paise" in header and "mrp_paise" in header
    payload = json.loads(run("product", "B0BZP2H373", "--json").stdout)
    assert int(cells["price_paise"]) == payload["price_paise"]
    assert float(cells["price"]) == float(payload["price"])
    assert int(cells["price_paise"]) == round(float(cells["price"]) * 100)


@respx.mock
def test_product_csv_of_a_batch_has_one_row_per_asin():
    asins = ["B0BZP2H373", "B0C3ZYFZ77", "1847941834"]
    for asin in asins:
        mock_product(asin)
    header, rows = read_csv(run("product", *asins, "--csv").stdout)
    assert header == product_cmd.CSV_HEADERS
    assert [r[0] for r in rows] == asins
    assert all(len(row) == len(header) for row in rows)


@respx.mock
def test_search_csv_round_trips_hostile_titles():
    respx.get(f"{BASE}/s").mock(
        return_value=httpx.Response(200, text=retitle_search(load_fixture("search_headphones"), NASTY))
    )
    result = run("search", "headphones", "--csv")
    assert result.exit_code == 0

    header, rows = read_csv(result.stdout)
    assert header == search_cmd.CSV_HEADERS
    assert rows, "the fixture has results, so the CSV must have rows"
    assert all(len(row) == len(header) for row in rows)
    assert all(dict(zip(header, row))["title"] == NASTY for row in rows)


@respx.mock
def test_search_csv_has_paise_alongside_rupees():
    respx.get(f"{BASE}/s").mock(
        return_value=httpx.Response(200, text=load_fixture("search_headphones"))
    )
    header, rows = read_csv(run("search", "headphones", "--csv").stdout)
    assert "price_paise" in header
    payload = json.loads(run("search", "headphones", "--json").stdout)["products"]
    by_asin = {p["asin"]: p for p in payload}
    for row in rows:
        cells = dict(zip(header, row))
        assert int(cells["price_paise"]) == by_asin[cells["asin"]]["price_paise"]


@respx.mock
def test_compare_csv_round_trips_hostile_titles():
    asins = ["B0BZP2H373", "B0C3ZYFZ77"]
    for asin in asins:
        mock_product(asin, retitle_product(load_product(asin), NASTY))
    result = run("compare", *asins, "--csv")
    assert result.exit_code == 0

    header, rows = read_csv(result.stdout)
    assert header == compare_cmd.CSV_HEADERS
    assert len(rows) == 2
    assert all(len(row) == len(header) for row in rows)
    assert all(dict(zip(header, row))["title"] == NASTY for row in rows)
    assert "price_paise" in header and "mrp_paise" in header


@respx.mock
def test_reviews_csv_round_trips_hostile_bodies():
    page = add_review_text(load_product("B0BZP2H373"), NASTY, NASTY + " -- body text")
    mock_product("B0BZP2H373", page)
    result = run("reviews", "B0BZP2H373", "--csv")
    assert result.exit_code == 0

    header, rows = read_csv(result.stdout)
    assert rows
    assert all(len(row) == len(header) for row in rows)
    cells = dict(zip(header, rows[0]))
    assert cells["title"] == NASTY
    assert cells["body"] == NASTY + " -- body text"


@respx.mock
def test_reviews_csv_matches_the_json_row_count():
    mock_product("B0BZP2H373")
    _, rows = read_csv(run("reviews", "B0BZP2H373", "--csv").stdout)
    payload = json.loads(run("reviews", "B0BZP2H373", "--json").stdout)
    assert len(rows) == len(payload["reviews"])


@respx.mock
def test_reviews_csv_keeps_the_whole_body_not_a_truncation():
    """`--plain` truncates bodies to keep a TSV line readable; CSV must not."""
    long_body = "x" * 400
    page = add_review_text(load_product("B0BZP2H373"), "T", long_body)
    mock_product("B0BZP2H373", page)
    _, rows = read_csv(run("reviews", "B0BZP2H373", "--csv").stdout)
    assert any(len(row[-1]) == 400 for row in rows)


@respx.mock
def test_an_empty_search_is_non_zero_with_nothing_on_stdout():
    """`--csv` consumers must never receive a header for zero results."""
    respx.get(f"{BASE}/s").mock(
        return_value=httpx.Response(200, text="<html><body>nothing here</body></html>")
    )
    result = run("search", "asdfqwerzxcv", "--csv")
    assert result.exit_code != 0
    assert result.stdout == ""
    assert "No results found." in result.stderr
    assert "Traceback" not in result.output


@respx.mock
def test_csv_and_json_agree_on_the_row_count_for_search():
    respx.get(f"{BASE}/s").mock(
        return_value=httpx.Response(200, text=load_fixture("search_headphones"))
    )
    _, rows = read_csv(run("search", "headphones", "--csv").stdout)
    payload = json.loads(run("search", "headphones", "--json").stdout)
    assert len(rows) == len(payload["products"])


# ============================================================ output.py units

def test_output_csv_quotes_commas_quotes_and_newlines(capsys):
    output_csv([[NASTY, 1, None]], ["title", "n", "blank"])
    header, rows = read_csv(capsys.readouterr().out)
    assert header == ["title", "n", "blank"]
    assert rows == [[NASTY, "1", ""]]


def test_output_csv_writes_no_header_when_none_given(capsys):
    output_csv([["a", "b"]])
    assert capsys.readouterr().out == "a,b\n"


def test_output_csv_uses_lf_not_crlf(capsys):
    """The default csv dialect emits CRLF, which mangles diffs and pipes."""
    output_csv([["a"], ["b"]], ["h"])
    assert "\r" not in capsys.readouterr().out


def test_output_csv_of_no_rows_still_writes_the_header(capsys):
    output_csv([], ["a", "b"])
    assert capsys.readouterr().out == "a,b\n"


def test_output_plain_is_tab_separated(capsys):
    output_plain([["a", 1], ["b", 2]], ["x", "y"])
    assert capsys.readouterr().out == "x\ty\na\t1\nb\t2\n"


def test_output_json_is_utf8_and_indented(capsys):
    output_json({"title": "naïve ₹1,000"})
    out = capsys.readouterr().out
    assert json.loads(out) == {"title": "naïve ₹1,000"}
    assert "₹" in out, "ensure_ascii would turn every rupee sign into \\u20b9"


@pytest.mark.parametrize("code", [1, 2, 3, 4, 5, 6])
def test_error_exits_with_the_code_it_is_given(code, capsys):
    with pytest.raises(SystemExit) as excinfo:
        error("something went wrong", code)
    assert excinfo.value.code == code
    captured = capsys.readouterr()
    assert "something went wrong" in captured.err
    assert captured.out == "", "an error must never pollute stdout"


def test_error_defaults_to_exit_one():
    with pytest.raises(SystemExit) as excinfo:
        error("boom")
    assert excinfo.value.code == 1


# ================================================ reviews honours global options

@respx.mock
def test_reviews_rejects_a_bad_asin_with_exit_two():
    """`amz reviews` must agree with `amz product` on what bad input costs."""
    result = run("reviews", "not-an-asin")
    assert result.exit_code == InputError.exit_code == 2
    assert "Invalid ASIN" in result.stderr


@respx.mock
def test_reviews_maps_a_404_to_exit_four():
    respx.get(f"{BASE}/dp/B0ZZZZZZZZ").mock(return_value=httpx.Response(404))
    result = run("reviews", "B0ZZZZZZZZ")
    assert result.exit_code == 4


@respx.mock
def test_reviews_honours_retries():
    route = respx.get(f"{BASE}/dp/B0BZP2H373").mock(return_value=httpx.Response(503))
    result = run("--retries", "0", "reviews", "B0BZP2H373")
    assert result.exit_code == 5
    assert route.call_count == 1, "--retries never reached the reviews client"


@respx.mock
def test_reviews_honours_the_cache(tmp_path):
    route = mock_product("B0BZP2H373")
    args = ["--cache", "10m", "--cache-dir", str(tmp_path), "reviews", "B0BZP2H373", "--json"]
    assert run(*args).exit_code == 0
    assert run(*args).exit_code == 0
    assert route.call_count == 1, "--cache never reached the reviews client"


@respx.mock
def test_reviews_json_keeps_its_shape():
    mock_product("B0BZP2H373")
    payload = json.loads(run("reviews", "B0BZP2H373", "--json").stdout)
    assert payload["asin"] == "B0BZP2H373"
    assert isinstance(payload["reviews"], list) and payload["reviews"]


@respx.mock
def test_reviews_plain_still_truncates_the_body():
    long_body = "y" * 400
    mock_product("B0BZP2H373", add_review_text(load_product("B0BZP2H373"), "T", long_body))
    lines = run("reviews", "B0BZP2H373", "--plain").stdout.strip().split("\n")
    assert lines[0].split("\t") == ["rating", "title", "author", "date", "verified", "body"]
    assert all(len(line.split("\t")[-1]) <= 100 for line in lines[1:])
