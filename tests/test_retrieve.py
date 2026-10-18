"""Retrieval: keyword reduction, title phrases, ranking and junk demotion."""

from __future__ import annotations

from infoforge import retrieve
from infoforge.kiwix import Hit
from infoforge.retrieve import (
    keyword_query,
    number_to_words,
    rank_hits,
    title_phrases,
)

# ------------------------------------------------------------------ keywords

def test_keyword_query_strips_stopwords_and_weak_verbs():
    q = "Who created Flappy Bird and why did he remove it from app stores?"
    assert keyword_query(q) == "Flappy Bird app stores"


def test_keyword_query_keeps_distinctive_words():
    assert keyword_query("What happened in the Emu War and who won?") == "Emu War"


def test_keyword_query_falls_back_to_the_raw_question():
    # A question made entirely of stopwords still deserves an attempt.
    assert keyword_query("what is it") == "what is it"


def test_number_to_words_reads_four_digits_as_a_year():
    assert number_to_words(1984) == "nineteen eighty four"
    assert number_to_words(2000) == "twenty"  # 20 hundreds, no remainder
    assert number_to_words(42) == "forty two"
    assert number_to_words(300) == "three hundred"
    assert number_to_words(100000) == ""


# -------------------------------------------------------------- title phrases

def test_title_phrases_lift_capitalized_runs_first():
    q = "Who created Flappy Bird?"
    phrases = title_phrases(q, keyword_query(q))
    assert phrases[0] == "Flappy Bird"


def test_title_phrases_spell_out_years():
    q = "What is the novel 1984 about?"
    assert "Nineteen Eighty Four" in title_phrases(q, keyword_query(q))


def test_title_phrases_include_shrinking_subphrases():
    q = "largest planet Solar System"
    phrases = title_phrases(q, keyword_query(q))
    # The capitalized run is the most title-like candidate, so it leads;
    # keyword sub-phrases follow, longest first.
    assert phrases[0] == "Solar System"
    assert phrases.index("largest planet Solar System") < \
        phrases.index("largest planet")
    assert phrases.index("largest planet") < phrases.index("planet")


def test_title_phrases_are_deduped_and_bounded():
    q = "a b c d e f g h i j k l m n o p"
    phrases = title_phrases(q, keyword_query(q))
    assert len(phrases) <= retrieve.MAX_TITLE_PHRASES
    assert len(phrases) == len(set(p.lower() for p in phrases))


# ------------------------------------------------------------------- ranking

def rank(hits, question, limit=5):
    kw = keyword_query(question)
    return rank_hits(hits, kw, title_phrases(question, kw), limit)


def test_title_match_beats_incidental_keyword_noise():
    # The real defect this rerank exists for: a company page that merely
    # mentions the keyword used to outrank the article named after it.
    question = "What was the Emu War?"
    hits = [
        Hit(url="/content/w/Iris_Capital", title="Iris Capital",
            snippet="an investor in the emu farming sector"),
        Hit(url="/content/w/Emu_War", title="Emu War",
            snippet="the Emu War was a 1932 wildlife management operation"),
    ]
    ranked = rank(hits, question)
    assert [h.title for h in ranked] == ["Emu War", "Iris Capital"]


def test_suggest_hits_outrank_full_text_hits():
    question = "Tell me about the Solar System"
    hits = [
        Hit(url="/content/w/Formation_of_the_Solar_System",
            title="Formation of the Solar System", snippet="solar system"),
        Hit(url="/content/w/Solar_System", title="Solar System",
            from_suggest=True),
    ]
    assert rank(hits, question)[0].title == "Solar System"


def test_disambiguation_titles_are_demoted():
    question = "What is Mercury?"
    hits = [
        Hit(url="/content/w/Mercury_dis", title="Mercury (disambiguation)",
            snippet="Mercury may refer to"),
        Hit(url="/content/w/Mercury_planet", title="Mercury (planet)",
            snippet="Mercury is the smallest planet"),
    ]
    assert rank(hits, question)[0].title == "Mercury (planet)"


def test_list_and_namespace_titles_are_demoted():
    question = "What is the tallest mountain?"
    hits = [
        Hit(url="/content/w/List", title="List of mountains",
            snippet="tallest mountain list"),
        Hit(url="/content/w/Cat", title="Category:Mountains",
            snippet="tallest mountain category"),
        Hit(url="/content/w/Everest", title="Mount Everest",
            snippet="the tallest mountain above sea level"),
    ]
    assert rank(hits, question)[0].title == "Mount Everest"


def test_bare_year_titles_are_demoted():
    question = "What happened in the 1932 Emu War?"
    hits = [
        Hit(url="/content/w/1932", title="1932", snippet="events of 1932 emu war"),
        Hit(url="/content/w/Emu_War", title="Emu War", snippet="1932 operation"),
    ]
    assert rank(hits, question)[0].title == "Emu War"


def test_duplicate_paths_are_merged_keeping_suggest_provenance():
    question = "Emu War"
    hits = [
        Hit(url="/content/w/Emu_War", title="Emu War", snippet="from search"),
        Hit(url="/content/w/emu_war", title="Emu War", from_suggest=True),
    ]
    ranked = rank(hits, question)
    assert len(ranked) == 1
    assert ranked[0].from_suggest is True
    assert ranked[0].snippet == "from search"  # snippet survives the merge


def test_ranking_respects_the_limit():
    hits = [Hit(url=f"/content/w/A{i}", title=f"Article {i}") for i in range(10)]
    assert len(rank(hits, "article", limit=3)) == 3


def test_ties_keep_the_incoming_kiwix_order():
    # kiwix's own relevance ordering is the tiebreaker; the sort must be stable.
    hits = [Hit(url=f"/content/w/X{i}", title="Same Title") for i in range(4)]
    ranked = rank(hits, "unrelated question", limit=4)
    assert [h.url for h in ranked] == [h.url for h in hits]


# -------------------------------------------------------------- full pipeline

class StubSession:
    """Serves canned suggest/search/article responses by URL shape."""

    def __init__(self, search_hits, articles, suggest_payload=None):
        self.search_hits = search_hits
        self.articles = articles
        self.suggest_payload = suggest_payload or []

    def get(self, url, params=None, timeout=None, headers=None):
        from conftest import FakeResponse, search_xml
        if "/suggest" in url:
            return FakeResponse(200, json_data=self.suggest_payload)
        if "/search" in url:
            return FakeResponse(200, search_xml(*self.search_hits))
        for path, body in self.articles.items():
            if url.endswith(path):
                return FakeResponse(200, body)
        return FakeResponse(404)


def test_retrieve_fetches_cleans_and_truncates(cfg):
    session = StubSession(
        search_hits=[("Emu War", "/content/wikipedia_en_all_nopic/Emu_War",
                      "1932 operation")],
        articles={"/Emu_War": "<p>The Emu War was a 1932 operation.</p>"},
    )
    sources = retrieve.retrieve(cfg, "What was the Emu War?", session=session)
    assert len(sources) == 1
    assert sources[0].title == "Emu War"
    assert sources[0].text == "The Emu War was a 1932 operation."
    assert sources[0].url.startswith("http://127.0.0.1:18080/content/")


def test_retrieve_skips_a_disambiguation_body_when_alternatives_exist(cfg):
    session = StubSession(
        search_hits=[
            ("Mercury", "/content/wikipedia_en_all_nopic/Mercury", "mercury"),
            ("Mercury (planet)", "/content/wikipedia_en_all_nopic/Mercury_planet",
             "the smallest planet Mercury"),
        ],
        articles={
            "/Mercury": "<p>Mercury may refer to several things.</p>",
            "/Mercury_planet": "<p>Mercury is the smallest planet.</p>",
        },
    )
    sources = retrieve.retrieve(cfg, "What is the planet Mercury?", session=session)
    assert [s.title for s in sources] == ["Mercury (planet)"]


def test_retrieve_returns_nothing_when_search_is_empty(cfg):
    session = StubSession(search_hits=[], articles={})
    assert retrieve.retrieve(cfg, "nothing at all", session=session) == []


def test_retrieve_walks_past_unfetchable_hits(cfg):
    session = StubSession(
        search_hits=[
            ("Broken", "/content/wikipedia_en_all_nopic/Broken", "emu war"),
            ("Emu War", "/content/wikipedia_en_all_nopic/Emu_War", "emu war 1932"),
        ],
        articles={"/Emu_War": "<p>The Emu War happened in 1932.</p>"},
    )
    sources = retrieve.retrieve(cfg, "Emu War", max_articles=1, session=session)
    assert [s.title for s in sources] == ["Emu War"]
