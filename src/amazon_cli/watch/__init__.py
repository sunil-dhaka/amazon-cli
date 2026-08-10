"""Price watchlist: persistent storage plus the alert rules that drive it.

The watchlist is the terminal counterpart of the Bhav Android app. Two pieces:

* :mod:`amazon_cli.watch.store` -- SQLite persistence. Every price is ``int``
  paise, every timestamp is epoch seconds.
* :mod:`amazon_cli.watch.service` -- the pure alert rule (:func:`decide_alert`),
  the polite sequential re-check loop, and the sparkline renderer.

Nothing in here talks to the terminal; :mod:`amazon_cli.commands.watch` does
that, so the rules stay testable without a CliRunner.
"""

from amazon_cli.watch.service import (
    CheckResult,
    Decision,
    check_all,
    decide_alert,
    seed_product,
    sparkline,
    time_weighted_average,
)
from amazon_cli.watch.store import (
    PricePoint,
    SCHEMA_VERSION,
    Watched,
    WatchStore,
    default_db_path,
)

__all__ = [
    "CheckResult",
    "Decision",
    "PricePoint",
    "SCHEMA_VERSION",
    "Watched",
    "WatchStore",
    "check_all",
    "decide_alert",
    "default_db_path",
    "seed_product",
    "sparkline",
    "time_weighted_average",
]
