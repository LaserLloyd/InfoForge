"""Prompt building, the LLM fallback chain, CoT stripping and citation checks.

Grounding contract: the model answers ONLY from the supplied sources, cites the
article titles it used, and says so plainly when the sources do not contain the
answer.  An honest "not in the sources" is the contract working — but it is NOT
an answer, so it must never leave the CLI as exit 0.

To make that machine-checkable the model opens its reply with a verdict marker,
``ANSWERED:`` or ``NOT_IN_SOURCES:`` (see ``parse_verdict``).  Small reasoning
models do not always comply, so a missing marker falls back to a refusal-phrase
regex on the reply's first sentence.  The citation check alone cannot catch a
refusal: "Eiffel Tower does not mention breakfast" names a source title.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

import requests

from .config import EXIT_CONFIG, Config, Endpoint, InfoForgeError, LLMUnreachableError
from .logging_ import event, warn
from .retrieve import Source

# Temperature is low but not zero: 0.0 makes some local builds loop on
# repetitive source text, and this is extraction, not creative writing.
TEMPERATURE = 0.3
MAX_TOKENS = 2048

# HTTP statuses worth trying the next endpoint for.  Everything else in the 4xx
# range is our own fault (bad model id, bad auth) and would fail identically
# on every endpoint, so it stops the chain with a pointed error instead.
# 409 used to be blanket-included here; per the integration contract a 409
# is never blanket-retried — its ``error.code`` decides (see _error_code /
# _TERMINAL_CODES / _HOLD_CODES below).
_RETRYABLE_STATUS = frozenset({408, 425})

# "Busy, come back shortly" — StudioForge's 503 priority_hold (a background-tier
# request waiting for a higher-priority model load) and a plain 429.  These are
# held out on the SAME endpoint, bounded by config hold_wait_s, before the
# chain advances: falling straight through to a weaker fallback model on every
# busy signal would silently downgrade answer quality exactly when the local
# server is in demand.
_HOLD_STATUS = frozenset({429, 503})
_HOLD_DEFAULT_DELAY_S = 10.0

# Integration contract: branch on the structured ``error.code``, never on
# prose. These codes are always a wait-and-retry, unchanged, on the SAME
# endpoint — never a reason to fall through to a weaker model.
_HOLD_CODES = frozenset({
    "priority_hold", "model_busy", "benchmark_busy", "model_benchmarking",
    "client_quota", "lease_vacating",
})
# gpu_leased is retry-safe only when the lease itself is not a benchmark lease
# (a benchmark lease means genuinely stand down — no polling loop at all).
_GPU_LEASED_CODE = "gpu_leased"
# Terminal: someone else holds the lease outright (a refusal, not a queue
# position); force is never sent and looping cannot help.
_TERMINAL_CODES = frozenset({"lease_conflict"})


class LLMRequestError(InfoForgeError):
    """A 4xx that means the request itself is wrong — not worth a fallback."""

    exit_code = EXIT_CONFIG


@dataclass
class Attempt:
    """One endpoint's turn in the chain (never carries the api_key)."""

    model_id: str
    base_url: str
    ok: bool
    error: str = ""


@dataclass
class Answer:
    text: str
    endpoint: Endpoint | None
    attempts: list[Attempt] = field(default_factory=list)
    cited: list[str] = field(default_factory=list)
    uncited: bool = False
    verdict: str = "answered"          # VERDICT_ANSWERED | VERDICT_NOT_IN_SOURCES
    verdict_source: str = "model"      # "model" (marker found) | "fallback" (regex)

    @property
    def not_in_sources(self) -> bool:
        return self.verdict == VERDICT_NOT_IN_SOURCES


# -------------------------------------------------------------------- prompt

PROMPT_HEADER = (
    "You are a question-answering assistant grounded ONLY in the source texts "
    "provided below.",
    "Answer the user's question using only information found in these sources. "
    "If the sources do not contain the answer, say so clearly.",
    'When you use information from a source, cite the article title (for '
    'example: "according to <title>").',
    "Do not invent facts, titles, or URLs that are not present in the sources.",
    "Begin your reply with exactly one of these two markers: "
    '"ANSWERED:" followed by the answer, if the sources contain it; or '
    '"NOT_IN_SOURCES:" followed by one sentence saying what is missing, if they '
    "do not.",
)


def build_prompt(question: str, sources: list[Source]) -> str:
    """Grounded prompt: the rules, then numbered sources, then the question."""
    lines: list[str] = [*PROMPT_HEADER, ""]
    for i, src in enumerate(sources, 1):
        lines.append(f"Source {i}: {src.title}")
        lines.append(src.text)
        lines.append("")
    lines.append(f"Question: {question}")
    return "\n".join(lines)


# ---------------------------------------------------------------- CoT strip

def strip_cot(text: str) -> str:
    """Remove chain-of-thought noise from a reply.

    Reasoning models leak <think>...</think> blocks (and sometimes a ```think
    fence, or a stray code fence around the whole answer) into the chat
    `content` field.  The printed answer must be the answer.
    """
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"<think>.*$", "", text, flags=re.DOTALL)  # unterminated block
    # R1 distills often omit the OPENING tag: everything up to a bare
    # </think> is reasoning, not answer.
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    text = re.sub(r"```think\s*.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"^\s*```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    return text.strip()


# ------------------------------------------------------------------ verdict

VERDICT_ANSWERED = "answered"
VERDICT_NOT_IN_SOURCES = "not_in_sources"

# The marker, tolerant of markdown bold/backticks, a "Verdict:" label, case,
# and spaces instead of underscores ("Not in sources:").  The LAST marker in
# the reply wins: a thinking preamble that leaked past strip_cot may rehearse
# a verdict before the model settles on one, and the settled one comes last.
_VERDICT_RE = re.compile(
    r"(?:^|(?<=[\s*`>\-#]))[*`_]*(?:verdict\s*[:\-]\s*)?[*`_]*"
    r"(ANSWERED|NOT[_ ]IN[_ ]SOURCES)[*`_]*\s*:[*`_]*\s*",
    re.IGNORECASE,
)

# Fallback when no marker is present: the reply's FIRST sentence reads as a
# refusal.  Only the first sentence, so an answer that merely notes a gap
# ("It was 1889. The sources do not give the day.") stays an answer.
_REFUSAL_RE = re.compile(
    r"\b(?:"
    r"(?:the\s+)?(?:provided\s+|given\s+|retrieved\s+)?(?:sources?|articles?|texts?)"
    r"\s+(?:do|does|did)\s*(?:not|n't)\s+"
    r"(?:contain|mention|include|provide|say|state|specify|cover|discuss|address|give)"
    r"|not\s+(?:found|mentioned|present|contained|covered|available|provided)\s+in\s+"
    r"(?:any\s+of\s+)?the\s+(?:provided\s+|given\s+)?(?:sources?|articles?|texts?)"
    r"|there\s+is\s+no\s+(?:information|mention|data|record)"
    r"|no\s+information\s+(?:about|on|regarding|is\s+(?:given|provided|available))"
    r"|(?:cannot|can't|can\s+not|unable\s+to)\s+(?:be\s+)?"
    r"(?:answer|determine|find|answered|determined|found)"
    r")",
    re.IGNORECASE,
)


@dataclass
class Verdict:
    verdict: str   # VERDICT_ANSWERED | VERDICT_NOT_IN_SOURCES
    source: str    # "model" | "fallback"
    text: str      # the reply with the marker (and anything before it) removed


def _first_sentence(text: str) -> str:
    match = re.search(r"[.!?](?:\s|$)", text)
    return text[:match.end()] if match else text


def parse_verdict(text: str) -> Verdict:
    """Read the model's grounding verdict out of an already CoT-stripped reply."""
    matches = list(_VERDICT_RE.finditer(text))
    if matches:
        last = matches[-1]
        word = re.sub(r"[_ ]", "_", last.group(1).upper())
        verdict = VERDICT_ANSWERED if word == "ANSWERED" else VERDICT_NOT_IN_SOURCES
        body = text[last.end():].strip()
        if not body:
            # Marker at the very end ("...\nNOT_IN_SOURCES:") — keep what came before.
            body = text[:last.start()].strip()
        return Verdict(verdict, "model", body)
    if _REFUSAL_RE.search(_first_sentence(text.strip())):
        return Verdict(VERDICT_NOT_IN_SOURCES, "fallback", text.strip())
    return Verdict(VERDICT_ANSWERED, "fallback", text.strip())


# ------------------------------------------------------------------ the call

def chat_body(cfg: Config, endpoint: Endpoint, prompt: str) -> dict:
    """The /chat/completions body for one endpoint."""
    body = {
        "model": endpoint.model_id,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
    }
    if cfg.studioforge:
        # StudioForge-only scheduling hint (higher = ahead of background work).
        # Other OpenAI-compatible servers reject or ignore unknown body keys,
        # so it is sent only when the config says the server is StudioForge.
        body["priority"] = 3
    return body


def _error_payload(resp) -> dict:
    """The dict a structured error body hangs its ``code`` off, or {} for a
    plain-text / non-JSON body (an older or foreign server)."""
    try:
        data = resp.json()
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    err = data.get("error")
    return err if isinstance(err, dict) else data


def _error_code(resp) -> str | None:
    """The stable ``error.code`` — the integration contract says branch on
    this, never on prose. None when the body carries no structured code at
    all."""
    code = _error_payload(resp).get("code")
    return code if isinstance(code, str) else None


def _gpu_leased_is_benchmark(resp) -> bool:
    """Whether a ``gpu_leased`` refusal's own lease ``kind`` is "benchmark" —
    that one case means genuinely stand down (no polling loop); any other
    kind is retry-safe."""
    payload = _error_payload(resp)
    lease = payload.get("lease")
    if not isinstance(lease, dict):
        lease = payload
    return lease.get("kind") == "benchmark"


def _hold_delay_s(resp) -> float:
    """How long the server asked us to wait: Retry-After header, then a JSON
    ``retry_after_s`` (StudioForge's shape), then a default."""
    raw = getattr(resp, "headers", None) or {}
    value = raw.get("Retry-After")
    if value is None:
        try:
            body = resp.json()
            value = body.get("retry_after_s") if isinstance(body, dict) else None
        except ValueError:
            value = None
    try:
        delay = float(value)
    except (TypeError, ValueError):
        return _HOLD_DEFAULT_DELAY_S
    return min(max(delay, 1.0), 120.0)


def call_endpoint(cfg: Config, endpoint: Endpoint, prompt: str,
                  session: requests.Session | None = None) -> str:
    """POST one chat request; return the raw reply content.

    Raises ConnectionError-ish ``requests`` exceptions and, for 5xx, a
    RuntimeError — both mean "try the next endpoint".  A 4xx that is not
    transient raises LLMRequestError and stops the chain.  A 503/429 "busy",
    or a structured ``error.code`` in the retry-safe set (``priority_hold``,
    ``model_busy``, ``benchmark_busy``, ``model_benchmarking``,
    ``client_quota``, ``lease_vacating``, or ``gpu_leased`` whose lease
    ``kind`` is not ``benchmark``), is waited out on this
    endpoint for up to ``cfg.hold_wait_s`` seconds (honouring Retry-After)
    before it counts as a failure. A 409 ``lease_conflict`` is terminal and
    is never blanket-retried like the rest of the 409 range used to be.
    """
    headers = {"Content-Type": "application/json"}
    if endpoint.api_key:
        headers["Authorization"] = f"Bearer {endpoint.api_key}"
    if cfg.studioforge:
        # Integration contract: every StudioForge caller names itself.
        # Sent only when the config says the server is StudioForge, same
        # gate as the `priority` body field below.
        headers["X-SF-Client"] = "infoforge"

    post = (session or requests).post
    deadline = time.monotonic() + cfg.hold_wait_s
    while True:
        resp = post(
            endpoint.chat_url,
            headers=headers,
            json=chat_body(cfg, endpoint, prompt),
            timeout=cfg.timeout_s,
        )
        code = _error_code(resp) if resp.status_code >= 400 else None
        if code in _TERMINAL_CODES:
            # e.g. 409 lease_conflict: someone else holds the lease outright.
            # Never blanket-retried — force is never sent and looping cannot
            # help.
            raise LLMRequestError(
                f"{endpoint.base_url} refused the request ({code}): HTTP "
                f"{resp.status_code}: {resp.text[:300]}"
            )
        hold_worthy = (
            resp.status_code in _HOLD_STATUS
            or code in _HOLD_CODES
            or (code == _GPU_LEASED_CODE and not _gpu_leased_is_benchmark(resp))
        )
        if hold_worthy:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    f"HTTP {resp.status_code}: still busy after waiting "
                    f"{cfg.hold_wait_s}s: {resp.text[:200]}"
                )
            delay = min(_hold_delay_s(resp), remaining)
            warn(f"{endpoint.model_id} busy (HTTP {resp.status_code}"
                 f"{f' {code}' if code else ''}), retrying in {delay:.0f}s")
            time.sleep(delay)
            continue
        break

    if resp.status_code >= 500 or resp.status_code in _RETRYABLE_STATUS:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    if resp.status_code >= 400:
        raise LLMRequestError(
            f"{endpoint.base_url} rejected the request: HTTP {resp.status_code}: "
            f"{resp.text[:300]}\n"
            f"  Check model_id ({endpoint.model_id!r}) and api_key in your config."
        )

    try:
        payload = resp.json()
    except ValueError:
        raise RuntimeError("non-JSON response from the LLM") from None
    try:
        message = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"unexpected LLM response shape: {exc}") from None

    # Some reasoning servers put everything in reasoning_content when the
    # answer is short; an empty content there is a formatting quirk, not a
    # refusal, so fall back to it before giving up.
    content = (message.get("content") or message.get("reasoning_content") or "").strip()
    if not content:
        raise RuntimeError("empty reply from the LLM")
    return content


def ask_llm(cfg: Config, prompt: str,
            session: requests.Session | None = None) -> tuple[str, Endpoint, list[Attempt]]:
    """Walk the endpoint chain: primary, then each fallback.

    Only transport-level failures advance the chain.  A model that answers
    "the sources do not contain this" has done its job and is returned as-is.
    """
    attempts: list[Attempt] = []
    for endpoint in cfg.endpoints:
        try:
            content = call_endpoint(cfg, endpoint, prompt, session=session)
        except LLMRequestError:
            attempts.append(Attempt(endpoint.model_id, endpoint.base_url, False,
                                    "request rejected"))
            event("llm_attempt", model_id=endpoint.model_id,
                  base_url=endpoint.base_url, ok=False, error="rejected")
            raise
        except (requests.exceptions.RequestException, RuntimeError) as exc:
            attempts.append(Attempt(endpoint.model_id, endpoint.base_url, False, str(exc)))
            event("llm_attempt", model_id=endpoint.model_id,
                  base_url=endpoint.base_url, ok=False, error=str(exc))
            warn(f"LLM {endpoint.model_id} at {endpoint.base_url} failed: {exc}")
            continue
        attempts.append(Attempt(endpoint.model_id, endpoint.base_url, True))
        event("llm_attempt", model_id=endpoint.model_id,
              base_url=endpoint.base_url, ok=True)
        return content, endpoint, attempts

    tried = ", ".join(f"{a.model_id}@{a.base_url}" for a in attempts) or "nothing"
    raise LLMUnreachableError(
        f"every configured LLM was unreachable (tried: {tried})\n"
        "  The sources found are printed above — search still worked; only the "
        "answering step failed.\n"
        "  Check that the endpoint is up and that base_url/model_id are right."
    )


# --------------------------------------------------------------- citations

def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def cited_titles(answer_text: str, sources: list[Source]) -> list[str]:
    """Which source titles the answer actually mentions.

    Punctuation- and case-insensitive, because models re-punctuate titles
    ("Nineteen Eighty Four" for "Nineteen Eighty-Four").
    """
    haystack = _normalize(answer_text)
    return [src.title for src in sources if _normalize(src.title) and
            _normalize(src.title) in haystack]


UNCITED_WARNING = (
    "WARNING: the answer does not cite any of the retrieved sources — treat it "
    "as ungrounded."
)


def answer_question(cfg: Config, question: str, sources: list[Source],
                    session: requests.Session | None = None) -> Answer:
    """Build the prompt, run the chain, strip CoT, read the verdict and check
    citations."""
    prompt = build_prompt(question, sources)
    content, endpoint, attempts = ask_llm(cfg, prompt, session=session)
    verdict = parse_verdict(strip_cot(content))
    if verdict.source == "fallback":
        warn(f"{endpoint.model_id} gave no ANSWERED:/NOT_IN_SOURCES: marker; "
             f"verdict inferred from phrasing ({verdict.verdict})")
    cited = cited_titles(verdict.text, sources)
    return Answer(
        text=verdict.text,
        endpoint=endpoint,
        attempts=attempts,
        cited=cited,
        uncited=not cited,
        verdict=verdict.verdict,
        verdict_source=verdict.source,
    )
