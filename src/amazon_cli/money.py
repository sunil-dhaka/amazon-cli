"""Money handling for Amazon.in prices.

Every price in ``amz`` is an ``int`` count of **paise** (1/100 of a rupee).
Integer paise removes an entire class of bug: a price is compared, stored and
summed exactly, and the only place a rupee string exists is the display edge.

The previous implementation used ``int(float(re.sub(r"[^\\d.]", "", text)))``,
which crashed on a labelled price (``"M.R.P.: ₹34,990.00"`` -> ``float('...34990.00')``),
silently read ``"-26%"`` as ``26``, and truncated ``"₹459.50"`` to ``459``.
"""

import re

#: Sentinel for "no usable price on the page". Never a legitimate Amazon price.
UNKNOWN: int = 0

#: Highest price we accept from a parse: Rs. 1 crore. Above this it is a mis-parse.
MAX_PAISE: int = 1_00_00_000_00

# An optional sign, a run of digits and grouping commas, then an optional
# decimal tail. Scanning for a token rather than stripping non-digits is what
# survives the labels Amazon wraps around prices -- otherwise the full stops in
# "M.R.P.:" get folded into the number.
_NUMBER_TOKEN = re.compile(r"(-?)(\d[\d,]*)(?:\.(\d+))?")

# Unicode spaces Amazon sprinkles into price markup.
_SPACES = "    "


def parse_paise(text: str | None) -> int:
    """Parse an Amazon price string into paise.

    Handles the Indian digit grouping Amazon.in emits, with or without the
    rupee symbol, with or without a decimal part, and with or without a label::

        "₹1,72,490.00"       -> 17249000
        "₹3,325"             ->   332500
        "459.50"             ->    45950
        "M.R.P.: ₹34,990.00" ->  3499000

    Returns :data:`UNKNOWN` when the input has no usable number, when the number
    is a percentage rather than money, when the result would exceed
    :data:`MAX_PAISE`, or when the input is otherwise malformed. Never raises.
    """
    if not text:
        return UNKNOWN

    match = _NUMBER_TOKEN.search(text)
    if match is None:
        return UNKNOWN

    # A signed number here is a discount string like "-26%", never a price.
    if match.group(1) == "-":
        return UNKNOWN

    # A number followed by '%' is a percentage. Skip intervening spaces so
    # "26 %" is caught too, and reject a '%' immediately before the number.
    tail = text[match.end():].lstrip(" " + _SPACES)
    if tail.startswith("%"):
        return UNKNOWN
    head = text[: match.start()].rstrip(" " + _SPACES)
    if head.endswith("%"):
        return UNKNOWN

    whole = match.group(2).replace(",", "")
    if not whole:
        return UNKNOWN

    # Only the first two fractional digits are money. Truncating rather than
    # rounding is deliberate: a scraped third digit must never round a price
    # down onto a target it did not actually meet.
    raw_frac = match.group(3) or ""
    if not raw_frac:
        frac = "00"
    elif len(raw_frac) == 1:
        frac = raw_frac + "0"
    else:
        frac = raw_frac[:2]

    try:
        paise = int(whole) * 100 + int(frac)
    except ValueError:  # pragma: no cover -- the regex guarantees digits
        return UNKNOWN

    return paise if 1 <= paise <= MAX_PAISE else UNKNOWN


def group_indian(value: int) -> str:
    """Indian digit grouping: last three digits, then pairs.

    ``1234567 -> '12,34,567'``, ``1000 -> '1,000'``, ``999 -> '999'``.
    """
    digits = str(abs(value))
    sign = "-" if value < 0 else ""
    if len(digits) <= 3:
        return sign + digits

    last3, rest = digits[-3:], digits[:-3]
    parts: list[str] = []
    while len(rest) > 2:
        parts.insert(0, rest[-2:])
        rest = rest[:-2]
    if rest:
        parts.insert(0, rest)
    parts.append(last3)
    return sign + ",".join(parts)


def format_inr(paise: int, symbol: str = "Rs.") -> str:
    """Format paise with Indian grouping: ``17249000 -> 'Rs.1,72,490'``.

    The decimal part appears only when non-zero, matching how Amazon.in itself
    renders whole-rupee prices.
    """
    if paise == UNKNOWN:
        return "--"
    sign = "-" if paise < 0 else ""
    paise = abs(paise)
    rupees, frac = divmod(paise, 100)
    out = f"{sign}{symbol}{group_indian(rupees)}"
    return f"{out}.{frac:02d}" if frac else out


def format_compact(paise: int, symbol: str = "Rs.") -> str:
    """Axis/sparkline form: ``'Rs.1.7L'``, ``'Rs.26k'``, ``'Rs.459'``."""
    if paise == UNKNOWN:
        return "--"
    rupees = paise // 100
    for cutoff, div, suffix in ((1_00_00_000, 1_00_00_000, "Cr"), (1_00_000, 1_00_000, "L"), (1_000, 1_000, "k")):
        if rupees >= cutoff:
            v = round(rupees / div, 1)
            text = str(int(v)) if v == int(v) else str(v)
            return f"{symbol}{text}{suffix}"
    return f"{symbol}{rupees}"


def rupees(paise: int) -> int | float:
    """Rupee value for JSON/CSV output.

    Returns an ``int`` for whole rupees so existing consumers keep seeing
    ``3695`` rather than ``3695.0``, and a ``float`` only when paise are present.
    """
    if paise == UNKNOWN:
        return 0
    whole, frac = divmod(paise, 100)
    return whole if frac == 0 else paise / 100


def discount_percent(price: int, mrp: int) -> int:
    """Percentage saved off ``mrp`` when buying at ``price``, rounded half-up.

    Zero when either side is unknown or the discount would be non-positive --
    a negative discount is never advertised.
    """
    if price <= 0 or mrp <= 0 or price >= mrp:
        return 0
    return int((mrp - price) * 100 / mrp + 0.5)


def change_percent(before: int, after: int) -> int:
    """Signed percentage change from ``before`` to ``after``, rounded half-up."""
    if before <= 0 or after <= 0:
        return 0
    delta = (after - before) * 100 / before
    return int(delta + 0.5) if delta >= 0 else -int(-delta + 0.5)


def sane_mrp(mrp: int, price: int) -> int:
    """Discard an MRP that is not actually a higher list price.

    An ``mrp`` at or below ``price`` means the selector landed on the wrong node
    (commonly a "similar products" carousel), and keeping it would render a
    zero-or-negative discount.
    """
    if mrp <= 0 or (price > 0 and mrp <= price):
        return UNKNOWN
    return mrp
