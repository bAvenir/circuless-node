"""The harness tests itself.

If token minting is quietly wrong, every later test is wrong with it and says nothing
useful. So the claim contract in `tests/realm/CONTRACT.md` is asserted here, against tokens
from a real Keycloak, before N2 relies on any of it.
"""

from __future__ import annotations

from .harness.keycloak import FixtureRealm, claims_of


def test_a_user_token_carries_the_contract_claims(realm: FixtureRealm) -> None:
    claims = claims_of(realm.user_token("alpha.user"))

    assert claims["iss"] == realm.issuer
    assert claims["aud"] == "node:test-node", "exactly one audience, this node's (§5.4)"
    assert claims["groups"] == ["/orgs/alpha"], "full paths, not short names"
    assert claims["principal_type"] == "user"
    assert claims["azp"] == "test-ui", "the actor is the client the token was issued to"
    # A user's organisation comes from groups; org_id belongs to service principals.
    assert "org_id" not in claims
    assert "node_id" not in claims


def test_an_admin_is_admin_of_one_org_only(realm: FixtureRealm) -> None:
    """R17: admin of X is membership of /orgs/x/admins, never a realm-wide role."""
    claims = claims_of(realm.user_token("alpha.admin"))
    assert set(claims["groups"]) == {"/orgs/alpha", "/orgs/alpha/admins"}
    assert not any(g.startswith("/orgs/beta") for g in claims["groups"])


def test_a_multi_org_user_carries_both_orgs(realm: FixtureRealm) -> None:
    """R11: org_ids is a set, and the acting org has to be resolved per request."""
    claims = claims_of(realm.user_token("consultant"))
    assert set(claims["groups"]) == {"/orgs/alpha", "/orgs/beta"}


def test_a_user_with_no_org_carries_no_groups(realm: FixtureRealm) -> None:
    """A self-registered user nobody has admitted yet can do nothing (FR1)."""
    claims = claims_of(realm.user_token("stranger"))
    assert claims.get("groups", []) == []


def test_principal_type_can_be_absent(realm: FixtureRealm) -> None:
    """The realm cannot default it — Keycloak's User Profile has no attribute defaults.

    So the claim really can be missing, and N3 must fail closed rather than assume `user`:
    a node whose attribute was never set would otherwise pass the check meant to reject it.
    This test exists to keep that case reachable once N3 arrives.
    """
    claims = claims_of(realm.user_token("no.principal.type"))
    assert "principal_type" not in claims


def test_a_service_token_carries_org_id_and_no_groups(realm: FixtureRealm) -> None:
    claims = claims_of(realm.service_token())

    assert claims["principal_type"] == "service"
    assert claims["org_id"] == "alpha", "a service's org is an attribute, not a group"
    assert claims.get("groups", []) == []
    assert claims["aud"] == "node:test-node"


def test_a_node_token_looks_exactly_as_d14_describes(realm: FixtureRealm) -> None:
    """This is the token N2 has to refuse. It must be mintable, or that cannot be tested."""
    claims = claims_of(realm.node_token())

    assert claims["principal_type"] == "node"
    assert claims["node_id"] == "test-node"
    # Infrastructure, not an organisation — which is exactly why it cannot consume.
    assert "org_id" not in claims
    assert claims.get("groups", []) == []


def test_a_token_for_another_node_can_be_minted(realm: FixtureRealm) -> None:
    """N2 must reject on audience as well as on principal type, so the suite needs a token
    that is perfectly valid and simply meant for somewhere else."""
    claims = claims_of(realm.user_token("alpha.user", scope="openid node:other-node"))
    assert claims["aud"] == "node:other-node"


def test_a_node_audienced_token_carries_no_name_or_email(realm: FixtureRealm) -> None:
    """D31. Here it holds because profile and email are optional on the client and not
    requested — a client contract, not something the realm can enforce."""
    claims = claims_of(realm.user_token("alpha.user"))
    assert "name" not in claims
    assert "email" not in claims


def test_requesting_profile_alongside_a_node_audience_does_leak_a_name(
    realm: FixtureRealm,
) -> None:
    """The other half of D31, recorded rather than wished away.

    Nothing in the realm stops a client asking for `profile` and a node audience together.
    That is why D31 is a client contract checked by T01, and why the node must never rely
    on a node-audienced token being nameless.
    """
    claims = claims_of(realm.user_token("alpha.user", scope="openid profile node:test-node"))
    assert claims["aud"] == "node:test-node"
    assert "name" in claims


def test_every_fixture_user_can_sign_in_as_themselves(realm: FixtureRealm) -> None:
    """The authorization-code flow is the only way in once H2 disables the password grant,
    so it has to work for every fixture user, not just the first one tried.

    Identity is checked by `sub`, not `preferred_username`: that claim rides on the
    `profile` scope, which a node-audienced token deliberately does not request (D31). Four
    distinct subjects is also the assertion that matters — a flow that silently returned
    the same session for everyone would pass a weaker check.
    """
    usernames = ["alpha.user", "alpha.admin", "beta.user", "consultant"]
    subjects = {name: claims_of(realm.user_token(name))["sub"] for name in usernames}

    assert len(set(subjects.values())) == len(usernames), f"subjects collided: {subjects}"
    for name, subject in subjects.items():
        assert subject == realm.user_id(name)
