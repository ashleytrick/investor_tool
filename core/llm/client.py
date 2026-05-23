"""JSON-validated LLM calls with retry-on-bad-json and pydantic validation.

Every LLM output in this system goes through complete_json: the model is asked
for JSON, the text is parsed, and the result is validated against a pydantic
schema. Malformed JSON or schema-invalid output triggers up to 3 retries with a
progressively stricter prompt, then raises.

Stub mode: when no ANTHROPIC_API_KEY is resolvable for the workspace, the client
runs offline. Callers must supply a `stub_response` dict; it is validated
against the same schema so the data shape is still proven end to end.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Type, TypeVar

from pydantic import BaseModel, ValidationError
from tenacity import retry, stop_after_attempt, wait_exponential

from core.config_loader import Workspace
from core.llm.limiter import get_sync_limiter

T = TypeVar("T", bound=BaseModel)

# Default model IDs; override per call or via workspace config.
MODEL_BATCH = "claude-sonnet-4-6"
MODEL_EMAIL = "claude-opus-4-7"


class LLMError(RuntimeError):
    """Raised when the model cannot produce schema-valid JSON after retries."""


@dataclass
class LLMUsage:
    calls_made: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, in_tok: int, out_tok: int) -> None:
        self.calls_made += 1
        self.input_tokens += in_tok
        self.output_tokens += out_tok


@dataclass
class LLMClient:
    workspace: Workspace
    usage: LLMUsage = field(default_factory=LLMUsage)
    _api_key: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._api_key = self.workspace.env("ANTHROPIC_API_KEY")

    @property
    def stub(self) -> bool:
        """True when no API key is available; calls run offline."""
        return not self._api_key

    def complete_json(
        self,
        prompt: str,
        schema: Type[T],
        *,
        model: str = MODEL_BATCH,
        max_tokens: int = 2048,
        max_retries: int = 3,
        stub_response: dict | None = None,
    ) -> T:
        """Return a validated pydantic instance of `schema`.

        In stub mode, validates and returns `stub_response`. In live mode, calls
        the model and retries up to `max_retries` times on bad JSON / bad schema.
        """
        if self.stub:
            if stub_response is None:
                raise LLMError(
                    "LLM client is in stub mode (no ANTHROPIC_API_KEY) and no "
                    "stub_response was supplied for this call."
                )
            self.usage.calls_made += 1
            return schema.model_validate(stub_response)

        last_err: Exception | None = None
        attempt_prompt = prompt
        for attempt in range(1, max_retries + 1):
            raw = self._raw_call(attempt_prompt, model, max_tokens)
            try:
                payload = _extract_json(raw)
                return schema.model_validate(payload)
            except (json.JSONDecodeError, ValidationError, ValueError) as err:
                last_err = err
                attempt_prompt = (
                    f"{prompt}\n\n"
                    f"Your previous response (attempt {attempt}) was not valid. "
                    f"Error: {err}. Return ONLY a single valid JSON object that "
                    f"matches the requested schema. No prose, no code fences."
                )
        raise LLMError(
            f"Model failed to produce schema-valid JSON after {max_retries} "
            f"attempts. Last error: {last_err}"
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, max=16))
    def _raw_call(self, prompt: str, model: str, max_tokens: int) -> str:
        """Single Anthropic call. tenacity retries transient network failures.

        Acquires the process-wide sync rate limiter for the 'anthropic'
        provider before each call so concurrent workspace runs in the same
        process share one bucket (Acceptance Criterion 17).
        """
        import anthropic

        get_sync_limiter("anthropic").acquire()
        client = anthropic.Anthropic(api_key=self._api_key)
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        self.usage.add(resp.usage.input_tokens, resp.usage.output_tokens)
        return "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        )


def _extract_json(text: str) -> dict:
    """Pull a JSON object out of model text, tolerating code fences."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[1]
        if cleaned.lstrip().lower().startswith("json"):
            cleaned = cleaned.lstrip()[4:]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("No JSON object found in model output.")
    return json.loads(cleaned[start : end + 1])
