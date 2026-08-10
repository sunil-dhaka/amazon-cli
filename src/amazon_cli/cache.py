"""On-disk HTTP response cache.

Amazon product pages are ~2 MB each and change on the order of hours, so
refetching one three times while comparing products is pure waste -- and every
extra request is another chance to trip a bot check. The cache is opt-in per
invocation (``--cache 10m``) and keyed by the full request URL.

Entries are gzipped HTML with a plain-text header line carrying the stored-at
timestamp, so a cache file is inspectable with ``zcat``.
"""

import gzip
import hashlib
import os
import re
import time
import zlib
from pathlib import Path

_DURATION = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$", re.IGNORECASE)
_MULTIPLIER = {"s": 1, "m": 60, "h": 3600, "d": 86400, "": 60}

#: Longest cache lifetime we accept. A year already means "effectively never
#: expires", so anything past it is a typo (`--cache 999999999999d`) whose only
#: effect would be to pin a stale page forever -- better to say so than to obey.
MAX_DURATION_SECONDS = 365 * 86400


def parse_duration(text: str) -> int:
    """``'10m' -> 600``. A bare number is minutes. Raises ``ValueError``."""
    match = _DURATION.match(text or "")
    if not match:
        raise ValueError(
            f"Invalid duration {text!r}. Use forms like 30s, 10m, 2h, 1d."
        )
    seconds = int(match.group(1)) * _MULTIPLIER[match.group(2).lower()]
    if seconds > MAX_DURATION_SECONDS:
        raise ValueError(f"Duration {text!r} is too long -- the maximum is 365d.")
    return seconds


def default_cache_dir() -> Path:
    """`$XDG_CACHE_HOME/amz` if set, else `~/.cache/amz`."""
    root = os.environ.get("XDG_CACHE_HOME")
    base = Path(root) if root else Path.home() / ".cache"
    return base / "amz"


class ResponseCache:
    """A tiny content cache. Disabled when ``ttl_seconds`` is zero or negative."""

    def __init__(self, ttl_seconds: int = 0, directory: Path | None = None):
        self.ttl_seconds = ttl_seconds
        self.directory = directory or default_cache_dir()

    @property
    def enabled(self) -> bool:
        return self.ttl_seconds > 0

    def _path_for(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        # Shard by the first two hex chars so one directory never holds
        # thousands of entries.
        return self.directory / digest[:2] / f"{digest}.html.gz"

    def get(self, key: str) -> str | None:
        """Return cached HTML, or ``None`` on a miss or an expired entry."""
        if not self.enabled:
            return None
        path = self._path_for(key)
        try:
            raw = gzip.decompress(path.read_bytes()).decode("utf-8")
        except (OSError, EOFError, gzip.BadGzipFile, UnicodeDecodeError, zlib.error):
            # A corrupt or half-written entry is a miss, never an error.
            # `zlib.error` is the one that bites: a file with an intact gzip
            # header over a damaged deflate stream raises it, and it is *not* an
            # OSError, so leaving it out let a bad cache file crash the command
            # the cache exists to speed up.
            return None
        stored_at, _, body = raw.partition("\n")
        try:
            age = time.time() - float(stored_at)
        except ValueError:
            return None
        if age > self.ttl_seconds:
            return None
        return body

    def set(self, key: str, html: str) -> None:
        """Store HTML. Failures are swallowed -- a cache is never load-bearing."""
        if not self.enabled or not html:
            return
        path = self._path_for(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = f"{time.time()}\n{html}".encode("utf-8")
            # Write-then-rename so a crash mid-write cannot leave a torn entry
            # that a later read would have to defend against.
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(gzip.compress(payload, 6))
            tmp.replace(path)
        except OSError:
            return

    def clear(self) -> int:
        """Delete every entry. Returns how many files were removed."""
        removed = 0
        if not self.directory.exists():
            return 0
        for entry in self.directory.rglob("*.html.gz"):
            try:
                entry.unlink()
                removed += 1
            except OSError:
                continue
        # A crashed write leaves a `.tmp` beside the entry it never became.
        # Nothing else ever reclaims those, so `cache clear` would report an
        # empty cache while the bytes stayed on disk. Debris, so not counted.
        for debris in self.directory.rglob("*.html.tmp"):
            try:
                debris.unlink()
            except OSError:
                continue
        return removed

    def stats(self) -> tuple[int, int]:
        """``(entry_count, total_bytes)`` currently on disk."""
        count = size = 0
        if not self.directory.exists():
            return (0, 0)
        for entry in self.directory.rglob("*.html.gz"):
            try:
                size += entry.stat().st_size
                count += 1
            except OSError:
                continue
        return (count, size)
