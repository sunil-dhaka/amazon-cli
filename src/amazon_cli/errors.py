"""Error types shared by every command.

The point of this hierarchy is that a caller can tell the three failure modes
apart without string matching: *you asked for something that does not exist*,
*Amazon refused us*, and *the page came back but we could not read it*. Before
this existed, a bot-check page parsed as "a product with no fields" and a
blocked search reported "0 results" -- both silent, both wrong.
"""


class AmzError(Exception):
    """Base class for every error `amz` raises deliberately."""

    #: Whether retrying the same request could plausibly succeed later.
    retryable = False

    #: Process exit code when this reaches the top level.
    exit_code = 1


class NotFoundError(AmzError):
    """No such product/page. A wrong ASIN will still be wrong in an hour."""

    retryable = False
    exit_code = 4


class BotCheckError(AmzError):
    """Amazon served a captcha or bot-verification interstitial.

    Retryable, but only after a real pause -- hammering it makes it worse.
    """

    retryable = True
    exit_code = 5

    def __init__(self, message: str = "Amazon served a bot check instead of the page"):
        super().__init__(
            f"{message}. Wait a minute and try again; if it persists, "
            "you are being rate-limited -- slow down or change network."
        )


class RateLimitedError(AmzError):
    """Amazon returned an explicit throttling status (429 / 503)."""

    retryable = True
    exit_code = 5


class NetworkError(AmzError):
    """Connection failed, timed out, or the server erred."""

    retryable = True
    exit_code = 3


class ParseError(AmzError):
    """The page loaded but the expected structure was not there.

    Usually means Amazon changed its markup -- the fixture tests exist to turn
    that into a failing test rather than a silently empty result.
    """

    retryable = True
    exit_code = 6


class InputError(AmzError):
    """The user gave us something we cannot use (bad ASIN, bad target price)."""

    retryable = False
    exit_code = 2
