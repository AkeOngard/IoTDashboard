"""List what the English catalogue is missing, and what it no longer needs.

    python scripts/check_i18n.py            # exits 1 if anything is missing
    python scripts/check_i18n.py --keys     # print every key the code uses

Thai text in the templates and scripts is the key (see app/i18n.py), so this
reads the keys straight out of `_('…')` in templates/ and `tr('…')` in
templates/ and static/ (and `translate(lang, "…")` in app/), then compares
them with static/i18n/en.js. It also
points at Thai text that is not inside either call, which would stay Thai for
an English viewer. Run it after changing any wording.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOGUE = ROOT / "static" / "i18n" / "en.js"
SOURCES = sorted((ROOT / "templates").glob("*.html")) + [
    p for p in sorted((ROOT / "static").glob("*.js")) if p.name != "i18n.js"
]

THAI = re.compile("[\u0e00-\u0e7f]")
LITERAL = re.compile(r"'((?:[^'\\]|\\.)*)'")
CALL = re.compile(r"(?<![\w.])(_|tr)\(")
COMMENT = re.compile(
    r"<!--.*?-->|\{#.*?#\}|/\*.*?\*/|(?m:^[ \t]*//[^\n]*$)|(?m:[ \t]{2,}//[^\n]*$)", re.S)
#: An element marked lang="th" is Thai on purpose: a language's own name.
IN_THAI = re.compile(r'<(\w+)[^>]*\blang="th"[^>]*>[^<]*</\1>')
#: The few messages the API words for a person: translate(lang, "…", …).
SERVER_CALL = re.compile(r'translate\(\s*[^,]+,\s*"([^"]+)"')


def first_argument(text: str, start: int) -> tuple[str, int]:
    """The first argument of the call whose `(` ends at `start`, and where the
    whole call ends."""
    depth, quote, i, cut = 0, "", start, None
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = ""
        elif c in "'`":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                return text[start: cut if cut is not None else i], i
            depth -= 1
        elif c == "," and depth == 0 and cut is None:
            cut = i
        i += 1
    return text[start:], len(text)


def scan(text: str) -> tuple[set[str], list[str]]:
    """Keys used in `text`, and the Thai left outside any call."""
    text = COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    text = IN_THAI.sub("", text)
    keys: set[str] = set()
    covered = [False] * len(text)
    for call in CALL.finditer(text):
        argument, end = first_argument(text, call.end())
        literals = [m.group(1).replace("\\'", "'") for m in LITERAL.finditer(argument)]
        if "?" in argument:            # tr(cond ? 'a' : 'b'): each is a key
            keys.update(k for k in literals if THAI.search(k))
        elif literals:                 # 'a' + 'b': one key over two lines
            keys.add("".join(literals))
        for i in range(call.start(), end):
            covered[i] = True
    loose = []
    for number, line in enumerate(text.split("\n"), 1):
        offset = sum(len(l) + 1 for l in text.split("\n")[: number - 1]) if THAI.search(line) else 0
        if any(THAI.match(ch) and not covered[offset + i] for i, ch in enumerate(line)):
            loose.append(f"{number}: {line.strip()[:110]}")
    return keys, loose


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    raw = CATALOGUE.read_text(encoding="utf-8")
    table: dict[str, str] = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])

    used: set[str] = set()
    problems = 0
    for path in SOURCES:
        keys, loose = scan(path.read_text(encoding="utf-8"))
        used |= keys
        for line in loose:
            problems += 1
            print(f"not translatable  {path.relative_to(ROOT)}:{line}")

    for path in sorted((ROOT / "app").rglob("*.py")):
        used.update(SERVER_CALL.findall(path.read_text(encoding="utf-8")))

    if "--keys" in sys.argv:
        for key in sorted(used):
            print(json.dumps(key, ensure_ascii=False))
        return 0

    for key in sorted(used - table.keys()):
        problems += 1
        print(f"missing in en.js  {json.dumps(key, ensure_ascii=False)}")
    for key in sorted(table.keys() - used):
        print(f"unused in en.js   {json.dumps(key, ensure_ascii=False)}")
    print(f"{len(used)} keys, {len(table)} translated, {problems} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
