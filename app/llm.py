"""The boundary between this workflow and whatever model serves it.

Two calls, because the workflow only makes two kinds of request:

  read_with_citations   evidence in, cited prose out
  structured            prose in, a validated object out

They are separate methods rather than one flexible one because the Citations
API refuses to do both at once: enabling citations on a document and asking for
a structured output returns a 400. That constraint is the reason the graph
reads and structures in separate passes, so it is worth having it visible in
the type rather than buried in a call site.

Keeping this interface narrow is also the point at which model routing would
be handed to the corporate architecture engagement. Replacing AnthropicClient
with something that speaks to a shared gateway should not require touching a
single node.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Protocol, TypeVar

from pydantic import BaseModel

from .config import Settings

T = TypeVar("T", bound=BaseModel)

# Handed one dict per round trip: stage, prompt name, model, tokens, latency.
# Optional, and never inspected here — the client's job is to report what it
# did, not to decide where that goes.
Recorder = Callable[[dict], None]


class LLMClient(Protocol):
    async def read_with_citations(
        self, system: str, instruction: str, documents: list[dict[str, Any]]
    ) -> Any:
        """Return raw response content: text blocks, some carrying citations."""
        ...

    async def structured(
        self, system: str, instruction: str, schema: type[T], fast: bool = False
    ) -> T:
        """Return an instance of schema. No documents, so no citation conflict.

        `fast` routes mechanical work — segmenting a document, say — to the
        smaller model, leaving the larger one for the judgment calls.
        """
        ...


class AnthropicClient:
    """LLMClient backed by Claude.

    The reading pass runs on a smaller model: it is mechanical work — restate
    what a document says, cite it — and the citations come from the API rather
    than from the model's judgment. The reasoning passes, where the actual
    difficulty lives, run on the larger one.
    """

    def __init__(self, settings: Settings, recorder: Recorder | None = None) -> None:
        from langchain_anthropic import ChatAnthropic

        common = {
            "api_key": settings.require_api_key(),
            # A generation that is allowed to be long is also allowed to be
            # slow. Removing the output cap moves the failure from "structured
            # object truncated mid-field" to "request timed out and retried
            # three times", so the timeout has to move with it.
            "timeout": 600,
            "max_retries": 3,
        }
        # Omitted unless configured: the client then requests the model's full
        # output ceiling rather than a number we picked.
        if settings.max_tokens is not None:
            common["max_tokens"] = settings.max_tokens

        reasoner_model = settings.resolved_model()
        reader_model = settings.resolved_reader_model()
        self._reader = ChatAnthropic(
            model=reader_model, **common, **_controls(reader_model, settings)
        )
        self._reasoner = ChatAnthropic(
            model=reasoner_model, **common, **_controls(reasoner_model, settings)
        )
        self._reader_model = reader_model
        self._reasoner_model = reasoner_model
        self._recorder = recorder

    def recording_to(self, recorder: Recorder) -> "AnthropicClient":
        """Report every round trip to `recorder`. Returns self, so it chains.

        Set after construction rather than in `__init__` because the client is
        built once and the trail it reports to belongs to a particular run.
        """
        self._recorder = recorder
        return self

    def _record(self, **fields: Any) -> None:
        """Report one round trip. A broken ledger must not break a run."""
        if self._recorder is None:
            return
        try:
            self._recorder(fields)
        except Exception:  # noqa: BLE001 - the trail is never worth a failed draft
            pass

    async def check(self) -> None:
        """Smallest possible round trip, to tell a bad key from a working one.

        Worth its few tokens: without it the first sign of a wrong key is a
        failed run several model calls in, which reads like the workflow is
        broken rather than the credentials.
        """
        await self._reader.ainvoke(
            [{"role": "user", "content": "Reply with the single word: ok"}],
            max_tokens=8,
        )

    async def read_with_citations(
        self, system: str, instruction: str, documents: list[dict[str, Any]]
    ) -> Any:
        started = time.perf_counter()
        response = await self._reader.ainvoke(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": [*documents, {"type": "text", "text": instruction}]},
            ]
        )
        self._record(
            stage="read_document",
            prompt_name="read_cited",
            model=self._reader_model,
            latency_ms=_elapsed_ms(started),
            **_usage(response),
        )
        return response.content

    async def structured(
        self, system: str, instruction: str, schema: type[T], fast: bool = False
    ) -> T:
        # Defaults to forced tool use rather than the native structured-output
        # format. Either would work here — there are no documents attached to
        # these calls — but tool use is the portable choice.
        base = self._reader if fast else self._reasoner
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": instruction},
        ]

        if self._recorder is None:
            return await base.with_structured_output(schema).ainvoke(messages)

        # include_raw keeps the response object, which is the only place the
        # token counts and stop reason live. It also stops LangChain raising
        # on a parse failure, so the raise is re-created below: an audited run
        # and an unaudited one must fail in exactly the same way.
        started = time.perf_counter()
        result = await base.with_structured_output(schema, include_raw=True).ainvoke(messages)
        self._record(
            stage=_stage_for(schema),
            prompt_name=_stage_for(schema),
            model=self._reader_model if fast else self._reasoner_model,
            latency_ms=_elapsed_ms(started),
            **_usage(result.get("raw")),
        )
        if result.get("parsing_error"):
            raise result["parsing_error"]
        return result["parsed"]


# Model families that took `output_config.effort` in place of the sampling
# parameters. On these, temperature/top_p/top_k return a 400; on everything
# older, effort does. The two controls are mutually exclusive by model, so the
# choice cannot be left to configuration — it is a property of the model id.
#
# The line is at 4.7, not 4.6: Claude Opus 4.7 is where the sampling
# parameters were removed, and 4.6 still takes a temperature. 4.6 does have
# its own rule — temperature OR top_p, never both, or the request 400s — which
# costs nothing here because this client sends neither top_p nor top_k and a
# test holds that true.
_EFFORT_MODELS = (
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-sonnet-5",
    "claude-fable-5",
    "claude-mythos-5",
)


def takes_effort(model: str) -> bool:
    return model.startswith(_EFFORT_MODELS)


def _controls(model: str, settings: Settings) -> dict:
    """The one steering parameter this model accepts, and only that one.

    Sending both is not a fallback — whichever the model does not know about
    fails the request outright, so a wrong guess here costs the whole run
    rather than degrading it.
    """
    if takes_effort(model):
        return {"output_config": {"effort": settings.effort}}
    if settings.temperature is None:
        return {}
    return {"temperature": settings.temperature}


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _stage_for(schema: type) -> str:
    from .audit import STAGE_BY_SCHEMA

    return STAGE_BY_SCHEMA.get(schema.__name__, schema.__name__.lower())


def _usage(response: Any) -> dict:
    """Token counts and stop reason, wherever the SDK happens to put them.

    Defensive on purpose: a provider that stops reporting usage should cost
    the audit page two numbers, not the run.
    """
    if response is None:
        return {"input_tokens": None, "output_tokens": None, "stop_reason": None}

    usage = getattr(response, "usage_metadata", None) or {}
    metadata = getattr(response, "response_metadata", None) or {}
    if not usage:
        usage = metadata.get("usage") or {}

    return {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "stop_reason": metadata.get("stop_reason"),
    }


class ReadOnlyClient:
    """Enough of an LLMClient to open the graph for checkpoint reads.

    Review, evidence, and audit must work without a live key. Generation still
    requires AnthropicClient.
    """

    async def read_with_citations(
        self, system: str, instruction: str, documents: list[dict[str, Any]]
    ) -> Any:
        raise RuntimeError("No API key loaded — cannot call the model.")

    async def structured(
        self, system: str, instruction: str, schema: type[T], fast: bool = False
    ) -> T:
        raise RuntimeError("No API key loaded — cannot call the model.")
