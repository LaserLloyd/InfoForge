"""HTML → plain text, and keyword-aware truncation of an article.

Ported from localwiki.py, where both behaviours were proven against real
Wikipedia ZIM articles.  The interesting one is :func:`smart_truncate`: plain
head-truncation regularly cut off the sentence that actually answers the
question, because the answer often lives deep inside a long article.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

# Tags whose start implies a line break in the extracted text, so sentences
# from different blocks do not fuse into one run-on string.
_BLOCK_TAGS = frozenset(
    ("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6")
)

# Content of these elements is machine noise, never article prose.
_DROP_TAGS = frozenset(("script", "style"))


class _TextExtractor(HTMLParser):
    """Collect text, inserting breaks at block boundaries, skipping script/style."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _DROP_TAGS:
            # Break here too: dropping a <script> between two words must not
            # fuse them into one token.
            self.parts.append("\n")
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _DROP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)


def strip_html(html_text: str) -> str:
    """Strip tags and collapse whitespace.

    Falls back to a regex strip when HTMLParser chokes: ZIM articles are
    machine-generated and occasionally malformed, and half a source beats no
    source at all.
    """
    if not html_text:
        return ""
    if "<" not in html_text:
        text = html_text
    else:
        parser = _TextExtractor()
        try:
            parser.feed(html_text)
            parser.close()
            text = "".join(parser.parts)
        except Exception:
            text = re.sub(r"<[^>]+>", " ", html_text)
    return re.sub(r"\s+", " ", text).strip()


def smart_truncate(text: str, max_chars: int, keywords: str) -> str:
    """Fit an article into ``max_chars`` while keeping the answer passage.

    Keeps a short lede (a quarter of the budget — enough for "X is a ...")
    plus a window around the passage that best matches the question's content
    words.  Candidate phrases are ranked by how *rare* they are in this
    article: the rarer, the more likely they pinpoint the answer, so "largest
    planet" wins over the ubiquitous "Solar System".  Longer n-grams of
    consecutive keywords break ties.
    """
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text

    words = [w for w in keywords.split() if len(w) > 3]
    low = text.lower()

    def score(phrase: str):
        return (low.count(phrase.lower()), -len(phrase.split()))

    best_phrase, best_pos = None, -1
    for n in range(min(3, len(words)), 0, -1):
        grams = [" ".join(words[i:i + n]) for i in range(len(words) - n + 1)]
        seen: set[str] = set()
        uniq = [g for g in grams if not (g.lower() in seen or seen.add(g.lower()))]
        for _, gram in sorted((score(g), g) for g in uniq):
            pos = low.find(gram.lower())
            if pos >= 0:
                best_phrase, best_pos = gram, pos
                break
        if best_phrase:
            break

    if best_phrase is None or best_pos < 0 or best_pos < max_chars:
        # No useful phrase, or the match already lives inside the plain prefix.
        return text[:max_chars].rstrip() + " …"

    head_len = max_chars // 4
    head = text[:head_len]
    win = max_chars - head_len - 3
    start = max(0, best_pos - win // 5)
    mid = text[start:start + win]
    return head + "\n[…] " + mid.rstrip() + " …"
