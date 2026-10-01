"""Node configuration. Every value comes from the environment or a .env file."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from . import overlay


class NodeOperator(StrEnum):
    """Who runs this node (D22).

    A **BVR-operated node refuses `classification=sensitive`**. Not because BVR's node is
    less secure — it is the better-run of the two, today — but because BVR holding a
    partner's sensitive data on their behalf is the arrangement the project said it would
    not make. A partner's own node may hold whatever that partner decides.
    """

    BVR = "bvr"
    PARTNER = "partner"


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

    # --- what this node is allowed to hold ---------------------------------------------
    operator: NodeOperator = Field(
        default=NodeOperator.BVR,
        description="Who runs this node. A BVR-operated node refuses sensitive data (D22).",
    )

    # --- how to reach this node --------------------------------------------------------
    # Advertised by /.well-known/circuless-node (N12). Clients try the overlay first and
    # fall back to the gateway, so a client that can install a NetBird agent never
    # traverses the Cloud at all (§5.6).
    #
    # Configured here rather than learned from the Cloud, because this document is what
    # somebody reads while trying to reach a node that is having trouble — and a node
    # that could not describe itself until the Cloud told it how would be least useful
    # exactly then. The gateway address is therefore in two places, here and in the
    # Cloud's node registry that C3 generates routes from; they are reconciled by C12's
    # configuration rather than by either side guessing.
    overlay_base_url: str | None = Field(
        default=None,
        description="Override for this node's overlay address. Normally unset: the "
        "address is detected from the interface the NetBird agent created.",
    )
    gateway_base_url: str | None = Field(
        default=None,
        description="This node's public address through the Cloud gateway, e.g. "
        "https://alpha.nodes.circuless.eu. The fallback for browsers and for clients "
        "that cannot join the overlay.",
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
    # nosec B104 — binding all interfaces is the point: this socket is what the gateway
    # and the overlay reach. The endpoints that must NOT be public live on the internal
    # app, which binds loopback (R8).
    public_host: str = Field(  # nosec B104
        default="0.0.0.0",  # nosec B104
        description="Gateway- and overlay-facing.",
    )
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
    def effective_overlay_base_url(self) -> str | None:
        """What `/.well-known` publishes as this node's overlay address.

        Detected unless overridden. The failure worth preventing is a node advertising
        an address it is not on — a setting is a line in a partner's `.env` that nobody
        validates until a client cannot connect, and an interface is the truth.

        Not cached. The ioctl loop is a handful of syscalls, and a node that joined the
        overlay a minute after starting should say so without being restarted.
        """
        if self.overlay_base_url:
            return self.overlay_base_url
        return overlay.overlay_base_url(self.public_port)

    @property
    def refuses_sensitive(self) -> bool:
        """D22, defaulting closed.

        A node nobody has configured is treated as BVR-operated, so the cost of the
        mistake is a partner being told to set a flag — not BVR silently holding
        sensitive data it undertook not to hold. The permissive value is the one that has
        to be typed out.
        """
        return self.operator is NodeOperator.BVR

    @property
    def audience(self) -> str:
        """The only audience this node accepts (N2)."""
        return f"node:{self.node_id}"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
