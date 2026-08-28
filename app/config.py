"""Runtime configuration.

Everything that changes between runs lives here. DATA_DIR is the important
one: it is the whole input surface, so pointing at a different folder is all
it takes to run the workflow against different evidence.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_DIR = Path(__file__).parent
PROJECT_DIR = PACKAGE_DIR.parent

# The evidence pack shipped with the build, so a workspace emptied by a
# director can be put back without a terminal.
DEMO_PACK_DIR = PACKAGE_DIR / "demo_pack"

# What the Settings dialog offers. A free-text box asked the director to know
# an exact model id, and a typo there is not a validation error — it is a run
# that fails four passes in with a 404 from the API.
#
# The list is deliberately short. Every entry is a currently-served model, and
# the reasoning/reading split below is the same judgement the defaults make:
# the larger models carry the judgement, the smaller ones read.
MODEL_CHOICES: list[tuple[str, str]] = [
    ("claude-opus-5", "Claude Opus 5 — deepest reasoning"),
    ("claude-opus-4-8", "Claude Opus 4.8 — previous Opus"),
    ("claude-opus-4-6", "Claude Opus 4.6 — takes a temperature"),
    ("claude-sonnet-5", "Claude Sonnet 5 — near-Opus, faster"),
    ("claude-sonnet-4-6", "Claude Sonnet 4.6 — previous Sonnet"),
    ("claude-haiku-4-5", "Claude Haiku 4.5 — fastest, mechanical work"),
]


def model_options(current: str) -> list[tuple[str, str]]:
    """The dropdown, with whatever is configured guaranteed to be in it.

    A model set in `.env` that predates this list must still be selectable, or
    opening Settings would silently propose changing it.
    """
    options = list(MODEL_CHOICES)
    if current and current not in {value for value, _ in options}:
        options.insert(0, (current, f"{current} — from configuration"))
    return options


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str = ""

    # The reasoning passes carry the judgement, so they run on the larger
    # model. The reading pass is mechanical — restate what a document says,
    # cite it, with the citations coming from the API rather than the model —
    # so it runs on the smaller one.
    #
    # Both are 4.6 rather than the 5 line because 4.6 is the newest generation
    # that still accepts a temperature; see `temperature` below.
    model: str = "claude-opus-4-6"
    reader_model: str = "claude-sonnet-4-6"
    # Unset, so the request carries the model's full output ceiling and a
    # structured object is never cut off mid-field. Thinking counts against
    # this budget on Claude Opus 5, and nothing here handles a truncated
    # response — it surfaces as a parse failure that costs a retry or the run.
    # The ceiling is not a target: unused headroom is not billed, so a low
    # number bought only that failure mode. Set an integer to cap it.
    max_tokens: int | None = None

    # Sampling temperature, for models that still take one. 0 narrows the
    # distribution as far as the API allows.
    #
    # It does not make a run reproducible, and never did on any Claude model —
    # two runs at 0 over identical evidence can still differ. What actually
    # made this pipeline's output move between runs was claim ordering, which
    # is fixed deterministically in `ordered_claims`.
    #
    # Ignored on any model that rejects it: temperature, top_p and top_k were
    # removed on Claude Opus 4.7 and everything after it, where sending one
    # returns a 400. 4.6 is the last generation that takes a temperature, and
    # takes it only without top_p — this client sends neither top_p nor top_k,
    # and a test on the built payload holds that true. See
    # `AnthropicClient._controls`.
    temperature: float | None = 0.0

    # How hard the model works before answering: low | medium | high | xhigh | max.
    #
    # The control that replaced temperature on the newer models, and only sent
    # to those — an older model 400s on it just as a newer one 400s on
    # temperature. Whichever is in force, the other is not sent at all.
    # Lower effort scopes the work more tightly to what was asked: fewer and
    # more consolidated tool calls, less exploration, less spread between two
    # runs over the same evidence.
    #
    # This was medium for one release and the compose pass started returning
    # objects with fields missing — narratives filled with placeholders, the
    # support reasons dropped entirely — which surfaces to the director as an
    # empty Key_Challenge and a draft that could not be made valid. Cheaper
    # deliberation against a schema that had just grown is what broke. The
    # latency that buys back is now recovered by splitting compose into
    # concurrent calls instead, which does not cost accuracy.
    effort: str = "high"

    data_dir: Path = PROJECT_DIR / "data"
    runs_dir: Path = PROJECT_DIR / "runs"

    # The quarter being drafted. Not derived from today's date: a director
    # submitting late is drafting last quarter, and guessing that wrong is
    # worse than asking.
    quarter: str = "2026-Q3"

    # How many times compose may be sent back over a repairable validation
    # failure before the problem goes to the director instead.
    max_repair_attempts: int = 2

    @property
    def evidence_dir(self) -> Path:
        return self.data_dir / "evidence"

    @property
    def objective_file(self) -> Path:
        return self.data_dir / "objective.md"

    @property
    def prior_update_file(self) -> Path:
        return self.data_dir / "prior_update.md"

    @property
    def understanding_dir(self) -> Path:
        """Cached document understanding, keyed by content hash."""
        return self.runs_dir / "understanding"

    @property
    def checkpoint_db(self) -> Path:
        return self.runs_dir / "checkpoints.db"

    @property
    def workspace_db(self) -> Path:
        """The queryable index over every run. See db.py for why it exists."""
        return self.runs_dir / "workspace.db"

    def run_dir(self, thread_id: str) -> Path:
        return self.runs_dir / thread_id

    def resolved_model(self) -> str:
        """The reasoning model, Settings dialog winning over configuration."""
        return runtime_model() or self.model

    def resolved_reader_model(self) -> str:
        """The reading model, Settings dialog winning over configuration."""
        return runtime_reader_model() or self.reader_model

    def resolved_api_key(self) -> str:
        """The key, in precedence order. Required — there is no offline mode."""
        return (
            runtime_api_key()
            or self.anthropic_api_key
            or os.environ.get("ANTHROPIC_API_KEY", "")
        )

    def require_api_key(self) -> str:
        key = self.resolved_api_key()
        if not key:
            raise MissingAPIKey(
                "ANTHROPIC_API_KEY is not set.\n"
                "Copy .env.example to .env and add a key, or export it:\n"
                "  export ANTHROPIC_API_KEY=sk-ant-..."
            )
        return key


class MissingAPIKey(RuntimeError):
    """Raised when the workflow is asked to run without a key."""


# --- a key supplied through the interface ------------------------------------
# Held in this process and nowhere else. Not written to .env, not logged, not
# echoed back to the page, and gone when the server stops. A web form that
# persists a secret to disk is a bigger problem than the convenience is worth,
# and for a local single-user tool a process-lifetime key is enough.

_runtime_api_key: str = ""
_runtime_model: str = ""
_runtime_reader_model: str = ""


def set_runtime_model(model: str) -> None:
    """Model chosen in Settings. Same lifetime as the key: this process only."""
    global _runtime_model
    _runtime_model = model.strip()


def runtime_model() -> str:
    return _runtime_model


def set_runtime_reader_model(model: str) -> None:
    """The reading model, chosen separately.

    Two dropdowns rather than one because the two passes are different work.
    Putting the reading pass on the reasoning model costs money for nothing;
    putting the reasoning passes on the reading model costs judgement.
    """
    global _runtime_reader_model
    _runtime_reader_model = model.strip()


def runtime_reader_model() -> str:
    return _runtime_reader_model


def set_runtime_api_key(key: str) -> None:
    global _runtime_api_key
    _runtime_api_key = key.strip()


def clear_runtime_api_key() -> None:
    global _runtime_api_key
    _runtime_api_key = ""


def runtime_api_key() -> str:
    return _runtime_api_key


def mask(key: str) -> str:
    """Enough to recognise which key is loaded, not enough to use it."""
    if not key:
        return ""
    return f"{key[:7]}…{key[-4:]}" if len(key) > 15 else "…" + key[-4:]


settings = Settings()
