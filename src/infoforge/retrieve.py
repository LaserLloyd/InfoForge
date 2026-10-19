"""Retrieval: question → ranked, fetched, truncated source articles.

Three proven ideas ported from localwiki.py, plus one new one.

Ported:
  * **keyword reduction** — kiwix's Xapian search ANDs every term in
    ``pattern``, so a natural-language question returns nothing.  Strip
    stopwords and weak verbs and a lone distinctive word does the work.
  * **title phrases + /suggest** — /suggest is a title prefix index, so the
    most title-like phrase in the question resolves straight to the article
    ("Flappy Bird", "Emu War", "Nineteen Eighty-Four") when full-text AND
    matching fails.
  * **progressive fallback** — retry with the 2-3 most distinctive keywords
    when the full pattern is too narrow.

New in v2:
  * **lexical rerank with junk demotion**.  v1 trusted kiwix's own ordering
    and took the first N, which let noise like a company page that happens to
    share one keyword outrank the article whose *title* matches the question.
    Now every candidate is scored on title overlap (weighted 3x), snippet
    overlap, exact-phrase and suggest bonuses, minus penalties for
    disambiguation pages, "List of ...", "Category:" and bare-year titles.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import requests

from .config import Config
from .extract import smart_truncate, strip_html
from .kiwix import Hit, content_url, fetch_article, search, suggest
from .logging_ import warn

# Stopwords + question words.  kiwix ANDs every term, so "What is the capital
# of X and when was it founded?" matches almost nothing until these go.
_STOPWORDS = frozenset("""
a about again all almost also am an and any are as at be been being both but
by can could did didi do does doing done each for from had has have having he
her hers him his how i if in into is it its just may me might more most much
must my no nor not of off on once only or other our ours out over own same she
should so some such than that the their theirs them then there these they this
those through to too under until up upon very was we were what when where
which while who whom why will with would you your yours
""".split())

# Weak / question verbs.  They add nothing to a title lookup but AND with the
# real content words: "Who created Flappy Bird and why did he remove it?" must
# reduce to "Flappy Bird", not to five terms that co-occur nowhere.
_WEAK_VERBS = frozenset("""
became become being called came cause caused causes create created creates
did happen happened happening happens known made make makes named novel
released remove removed removes removing say says said tell tells told went
won win wins wrote write writes written
""".split())

# Letters and digits of any script ("Galápagos", "Zürich", "東京"), not
# underscores; an ASCII-only class split "Galápagos" into "Gal" + "pagos".
_TOKEN_RE = re.compile(r"[^\W_]+(?:['-][^\W_]+)*")
_TITLE_WORD_RE = re.compile(r"(?:[^\W_]|')+")

# Number-to-words, read year-style, because Wikipedia titles novels and events
# in words: "1984" has to become "Nineteen Eighty Four" for /suggest to find
# "Nineteen Eighty-Four".
_NUM_WORDS = (
    "zero one two three four five six seven eight nine ten eleven twelve "
    "thirteen fourteen fifteen sixteen seventeen eighteen nineteen"
).split()
_NUM_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
             "eighty", "ninety")

MAX_TITLE_PHRASES = 12
MAX_SUGGEST_HITS = 3


@dataclass
class Source:
    """One grounded source handed to the LLM."""

    title: str
    url: str
    text: str
    score: float = 0.0


# ------------------------------------------------------------------ keywords

def keyword_query(question: str) -> str:
    """Reduce a question to bare content words (original order preserved).

    Falls back to the raw question when stripping would leave nothing — a
    question made entirely of stopwords still deserves an attempt.
    """
    tokens = _TOKEN_RE.findall(question)
    keywords = [
        t for t in tokens
        if t.lower() not in _STOPWORDS and t.lower() not in _WEAK_VERBS
    ]
    return " ".join(keywords) if keywords else question.strip()


def number_to_words(n: int) -> str:
    """Integer to words, reading 4-digit numbers as years.

    1984 -> "nineteen eighty four" (how Wikipedia titles it), not
    "one thousand nine hundred eighty four".
    """
    if n < 0 or n > 9999:
        return ""
    if 1000 <= n <= 9999:
        hi, lo = divmod(n, 100)
        hi_words = number_to_words(hi)
        lo_words = number_to_words(lo) if lo else ""
        return hi_words + (" " + lo_words if lo_words else "")
    if n < 20:
        return _NUM_WORDS[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _NUM_TENS[tens] + (" " + _NUM_WORDS[ones] if ones else "")
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        out = _NUM_WORDS[hundreds] + " hundred"
        return out + (" " + number_to_words(rest) if rest else "")
    return ""


def title_phrases(question: str, keywords: str) -> list[str]:
    """Ordered candidate *title* phrases for /suggest, most specific first.

    1. capitalized runs of >=2 words from the question ("the Solar System" ->
       "Solar System"); single-word runs are too noisy to be worth an HTTP call
       and full-text handles lone distinctive words well.
    2. spelled-out numbers ("1984" -> "Nineteen Eighty Four").
    3. contiguous sub-phrases of the keyword query, longest first.
    """
    tokens = _TOKEN_RE.findall(question)
    lows = [t.lower() for t in tokens]
    phrases: list[str] = []

    run: list[str] = []
    for tok, low in zip(tokens, lows):
        if low not in _STOPWORDS and low not in _WEAK_VERBS and tok[:1].isupper():
            run.append(tok)
        else:
            if len(run) >= 2:
                phrases.append(" ".join(run))
            run = []
    if len(run) >= 2:
        phrases.append(" ".join(run))

    for tok in tokens:
        if tok.isascii() and tok.isdigit():
            words = number_to_words(int(tok))
            if " " in words:
                phrases.append(" ".join(w.capitalize() for w in words.split()))

    kw = keywords.split()
    for size in range(len(kw), 0, -1):
        for i in range(len(kw) - size + 1):
            phrases.append(" ".join(kw[i:i + size]))

    seen: set[str] = set()
    out: list[str] = []
    for phrase in phrases:
        key = phrase.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(phrase)
        if len(out) >= MAX_TITLE_PHRASES:
            break
    return out


# ------------------------------------------------------------------- ranking

# Titles that are almost never the answer to a factual question.  Each costs
# the hit points rather than dropping it, so a penalised page can still win
# when nothing better exists.
_PENALTIES: tuple[tuple[re.Pattern[str], float, str], ...] = (
    (re.compile(r"\(disambiguation\)\s*$", re.I), 9.0, "disambiguation"),
    (re.compile(r"^\s*list of\b", re.I), 5.0, "list"),
    (re.compile(r"^\s*(category|template|portal|help|wikipedia|module|draft):", re.I),
     8.0, "namespace"),
    (re.compile(r"^\s*\d{3,4}(\s*(BC|AD|BCE|CE))?\s*$", re.I), 6.0, "year-only"),
    (re.compile(r"^\s*index of\b", re.I), 5.0, "index"),
    (re.compile(r"^\s*outline of\b", re.I), 3.0, "outline"),
)

# Body marker for a disambiguation page that is not titled as one.
_DISAMBIG_BODY = re.compile(r"\bmay (?:also )?refer to\b", re.I)

_SCORE_TITLE_TERM = 3.0     # a question keyword present in the title
_SCORE_SNIPPET_TERM = 1.0   # ... present in the search snippet
_SCORE_EXACT_PHRASE = 6.0   # the title *is* a title-phrase from the question
_SCORE_PHRASE_IN_TITLE = 2.5  # a title-phrase appears inside the title
_SCORE_SUGGEST = 7.0        # /suggest resolved this title exactly


def _terms(keywords: str) -> list[str]:
    """Scoring terms: content words of length > 2, deduped, lowercased."""
    seen: set[str] = set()
    out: list[str] = []
    for word in keywords.split():
        low = word.lower()
        if len(low) > 2 and low not in seen:
            seen.add(low)
            out.append(low)
    return out


def score_hit(hit: Hit, keywords: str, phrases: list[str]) -> float:
    """Lexical relevance score for one candidate (higher is better)."""
    terms = _terms(keywords)
    title_low = hit.title.lower()
    snippet_low = hit.snippet.lower()
    title_words = set(_TITLE_WORD_RE.findall(title_low))

    score = 0.0
    for term in terms:
        if term in title_words:
            score += _SCORE_TITLE_TERM
        elif term in title_low:
            # substring-only match (plurals, hyphenation) is worth less
            score += _SCORE_TITLE_TERM / 2
        if snippet_low and term in snippet_low:
            score += _SCORE_SNIPPET_TERM

    for phrase in phrases:
        low = phrase.lower()
        if not low:
            continue
        if title_low == low:
            score += _SCORE_EXACT_PHRASE
            break
        if low in title_low:
            score += _SCORE_PHRASE_IN_TITLE
            break

    if hit.from_suggest:
        score += _SCORE_SUGGEST

    for pattern, cost, _label in _PENALTIES:
        if pattern.search(hit.title):
            score -= cost

    return score


def rank_hits(hits: list[Hit], keywords: str, phrases: list[str],
              limit: int) -> list[Hit]:
    """Score, dedupe by content path and take the best ``limit`` hits.

    The sort is stable on the incoming order, so kiwix's own relevance
    ordering survives as the tiebreaker between equally-scoring hits.
    """
    deduped: dict[str, Hit] = {}
    for hit in hits:
        existing = deduped.get(hit.key)
        if existing is None:
            deduped[hit.key] = hit
        elif hit.from_suggest and not existing.from_suggest:
            # Same article reached two ways: keep the suggest provenance (it
            # is the stronger signal) but keep any snippet we already have.
            existing.from_suggest = True
            if not existing.snippet:
                existing.snippet = hit.snippet

    ordered = list(deduped.values())
    for hit in ordered:
        hit.score = score_hit(hit, keywords, phrases)
    ordered.sort(key=lambda h: -h.score)
    return ordered[:limit]


# ----------------------------------------------------------------- the search

def gather_hits(cfg: Config, question: str,
                session: requests.Session | None = None) -> list[Hit]:
    """Every candidate from every enabled ZIM, unranked."""
    keywords = keyword_query(question)
    phrases = title_phrases(question, keywords)
    hits: list[Hit] = []

    for zim in cfg.enabled_zims:
        book = zim.books_name

        # 1. exact-title lookups first.
        found = 0
        for phrase in phrases:
            hit = suggest(cfg, book, phrase, session=session)
            if hit:
                hits.append(hit)
                found += 1
                if found >= MAX_SUGGEST_HITS:
                    break

        # 2. full-text over the reduced keyword query.
        pool = search(cfg, book, keywords, cfg.search_pool, session=session)

        # 3. progressive fallback: too few hits usually means the AND was too
        #    narrow, so retry with the 2-3 most distinctive keywords
        #    (capitalized and long beats short and generic).
        if len(pool) < 2:
            ranked_words = sorted(
                keywords.split(), key=lambda w: (w[:1].isupper(), len(w)), reverse=True
            )
            tight = " ".join(ranked_words[:3])
            if tight and tight != keywords:
                pool = search(cfg, book, tight, cfg.search_pool, session=session) or pool

        hits.extend(pool)

    return hits


def retrieve(cfg: Config, question: str, max_articles: int | None = None,
             session: requests.Session | None = None) -> list[Source]:
    """Full retrieval: search, rank, fetch, clean, truncate.

    Fetching walks further down the ranked list than ``max_articles`` so that
    unfetchable pages and disambiguation stubs can be skipped without leaving
    the LLM short of sources.
    """
    want = max_articles or cfg.max_articles
    keywords = keyword_query(question)
    phrases = title_phrases(question, keywords)

    hits = gather_hits(cfg, question, session=session)
    # Rank a deeper slice than we need: some of it will be dropped below.
    ranked = rank_hits(hits, keywords, phrases, max(want * 2, want + 3))

    sources: list[Source] = []
    for index, hit in enumerate(ranked):
        if len(sources) >= want:
            break
        raw = fetch_article(cfg, hit.url, session=session)
        if raw is None:
            continue
        text = strip_html(raw)
        if not text:
            continue
        # A disambiguation page ("X may refer to ...") grounds nothing.  Skip
        # it only while an alternative exists — either one already collected or
        # one still to come; the last candidate standing is better than
        # answering with no sources at all.
        has_alternative = bool(sources) or index < len(ranked) - 1
        if _DISAMBIG_BODY.search(text[:400]) and has_alternative:
            warn(f"skipping disambiguation page: {hit.title}")
            continue
        sources.append(
            Source(
                title=hit.title,
                url=content_url(cfg, hit.url),
                text=smart_truncate(text, cfg.max_chars, keywords),
                score=hit.score,
            )
        )
    return sources
