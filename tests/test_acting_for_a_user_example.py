"""`examples/acting-for-a-user` — the difference between the two patterns.

The example shows a backend calling a node two ways, and **one grant type apart** is
the whole security story: as itself, the node decides against the service's
organisation; acting for a person, against the person's. Getting that wrong does not
fail — it succeeds, against the wrong organisation, and the access log names the wrong
party.

Running it needs a Keycloak, so these check what can be checked without one: that the
two paths really are different in the way that matters, and that the example does not
leak the key it generates.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "acting-for-a-user" / "act.py"


@pytest.fixture(scope="module")
def source() -> str:
    return EXAMPLE.read_text()


@pytest.fixture(scope="module")
def functions(source: str) -> dict[str, str]:
    """Each top-level function's own source, so a check about one cannot pass because
    of something written in another."""
    tree = ast.parse(source)
    lines = source.splitlines()
    return {
        node.name: "\n".join(lines[node.lineno - 1 : node.end_lineno])
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }


def test_acting_as_itself_sends_no_subject_token(functions: dict[str, str]) -> None:
    """Pattern B is client credentials and nothing else. A subject token here would
    silently make it pattern C — the request would succeed, and the access log would
    name a person who never asked for anything."""
    body = functions["as_itself"]

    assert "client_credentials" in body
    assert "subject_token" not in body
    assert "EXCHANGE_GRANT" not in body


def test_acting_for_a_person_exchanges(functions: dict[str, str]) -> None:
    """And pattern C must *be* an exchange. Falling back to client credentials when the
    exchange is refused would be the worst possible repair: it works, against the
    service's own organisation, with the person's name nowhere."""
    body = functions["for_user"]

    assert "EXCHANGE_GRANT" in body
    assert "subject_token" in body
    assert "client_credentials" not in body


def test_both_prove_the_client_by_signing(functions: dict[str, str]) -> None:
    """`private_key_jwt` (D18). No client secret exists anywhere in this flow, so a
    path that expected one could never work — and would send someone looking for a
    secret to copy."""
    for name in ("as_itself", "for_user"):
        assert "client_assertion" in functions[name], name
    assert "client_secret" not in "".join(functions.values())


def test_the_private_key_is_written_once_and_never_printed(
    functions: dict[str, str], source: str
) -> None:
    """It prints a certificate, which is public, and writes a key, which is not. A
    worked example that printed a private key would be copied."""
    body = functions["keypair"]

    assert "0o600" in body
    assert "PrivateFormat" in body
    for printed in ("print(certificate", "certificate.public_bytes"):
        assert printed in body
    assert "print(key" not in source
    # Serialised in exactly one place — the write. A second `private_bytes` anywhere is
    # the key being turned into text for some other purpose, which is worth looking at.
    assert source.count("private_bytes") == 1


def test_an_existing_key_is_not_replaced_by_accident(functions: dict[str, str]) -> None:
    """Overwriting it silently would leave a client whose registered certificate no
    longer matches anything — authentication failing for a reason nobody can see."""
    body = functions["keypair"]

    assert "exists()" in body
    assert "--force" in body or "args.force" in body


def test_every_subcommand_is_reachable(source: str) -> None:
    """Three commands, each wired to a function. A parser entry with no `set_defaults`
    fails only when somebody runs it."""
    for command in ("keypair", "as-itself", "for-user"):
        assert f'"{command}"' in source, command
    assert source.count("set_defaults") >= 2
