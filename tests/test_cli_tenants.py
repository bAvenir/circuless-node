"""`circuless-node tenants` (N13 adjacent, Q4).

The command exists for the moment an install is not working, so it has to work when
nothing else does: no token, no Cloud, no running server. That is most of what these
assert — and the empty case especially, because "no tenants" is the normal state of a
fresh node and the unhelpful answer is to print nothing.
"""

from __future__ import annotations

import uuid

import pytest
from sqlmodel import Session, SQLModel, create_engine

from circuless_node.__main__ import _tenants_command, main
from circuless_node.models import Tenant
from circuless_node.settings import Settings


@pytest.fixture
def node(tmp_path) -> Settings:  # noqa: ANN001
    settings = Settings(  # type: ignore[call-arg]
        node_id="alpha-onprem",
        issuer="https://auth.example/realms/circuless",
        database_url=f"sqlite:///{tmp_path / 'node.db'}",
        data_dir=tmp_path / "data",
    )
    engine = create_engine(settings.database_url)
    SQLModel.metadata.create_all(engine, tables=[Tenant.__table__])
    return settings


def add(settings: Settings, slug: str) -> None:
    with Session(create_engine(settings.database_url)) as session:
        session.add(
            Tenant(
                org_id=uuid.uuid5(uuid.NAMESPACE_DNS, slug),
                group_path=f"/orgs/{slug}",
                slug=slug,
            )
        )
        session.commit()


def test_it_lists_what_the_node_serves(node: Settings, capsys) -> None:  # noqa: ANN001
    add(node, "beta")
    add(node, "alpha")

    assert _tenants_command(node) == 0

    lines = capsys.readouterr().out.splitlines()
    assert [line.split("\t")[0] for line in lines] == ["alpha", "beta"], "not sorted by slug"


def test_each_line_carries_what_an_operator_needs_to_check(node: Settings, capsys) -> None:  # noqa: ANN001
    """`group_path` especially: ownership is parsed from it, so a tenant that nobody can
    administer looks exactly like a working one until somebody tries."""
    add(node, "alpha")
    _tenants_command(node)

    slug, group_path, org_id = capsys.readouterr().out.strip().split("\t")
    assert (slug, group_path) == ("alpha", "/orgs/alpha")
    assert uuid.UUID(org_id)


def test_no_tenants_says_where_they_come_from(node: Settings, capsys) -> None:  # noqa: ANN001
    """The normal state of a fresh node, and the moment someone is most likely to run
    this. Printing nothing would leave them looking on the wrong machine — tenants are
    set in the Cloud's registry and arrive by sync, not created here."""
    assert _tenants_command(node) == 0

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "PUT /v1/nodes/<node_uuid>/tenants" in captured.err
    assert "circuless-node check" in captured.err


def test_it_opens_no_network_connection(node: Settings, monkeypatch) -> None:  # noqa: ANN001
    """The property the command exists for, asserted rather than described.

    A version that called `GET /v1/tenants` would need a node-audienced token from a
    Keycloak that may be exactly what is broken, on a node that may not be
    authenticating yet. So: make every socket raise, and the command still answers.
    """
    import socket

    def refuse(*_args, **_kwargs):
        raise AssertionError("the tenants command opened a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)

    add(node, "alpha")
    assert _tenants_command(node) == 0


def test_the_command_is_reachable_from_the_command_line(monkeypatch, node: Settings) -> None:  # noqa: ANN001
    """The dispatch table and the parser's `choices` are two lists that have to agree,
    and nothing else checks that `tenants` is in both."""
    import circuless_node.__main__ as entry

    monkeypatch.setattr(entry, "get_settings", lambda: node)
    monkeypatch.setattr("sys.argv", ["circuless-node", "tenants"])

    assert main() == 0
