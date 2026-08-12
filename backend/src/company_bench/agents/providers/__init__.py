"""External model-provider adapters."""

from company_bench.agents.providers.newapi import (
    NewApiAnthropicMessagesGateway,
    NewApiCapabilityProbe,
    NewApiConfig,
    NewApiGatewayFactory,
    NewApiGatewayType,
    NewApiModelConfig,
    NewApiModelGateway,
    NewApiResponsesGateway,
    NewApiTransport,
    NewApiWireProtocol,
)

__all__ = (
    "NewApiAnthropicMessagesGateway",
    "NewApiCapabilityProbe",
    "NewApiConfig",
    "NewApiGatewayFactory",
    "NewApiGatewayType",
    "NewApiModelConfig",
    "NewApiModelGateway",
    "NewApiResponsesGateway",
    "NewApiTransport",
    "NewApiWireProtocol",
)
