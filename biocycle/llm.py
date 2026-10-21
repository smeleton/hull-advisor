"""The one wrapper for every Claude call: retries, refusal handling, token + cost logging."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anthropic
from dotenv import load_dotenv

from biocycle.config import (
    LLM_FALLBACK_BETA,
    LLM_MAX_RETRIES,
    LLM_TIMEOUT_S,
    LLM_USAGE_LOG,
    MODEL,
    TOKEN_PRICES_PER_MTOK,
)

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """A Claude call failed in a way the UI should show to the user."""


@dataclass(frozen=True)
class LLMResult:
    text: str
    model: str  # model that served the response (may differ if a fallback ran)
    input_tokens: int
    output_tokens: int
    cost_usd: float
    stop_reason: str | None
    request_id: str | None


def make_client() -> anthropic.Anthropic:
    load_dotenv()
    return anthropic.Anthropic(max_retries=LLM_MAX_RETRIES, timeout=LLM_TIMEOUT_S)


def token_cost(model: str, input_tokens: int, output_tokens: int,
               cache_write: int = 0, cache_read: int = 0) -> float:
    """Estimated $ for one model's tokens. Unknown models are priced as the configured MODEL."""
    in_price, out_price = TOKEN_PRICES_PER_MTOK.get(model, TOKEN_PRICES_PER_MTOK[MODEL])
    return (
        input_tokens * in_price
        + cache_write * in_price * 1.25
        + cache_read * in_price * 0.10
        + output_tokens * out_price
    ) / 1_000_000


def estimate_cost(usage: Any, requested_model: str) -> float:
    """Sum per-iteration costs when present (a fallback bills at its own model's rates)."""
    iterations = getattr(usage, "iterations", None)
    parts = iterations if iterations else [usage]
    return sum(
        token_cost(
            getattr(it, "model", None) or requested_model,
            it.input_tokens or 0,
            it.output_tokens or 0,
            getattr(it, "cache_creation_input_tokens", 0) or 0,
            getattr(it, "cache_read_input_tokens", 0) or 0,
        )
        for it in parts
    )


def log_usage(result: LLMResult, purpose: str, path: Path | None = None) -> None:
    path = path or LLM_USAGE_LOG
    entry = {"ts": datetime.now(timezone.utc).isoformat(), "purpose": purpose, **asdict(result)}
    entry.pop("text")
    log.info("LLM %s: %s in / %s out tokens, $%.4f (%s)", purpose, result.input_tokens,
             result.output_tokens, result.cost_usd, result.model)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:  # logging must never break the app
        log.warning("could not write usage log: %s", e)


def complete(
    system: str,
    user: str,
    *,
    max_tokens: int,
    effort: str,
    purpose: str,
    client: anthropic.Anthropic | None = None,
) -> LLMResult:
    """Send one system + user message and return the text with token and cost accounting."""
    client = client or make_client()
    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": effort},
            betas=[LLM_FALLBACK_BETA],
            fallbacks="default",
        )
    except anthropic.AuthenticationError as e:
        raise LLMError("Claude rejected the API key. Set ANTHROPIC_API_KEY in .env.") from e
    except anthropic.PermissionDeniedError as e:
        raise LLMError(f"This API key can't use {MODEL}.") from e
    except anthropic.NotFoundError as e:
        raise LLMError(f"Model {MODEL} was not found.") from e
    except anthropic.RateLimitError as e:
        raise LLMError("Rate limited by the Claude API after retries. Try again shortly.") from e
    except anthropic.APIStatusError as e:
        raise LLMError(f"Claude API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise LLMError("Could not reach the Claude API. Check your network.") from e
    except TypeError as e:  # the SDK raises TypeError when no credentials resolve at all
        if "authentication" not in str(e):
            raise
        raise LLMError("No Claude credentials found. Set ANTHROPIC_API_KEY in .env.") from e

    usage = response.usage
    result = LLMResult(
        text="".join(b.text for b in response.content if b.type == "text"),
        model=response.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=estimate_cost(usage, MODEL),
        stop_reason=response.stop_reason,
        request_id=getattr(response, "_request_id", None),
    )
    log_usage(result, purpose)

    if response.stop_reason == "refusal":
        raise LLMError("Claude declined to write this report.")
    if response.stop_reason == "max_tokens":
        raise LLMError("The report was cut off at the token limit. Try again.")
    if not result.text.strip():
        raise LLMError("Claude returned an empty response.")
    return result
