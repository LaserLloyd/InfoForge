"""Prompt building, the LLM fallback chain, CoT stripping and citation checks.

Grounding contract: the model answers ONLY from the supplied sources, cites the
article titles it used, and says so plainly when the sources do not contain the
answer.  That last case is a *successful* run — an honest "not in the sources"
is the whole point of a grounded assistant.
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
_RETRYABLE_STATUS = frozenset({408, 409, 425})

# "Busy, come back shortly" — StudioForge's 503 priority_hold (a background-tier
# request waiting for a higher-priority model load) and a plain 429.  These are
# held out on the SAME endpoint, bounded by config hold_wait_s, before the
# chain advances: falling straight through to a weaker fallback model on every
# busy signal would silently downgrade answer quality exactly when the rig is
# in demand.
_HOLD_STATUS = frozenset({429, 503})
_HOLD_DEFAULT_DELAY_S = 10.0


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


# -------------------------------------------------------------------- prompt

PROMPT_HEADER = (
    "You are a question-answering assistant grounded ONLY in the source texts "
    "provided below.",
    "Answer the user's question using only information found in these sources. "
    "If the sources do not contain the answer, say so clearly.",
    'When you use information from a source, cite the article title (for '
    'example: "according to <title>").',
    "Do not invent facts, titles, or URLs that are not present in the sources.",
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
    text = re.sub(r"```think\s*.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"^\s*```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    return text.strip()


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
    transient raises LLMRequestError and stops the chain.  A 503/429 "busy"
    is waited out on this endpoint for up to ``cfg.hold_wait_s`` seconds
    (honouring Retry-After) before it counts as a failure.
    """
    headers = {"Content-Type": "application/json"}
    if endpoint.api_key:
        headers["Authorization"] = f"Bearer {endpoint.api_key}"

    post = (session or requests).post
    deadline = time.monotonic() + cfg.hold_wait_s
    while True:
        resp = post(
            endpoint.chat_url,
            headers=headers,
            json=chat_body(cfg, endpoint, prompt),
            timeout=cfg.timeout_s,
        )
        if resp.status_code in _HOLD_STATUS:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    f"HTTP {resp.status_code}: still busy after waiting "
                    f"{cfg.hold_wait_s}s: {resp.text[:200]}"
                )
            delay = min(_hold_delay_s(resp), remaining)
            warn(f"{endpoint.model_id} busy (HTTP {resp.status_code}), "
                 f"retrying in {delay:.0f}s")
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
    """Build the prompt, run the chain, strip CoT and check citations."""
    prompt = build_prompt(question, sources)
    content, endpoint, attempts = ask_llm(cfg, prompt, session=session)
    text = strip_cot(content)
    cited = cited_titles(text, sources)
    return Answer(
        text=text,
        endpoint=endpoint,
        attempts=attempts,
        cited=cited,
        uncited=not cited,
    )
