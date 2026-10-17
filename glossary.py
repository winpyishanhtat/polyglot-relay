"""Event glossary: names and terms that must come out the same way in every caption.

POLYGLOT_GLOSSARY_FILE points at a JSON object mapping each term to how it is written in each language:

    {
      "UIT": "*",                                   keep it exactly as written, in every language
      "Win Pyi Shan Htat": "*",
      "Polyglot Relay": {"*": "Polyglot Relay"},     the same thing, written out
      "broker": {"th": "โบรกเกอร์", "vi": "máy môi giới"}   only these languages; others translate it freely
    }

Gemini is told the required renderings in its instructions. LibreTranslate cannot take instructions, so each
term is swapped for a placeholder it leaves alone, and the rendering is put back afterwards. The file is read
again when it changes, so terms can be added during an event without restarting anything.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable

LOG = logging.getLogger("polyglot.glossary")
KEEP = "*"
CHECK_EVERY = 2.0          # seconds between looks at the file's modification time


def placeholder(index: int) -> str:
    # Chosen by trying several styles on the live LibreTranslate (see tests/test_glossary.py): a short
    # all-capitals code with digits came back untouched in every language it serves.
    return f"ZQX{index}Q"


_PLACEHOLDER = re.compile(r"Z\s?Q\s?X\s?(\d+)\s?Q", re.IGNORECASE)


class Glossary:
    def __init__(self, path: str = "", terms: dict | None = None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._terms: dict[str, dict[str, str]] = {}
        self._patterns: list[tuple[str, re.Pattern]] = []
        self._mtime: float | None = None
        self._checked = 0.0
        self.version = 0
        if terms is not None:
            self._install(terms)
        elif path:
            self._reload(force=True)

    @classmethod
    def from_env(cls) -> "Glossary":
        return cls(os.getenv("POLYGLOT_GLOSSARY_FILE") or "")

    # ---- loading -------------------------------------------------------------------------------

    @staticmethod
    def _normalise(raw: dict) -> dict[str, dict[str, str]]:
        if not isinstance(raw, dict):
            raise ValueError("the glossary must be a JSON object of term -> rendering")
        terms: dict[str, dict[str, str]] = {}
        for term, rendering in raw.items():
            term = " ".join(str(term).split())
            if not term:
                continue
            if rendering == KEEP:
                terms[term] = {KEEP: term}
            elif isinstance(rendering, str):
                terms[term] = {KEEP: rendering}
            elif isinstance(rendering, dict):
                terms[term] = {str(lang): (term if value == KEEP else str(value)) for lang, value in rendering.items() if value}
            else:
                raise ValueError(f"the rendering of {term!r} must be \"*\", a string, or an object of language -> text")
        return terms

    def _install(self, raw: dict) -> None:
        terms = self._normalise(raw)
        patterns = []
        for term in sorted(terms, key=len, reverse=True):         # longest first: "Polyglot Relay" before "Relay"
            escaped = re.escape(term).replace(r"\ ", r"\s+")
            if re.match(r"\w", term[0], re.ASCII) and re.match(r"\w", term[-1], re.ASCII):
                escaped = rf"(?<!\w){escaped}(?!\w)"              # whole words only, for Latin-script terms
            patterns.append((term, re.compile(escaped, re.IGNORECASE)))
        with self._lock:
            self._terms, self._patterns = terms, patterns
            self.version += 1

    def _reload(self, force: bool = False) -> None:
        now = time.monotonic()
        if not self.path or (not force and now - self._checked < CHECK_EVERY):
            return
        self._checked = now
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            if self._terms:
                LOG.warning("Glossary file %s is gone; no glossary in use", self.path)
                self._install({})
            self._mtime = None
            return
        if mtime == self._mtime:
            return
        self._mtime = mtime
        try:
            with open(self.path, encoding="utf-8") as handle:
                self._install(json.load(handle))
            LOG.info("Glossary loaded from %s: %d terms", self.path, len(self._terms))
        except (OSError, ValueError) as exc:
            LOG.warning("Glossary file %s could not be used (%s); keeping the previous one", self.path, exc)

    # ---- use -----------------------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._terms)

    def rendering(self, term: str, lang: str) -> str | None:
        entry = self._terms.get(term, {})
        return entry.get(lang) or entry.get(KEEP)

    def matches(self, text: str, lang: str) -> list[tuple[str, str]]:
        """(term, rendering in `lang`) for each glossary term found in `text`, longest terms first."""
        self._reload()
        with self._lock:
            patterns = list(self._patterns)
        found = []
        for term, pattern in patterns:
            rendering = self.rendering(term, lang)
            if rendering and pattern.search(text):
                found.append((term, rendering))
        return found

    def instruction(self, text: str, lang: str) -> str:
        """The sentence added to a Gemini prompt, or "" when no term occurs in the text."""
        found = self.matches(text, lang)
        if not found:
            return ""
        pairs = "; ".join(f"“{term}” → “{rendering}”" for term, rendering in found)
        return f" Use exactly these renderings for names and terms: {pairs}."

    def protect(self, text: str, lang: str) -> tuple[str, Callable[[str], str | None]]:
        """Swap each term for a placeholder before machine translation. The returned function puts the
        renderings back, or returns None if the translator lost a placeholder (the caller then retries
        without protection)."""
        self._reload()
        with self._lock:
            patterns = list(self._patterns)
        renderings: list[str] = []
        protected = text
        for term, pattern in patterns:
            rendering = self.rendering(term, lang)
            if not rendering or not pattern.search(protected):
                continue

            def swap(_match, index=len(renderings)):
                return placeholder(index)
            protected = pattern.sub(swap, protected)
            renderings.append(rendering)

        def restore(translated: str) -> str | None:
            if not renderings:
                return translated
            seen: set[int] = set()

            def put_back(match):
                index = int(match.group(1))
                if index >= len(renderings):
                    return match.group(0)
                seen.add(index)
                return renderings[index]
            result = _PLACEHOLDER.sub(put_back, translated)
            return result if len(seen) == len(renderings) else None
        return protected, restore


def context_instruction(context: list[str] | None) -> str:
    """The sentence added to a Gemini prompt carrying what was said just before (for consistency only)."""
    lines = [" ".join(str(line).split()) for line in (context or []) if str(line).strip()]
    if not lines:
        return ""
    quoted = " ".join(f"({n}) “{line[:300]}”" for n, line in enumerate(lines[-5:], 1))
    return (" For context only, these sentences were said just before this one: " + quoted +
            ". Use them to keep names, pronouns, gender and tense consistent, but translate only the new fragment"
            " and do not repeat or translate the earlier sentences.")
