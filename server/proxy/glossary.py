"""Per-game glossary loading and matching (GalTransl gpt_dict format).

A glossary file holds one entry per line: ``src->dst`` with an optional
`` #note`` suffix, e.g. ``ダイゴ->大吾 #人名``. Lines that are blank or start
with ``#`` / ``//`` are treated as comments.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


class GlossaryError(Exception):
    """Raised when a configured glossary file cannot be loaded."""


@dataclass(frozen=True)
class GlossaryEntry:
    """One gpt_dict term mapping (Japanese source -> target name)."""

    src: str
    dst: str
    note: str = ""

    def to_line(self) -> str:
        """Render the entry back to its gpt_dict prompt line."""
        return f"{self.src}->{self.dst} #{self.note}" if self.note else f"{self.src}->{self.dst}"


def parse_glossary(text: str, *, source: str = "<string>") -> list[GlossaryEntry]:
    """Parse gpt_dict lines; malformed or duplicate-src lines are skipped with a warning."""
    entries: list[GlossaryEntry] = []
    seen: set[str] = set()
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        src, arrow, rest = line.partition("->")
        if not arrow:
            logger.warning("%s:%d: no '->' separator, line skipped: %r", source, lineno, raw)
            continue
        dst, _, note = rest.partition(" #")
        src, dst, note = src.strip(), dst.strip(), note.strip()
        if not src or not dst:
            logger.warning("%s:%d: empty src or dst, line skipped: %r", source, lineno, raw)
            continue
        if src in seen:
            logger.warning("%s:%d: duplicate src %r, line skipped", source, lineno, src)
            continue
        seen.add(src)
        entries.append(GlossaryEntry(src, dst, note))
    return entries


class Glossary:
    """Term table with mtime-based hot reload; ``Glossary(None)`` is an empty no-op table."""

    def __init__(self, path: Path | None):
        self.path = path
        self._fingerprint: tuple[int, int] | None = None
        self._entries: list[GlossaryEntry] = []
        if path is not None:
            self._reload(initial=True)

    def _reload(self, *, initial: bool = False) -> None:
        try:
            before = self.path.stat()
            fingerprint = (before.st_mtime_ns, before.st_size)
            if fingerprint == self._fingerprint:
                return
            text = self.path.read_text(encoding="utf-8")
            after = self.path.stat()
        except OSError as exc:
            if initial:
                raise GlossaryError(
                    f"Cannot read glossary file {self.path}: {exc}. "
                    "Fix GLOSSARY_PATH or unset it to disable glossary injection."
                ) from exc
            logger.warning(
                "Glossary %s became unreadable (%s); keeping %d previously loaded entries",
                self.path, exc, len(self._entries),
            )
            return
        self._entries = parse_glossary(text, source=str(self.path))
        # Commit the fingerprint only if the file was stable across the read;
        # a mid-read write leaves it unset so the next request reloads. Size is
        # part of the fingerprint because a truncate-then-write can land within
        # one mtime tick.
        self._fingerprint = (
            fingerprint if (after.st_mtime_ns, after.st_size) == fingerprint else None
        )
        logger.info("Loaded %d glossary entries from %s", len(self._entries), self.path)

    def match(self, text: str) -> list[GlossaryEntry]:
        """Entries whose src occurs in ``text``, ordered by first occurrence."""
        if self.path is not None:
            self._reload()
        hits = [
            (text.find(entry.src), index, entry)
            for index, entry in enumerate(self._entries)
            if entry.src in text
        ]
        hits.sort(key=lambda item: (item[0], item[1]))
        return [entry for _, _, entry in hits]
