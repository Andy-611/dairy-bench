"""Codex runtime adapter behind the provider-neutral model gateway."""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Literal, Protocol, Self

from openai_codex import (
    ApprovalMode,
    AsyncCodex,
    CodexConfig,
    Sandbox,
    TurnResult,
    is_retryable_error,
)
from openai_codex.types import (
    GetAccountResponse,
    ReasoningEffort,
    ReasoningSummary,
    TurnStatus,
)
from pydantic import Field, ValidationError

from company_bench.agent_models import (
    DecisionModel,
    DecisionSubmission,
    ModelGateway,
    ModelInfrastructureError,
    ModelOutputError,
    ModelRequest,
    ModelResult,
)
from company_bench.codex_artifacts import (
    CodexArtifactIdentity,
    CodexArtifactSink,
    CodexArtifactStore,
)
from company_bench.diagnostics import bounded_error
from company_bench.models import StrictModel
from company_bench.run_models import TokenUsage

type CodexReasoningEffort = Literal["low", "medium", "high", "xhigh"]

_BASE_INSTRUCTIONS = (
    "You are a business decision engine inside a controlled benchmark. "
    "Do not inspect files, run commands, browse, call tools, or seek outside information. "
    "Reason only from the structured company observation supplied by the benchmark. "
    "Write any exposed reasoning summary in English."
)
_CONFIG_OVERRIDES = (
    "features.memories=false",
    "features.memory_tool=false",
    "features.multi_agent=false",
    "features.remote_plugin=false",
    "features.search_tool=false",
    "features.shell_tool=false",
    "features.tool_search=false",
    "features.web_search=false",
    "features.workspace_dependencies=false",
    "include_apps_instructions=false",
    "include_collaboration_mode_instructions=false",
    "include_environment_context=false",
    "include_permissions_instructions=false",
    "hide_agent_reasoning=false",
    "show_raw_agent_reasoning=true",
)


class _SavedThread(Protocol):
    """Persisted SDK thread metadata used only to locate its Session."""

    path: str | None


class _ThreadReadResponse(Protocol):
    """Narrow result returned by ``thread/read``."""

    thread: _SavedThread


class _CodexThread(Protocol):
    """Narrow SDK thread surface used by the gateway."""

    id: str

    async def run(
        self,
        input: str,
        *,
        approval_mode: ApprovalMode,
        effort: ReasoningEffort,
        model: str,
        output_schema: dict[str, object],
        sandbox: Sandbox,
        summary: ReasoningSummary,
    ) -> TurnResult:
        """Execute one structured benchmark turn."""
        ...

    async def read(self, *, include_turns: bool = False) -> _ThreadReadResponse:
        """Return persisted thread metadata."""
        ...


class _CodexClient(Protocol):
    """Narrow SDK client surface used by the gateway."""

    async def __aenter__(self) -> Self:
        """Start the local Codex runtime."""
        ...

    async def account(self, *, refresh_token: bool = False) -> GetAccountResponse:
        """Return the active Codex account."""
        ...

    async def thread_start(
        self,
        *,
        approval_mode: ApprovalMode,
        base_instructions: str,
        cwd: str,
        developer_instructions: str,
        ephemeral: bool,
        model: str,
        sandbox: Sandbox,
    ) -> _CodexThread:
        """Create one isolated decision thread."""
        ...

    async def close(self) -> None:
        """Stop the local Codex runtime."""
        ...


class CodexAgentConfig(StrictModel):
    """Validated server-only configuration for Codex company Agents."""

    model: str = Field(default="gpt-5.6-sol", min_length=1)
    reasoning_effort: CodexReasoningEffort = "low"
    timeout_seconds: float = Field(default=180.0, gt=0, le=900)
    max_attempts: int = Field(default=2, ge=1, le=3)
    codex_home: Path | None = None

    @classmethod
    def from_environment(cls) -> CodexAgentConfig | None:
        """Load Codex only after the backend operator explicitly enables it."""
        if not _environment_flag("DAIRY_BENCH_CODEX_ENABLED"):
            return None
        codex_home = os.getenv("DAIRY_BENCH_CODEX_HOME", "").strip()
        return cls(
            model=os.getenv("DAIRY_BENCH_CODEX_MODEL", "gpt-5.6-sol"),
            reasoning_effort=os.getenv(
                "DAIRY_BENCH_CODEX_REASONING_EFFORT",
                "low",
            ),
            timeout_seconds=float(os.getenv("DAIRY_BENCH_CODEX_TIMEOUT_SECONDS", "180")),
            max_attempts=int(os.getenv("DAIRY_BENCH_CODEX_MAX_ATTEMPTS", "2")),
            codex_home=Path(codex_home) if codex_home else None,
        )

    @property
    def fingerprint(self) -> str:
        """Identify behavior-affecting settings without exposing credentials."""
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()[:16]

    def runtime_config(self, cwd: Path) -> CodexConfig:
        """Build the isolated local runtime launch configuration."""
        environment = {"CODEX_HOME": str(self.codex_home)} if self.codex_home is not None else None
        return CodexConfig(
            config_overrides=_CONFIG_OVERRIDES,
            cwd=str(cwd),
            env=environment,
        )


class CodexModelGateway(ModelGateway):
    """Generate typed decisions through one company-owned Codex runtime."""

    provider = "codex"

    def __init__(
        self,
        config: CodexAgentConfig,
        company_id: str,
        client: _CodexClient | None = None,
        artifact_sink: CodexArtifactSink | None = None,
    ) -> None:
        self.config = config
        self._workspace = TemporaryDirectory(prefix=f"dairy-bench-{company_id}-")
        self._client = client or AsyncCodex(config.runtime_config(Path(self._workspace.name)))
        self._artifact_sink = artifact_sink or CodexArtifactStore.from_environment()
        self._closed = False
        self._started = False

    async def generate(
        self,
        request: ModelRequest,
        output_type: type[DecisionModel],
    ) -> ModelResult:
        """Run one persistent structured Codex turn with bounded retries."""
        started = monotonic()
        await self._ensure_started()
        submission_type = DecisionSubmission[output_type]
        for attempt in range(1, self.config.max_attempts + 1):
            try:
                async with asyncio.timeout(self.config.timeout_seconds):
                    thread = await self._client.thread_start(
                        approval_mode=ApprovalMode.deny_all,
                        base_instructions=_BASE_INSTRUCTIONS,
                        cwd=self._workspace.name,
                        developer_instructions=request.instructions,
                        ephemeral=False,
                        model=self.config.model,
                        sandbox=Sandbox.read_only,
                    )
                    result = await thread.run(
                        request.input_text,
                        approval_mode=ApprovalMode.deny_all,
                        effort=ReasoningEffort(self.config.reasoning_effort),
                        model=self.config.model,
                        output_schema=_strict_output_schema(submission_type),
                        sandbox=Sandbox.read_only,
                        summary=ReasoningSummary(root="detailed"),
                    )
            except Exception as error:
                if attempt == self.config.max_attempts or not _retryable(error):
                    raise _infrastructure_error(error) from error
                await asyncio.sleep(0.25 * 2 ** (attempt - 1))
            else:
                break
        else:
            raise AssertionError("bounded retry loop did not terminate")

        identity = CodexArtifactIdentity(
            invocation_id=request.invocation_id,
            run_id=request.run_id,
            company_id=request.observation.company_id,
            day=request.observation.day,
            model=self.config.model,
            thread_id=thread.id,
            turn_id=result.id,
        )
        try:
            await asyncio.to_thread(
                self._artifact_sink.export,
                identity,
                result,
                await _session_path(thread),
            )
        except Exception as error:
            raise _infrastructure_error(error) from error
        try:
            submission = _parse_submission(result, submission_type)
        except ModelOutputError as error:
            raise ModelOutputError(
                str(error),
                request_id=thread.id,
                response_id=result.id,
                usage=_token_usage(result),
                attempts=attempt,
                latency_ms=int((monotonic() - started) * 1000),
            ) from error
        return ModelResult(
            decision=submission.decision,
            provider=self.provider,
            model=self.config.model,
            response_id=result.id,
            request_id=thread.id,
            usage=_token_usage(result),
            attempts=attempt,
            latency_ms=int((monotonic() - started) * 1000),
        )

    async def close(self) -> None:
        """Close the runtime before deleting its isolated workspace."""
        if self._closed:
            return
        self._closed = True
        try:
            await self._client.close()
        finally:
            self._started = False
            self._workspace.cleanup()

    async def _ensure_started(self) -> None:
        """Start the runtime once and reject missing ChatGPT authentication."""
        if self._started:
            return
        if self._closed:
            raise ModelInfrastructureError("Codex gateway is already closed")
        try:
            await self._client.__aenter__()
            self._started = True
            account = await self._client.account(refresh_token=True)
        except Exception as error:
            await self.close()
            raise _infrastructure_error(error) from error
        if account.account is None:
            await self.close()
            raise ModelInfrastructureError(
                "Codex is not logged in; run `codex login` as the backend user"
            )


async def _session_path(thread: _CodexThread) -> Path | None:
    """Return the runtime-owned Session path when the SDK exposes it."""
    try:
        saved = await thread.read()
    except Exception:
        return None
    return Path(saved.thread.path) if saved.thread.path else None


def _parse_submission(
    result: TurnResult,
    submission_type: type[DecisionSubmission[DecisionModel]],
) -> DecisionSubmission[DecisionModel]:
    """Validate the final response against the role-specific envelope."""
    if result.status is not TurnStatus.completed:
        message = result.error.message if result.error is not None else result.status.value
        raise ModelInfrastructureError(f"Codex turn did not complete: {message}")
    if result.final_response is None:
        raise ModelOutputError("Codex returned no final structured response")
    try:
        return submission_type.model_validate_json(result.final_response)
    except (ValidationError, ValueError) as error:
        raise ModelOutputError(f"invalid Codex structured output: {error}") from error


def _strict_output_schema(
    submission_type: type[DecisionSubmission[DecisionModel]],
) -> dict[str, object]:
    """Normalize Pydantic JSON Schema to the strict Codex subset."""
    schema = submission_type.model_json_schema()
    _normalize_schema_node(schema)
    return schema


def _normalize_schema_node(node: object) -> None:
    """Require all fields and defer unsupported regex checks to Pydantic."""
    if isinstance(node, dict):
        node.pop("pattern", None)
        properties = node.get("properties")
        if isinstance(properties, dict):
            node["required"] = list(properties)
        for child in node.values():
            _normalize_schema_node(child)
    elif isinstance(node, list):
        for child in node:
            _normalize_schema_node(child)


def _token_usage(result: TurnResult) -> TokenUsage:
    """Normalize Codex turn counters into benchmark audit fields."""
    if result.usage is None:
        return TokenUsage()
    usage = result.usage.total
    return TokenUsage(
        input_tokens=usage.input_tokens,
        cached_tokens=usage.cached_input_tokens,
        output_tokens=usage.output_tokens,
        reasoning_tokens=usage.reasoning_output_tokens,
        total_tokens=usage.total_tokens,
    )


def _environment_flag(name: str) -> bool:
    """Parse an opt-in Boolean environment variable."""
    value = os.getenv(name, "").strip().lower()
    if value in {"", "0", "false", "no", "off"}:
        return False
    if value in {"1", "true", "yes", "on"}:
        return True
    raise ValueError(f"{name} must be true or false")


def _retryable(error: Exception) -> bool:
    """Retry SDK overloads and local timeouts only."""
    return isinstance(error, TimeoutError) or is_retryable_error(error)


def _infrastructure_error(error: Exception) -> ModelInfrastructureError:
    """Return a bounded runtime diagnostic without prompt or credentials."""
    if isinstance(error, ModelInfrastructureError):
        return error
    return ModelInfrastructureError(bounded_error(error, 300))
