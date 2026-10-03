"""Thai or English, chosen per browser.

Thai is the language the dashboard is written in, so the Thai text in the
templates and scripts is itself the key: `_("ภาพรวม")` renders as written in
Thai and is looked up in one catalogue, static/i18n/en.js, for English. A
string missing from the catalogue falls back to Thai rather than to nothing.

The same catalogue serves both sides: the browser loads it as a script (only
when the viewer chose English), and this module reads the object out of that
file for the text Jinja renders. `scripts/check_i18n.py` lists what is missing.

Where one Thai word needs two English ones ("เปิดอยู่" is "on" for a lamp and
"open" for a window) the key carries a hint after `##`, which never shows.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from app.config import STATIC_DIR

from jinja2 import pass_context
from starlette.requests import HTTPConnection

log = logging.getLogger(__name__)

LANGS = ("th", "en")
DEFAULT_LANG = "th"
LANG_COOKIE = "lang"
CONTEXT_MARK = "##"

CATALOGUE_PATH = STATIC_DIR / "i18n" / "en.js"

_catalogue: tuple[float, dict[str, str]] = (0.0, {})


def catalogue() -> dict[str, str]:
    """The Thai -> English table, re-read when the file changes (one stat)."""
    global _catalogue
    try:
        mtime = CATALOGUE_PATH.stat().st_mtime
        if mtime != _catalogue[0]:
            text = CATALOGUE_PATH.read_text(encoding="utf-8")
            # The file is `window.I18N = {...};` -- the object is plain JSON.
            table = json.loads(text[text.index("{"): text.rindex("}") + 1])
            _catalogue = (mtime, table)
    except (OSError, ValueError) as exc:
        log.warning("cannot read %s (%s); English falls back to Thai", CATALOGUE_PATH, exc)
        _catalogue = (_catalogue[0] or -1.0, _catalogue[1])
    return _catalogue[1]


def lang_of(connection: HTTPConnection) -> str:
    """The language this browser asked for; Thai unless it chose otherwise."""
    lang = connection.cookies.get(LANG_COOKIE)
    return lang if lang in LANGS else DEFAULT_LANG


def translate(lang: str, text: str, **values: object) -> str:
    out = catalogue().get(text) if lang == "en" else None
    if out is None:
        out = text.split(CONTEXT_MARK, 1)[0]
    for name, value in values.items():
        out = out.replace("{" + name + "}", str(value))
    return out


@pass_context
def jinja_translate(context, text: str, **values: object) -> str:
    return translate(context.get("lang", DEFAULT_LANG), text, **values)
