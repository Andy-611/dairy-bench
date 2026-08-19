"""Public interface for NewAPI-backed company decision gateways."""

from company_bench.agents.providers.newapi.config import (
    DEFAULT_NEWAPI_BASE_URL,
    DEFAULT_NEWAPI_ENV_PREFIX,
    NewApiConfig,
    NewApiModelConfig,
    NewApiWireProtocol,
)
from company_bench.agents.providers.newapi.gateway import (
    NewApiAnthropicMessagesGateway,
    NewApiCapabilityProbe,
    NewApiGatewayFactory,
    NewApiGatewayType,
    NewApiModelGateway,
    NewApiResponsesGateway,
)
from company_bench.agents.providers.newapi.transport import NewApiTransport
from company_bench.agents.providers.newapi.wire import NewApiRequestFeatures

__all__ = (
    "DEFAULT_NEWAPI_BASE_URL",
    "DEFAULT_NEWAPI_ENV_PREFIX",
    "NewApiAnthropicMessagesGateway",
    "NewApiCapabilityProbe",
    "NewApiConfig",
    "NewApiGatewayFactory",
    "NewApiGatewayType",
    "NewApiModelConfig",
    "NewApiModelGateway",
    "NewApiRequestFeatures",
    "NewApiResponsesGateway",
    "NewApiTransport",
    "NewApiWireProtocol",
)
