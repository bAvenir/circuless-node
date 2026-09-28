"""Node configuration. Every value comes from the environment or a .env file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CIRCULESS_NODE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- identity ---------------------------------------------------------------------
    node_id: str = Field(
        description="This node's id. Tokens are accepted only for audience node:{node_id}, "
        "so it must match the Keycloak client scope exactly."
    )
    issuer: str = Field(
        default="http://127.0.0.1:8080/realms/circuless",
        description="Keycloak realm URL. Its JWKS is the only signature authority.",
    )
    cloud_api_url: str = Field(default="http://127.0.0.1:8081")
    node_client_id: str = Field(
        default="",
        description="Keycloak client this node authenticates as. Defaults to node-<node_id>.",
    )

    # --- storage ----------------------------------------------------------------------
    database_url: str = Field(
        default="sqlite:///./data/node.db",
        description="SQLite in the beta; a Postgres URL is a configuration change only.",
    )
    data_dir: Path = Field(
        default=Path("./data"),
        description="Root for resource content. Each tenant gets data_dir/<tenant_id>/.",
    )

    # --- interfaces -------------------------------------------------------------------
    # Two servers, not one. /healthz, /metrics, /internal/authz and the API docs must not be
    # reachable through the gateway (R8, D21), and the reliable way to guarantee that is to
    # serve them from a socket the gateway cannot reach — not a middleware check on a header
    # the caller controls.
    public_host: str = Field(default="0.0.0.0", description="Gateway- and overlay-facing.")
    public_port: int = Field(default=8000)
    internal_host: str = Field(
        default="127.0.0.1",
        description="Loopback, or the overlay address. Never the gateway-facing interface.",
    )
    internal_port: int = Field(default=8001)

    # --- browser access ---------------------------------------------------------------
    cors_allow_origins: list[str] = Field(
        default_factory=list,
        description="Exact origins, never '*' (G7). The Cloud UI and Workflows in practice.",
    )

    @field_validator("cors_allow_origins")
    @classmethod
    def reject_wildcard_origin(cls, origins: list[str]) -> list[str]:
        # Browser consumption is cross-origin and carries a bearer token, so a wildcard here
        # would let any site spend a user's node token.
        if "*" in origins:
            raise ValueError("cors_allow_origins must list exact origins, never '*'")
        return origins

    @field_validator("issuer", "cloud_api_url")
    @classmethod
    def strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="after")
    def default_client_id_from_node_id(self) -> Settings:
        # Derived rather than required: the two are the same thing in every deployment so
        # far, and one fewer value to get wrong on an install is worth the indirection.
        if not self.node_client_id:
            object.__setattr__(self, "node_client_id", f"node-{self.node_id}")
        return self

    @property
    def audience(self) -> str:
        """The only audience this node accepts (N2)."""
        return f"node:{self.node_id}"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
