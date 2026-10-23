"""Turn a unittest log into GitHub Actions error annotations: one per failing test, with the end of its traceback.

    python -m unittest discover -s tests 2>&1 | tee unit.log
    python tools/ci_annotate.py unit.log

Annotations show on the run's page and can be read through GitHub's public API without signing in, so a
failure can be diagnosed even by someone who cannot open the full log. GitHub keeps at most 10 per step.
"""
from __future__ import annotations

import re
import sys

HEADER = re.compile(r"^(FAIL|ERROR): (\S+) \(([^)]+)\)")
RULE = re.compile(r"^(=|-){20,}$")


def failures(log: str, tail: int = 12) -> list[tuple[str, str, str]]:
    """(kind, test, last lines of its traceback) for every FAIL/ERROR block in a unittest log."""
    lines = log.splitlines()
    found = []
    for index, line in enumerate(lines):
        match = HEADER.match(line)
        if not match:
            continue
        body = []
        for following in lines[index + 1:]:
            if HEADER.match(following) or (body and RULE.match(following) and following.startswith("=")):
                break
            if not RULE.match(following):
                body.append(following)
        found.append((match.group(1), f"{match.group(3)}", "\n".join(b for b in body if b.strip())[-1500:].splitlines()[-tail:]))
    return [(kind, test, "\n".join(text)) for kind, test, text in found]


def escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "").replace("\n", "%0A")


def main(argv: list[str]) -> int:
    with open(argv[1], encoding="utf-8", errors="replace") as handle:
        log = handle.read()
    problems = failures(log)
    for kind, test, text in problems[:10]:
        print(f"::error title={escape(kind + ' ' + test)}::{escape(text)}")
    summary = [line for line in log.splitlines() if line.startswith(("Ran ", "FAILED", "OK"))]
    if summary:
        print(f"::notice title=unittest summary::{escape(' '.join(summary[-2:]))}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
