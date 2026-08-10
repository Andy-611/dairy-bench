"""Small deterministic Adapters used by contract and integration tests."""

from collections.abc import Callable, Iterable

from pydantic import TypeAdapter

from company_bench.agents.contracts import (
    CommandModelRequest,
    CommandModelResult,
    ModelOutputError,
)
from company_bench.agents.providers.capabilities import (
    ModelCapabilityCatalog,
    verified_capabilities,
)
from company_bench.domain.models import PolicyKind, PolicyMetadata
from company_bench.runtime.models import AgentTurn, CompanyCommand

_COMMAND_ADAPTER = TypeAdapter(CompanyCommand)


def model_capability_catalog(limits: dict[str, int]) -> ModelCapabilityCatalog:
    """Return a no-I/O catalog of gateway-confirmed model limits."""
    return ModelCapabilityCatalog(None, None, seeds=verified_capabilities(limits))


class FixedCommandAgent:
    """Return commands from one deterministic iterable."""

    metadata = PolicyMetadata(name="fixed-command", kind=PolicyKind.BASELINE)

    def __init__(self, commands: Iterable[CompanyCommand]) -> None:
        self._commands = iter(commands)

    async def act(self, _: AgentTurn) -> CompanyCommand:
        try:
            return next(self._commands)
        except StopIteration as error:
            raise ValueError("fixed Agent command stream is exhausted") from error


class ScriptedModelGateway:
    """Adapt a deterministic command factory to the provider Gateway Interface."""

    provider = "scripted"

    def __init__(self, command_factory: Callable[[CommandModelRequest], CompanyCommand]) -> None:
        self._command_factory = command_factory
        self.command_requests: list[CommandModelRequest] = []

    async def generate_command(self, request: CommandModelRequest) -> CommandModelResult:
        self.command_requests.append(request)
        command = _COMMAND_ADAPTER.validate_python(self._command_factory(request))
        if command.kind not in request.allowed_commands:
            raise ModelOutputError(f"command {command.kind} is not allowed")
        return CommandModelResult(
            command=command,
            provider=self.provider,
            model="scripted-current",
        )

    async def close(self) -> None:
        """Release no resources."""
