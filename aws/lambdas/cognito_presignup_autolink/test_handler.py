"""Unit tests for the PreSignUp_ExternalProvider auto-link handler.

Run from this directory:

    cd aws/lambdas/cognito_presignup_autolink
    python -m pytest -q

The boto3 cognito client is stubbed so no AWS calls are made. The tests cover
the full decision matrix:

  * verified + trusted (Google) + single native match -> links + autoconfirm
  * verified + trusted (MicrosoftEntra) + single match -> links
  * unverified email                                   -> no link
  * GitHub provider (untrusted)                        -> no link
  * no matching native user                            -> no link
  * multiple matching native users                     -> no link
  * match exists but is itself federated               -> no link
  * match exists but is UNCONFIRMED                    -> no link
  * non-ExternalProvider trigger source                -> no link / untouched
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


class FakeCognito:
    """Minimal stub of the boto3 cognito-idp client used by the handler."""

    def __init__(self, users):
        self._users = users
        self.link_calls = []

    def list_users(self, UserPoolId, Filter, Limit):  # noqa: N803 (boto3 casing)
        return {"Users": self._users}

    def admin_link_provider_for_user(self, **kwargs):  # noqa: N803
        self.link_calls.append(kwargs)
        return {}


@pytest.fixture
def mod(monkeypatch):
    monkeypatch.setenv("USER_POOL_ID", "us-east-1_rgTB9dbZ1")
    import handler as h

    importlib.reload(h)
    return h


def _install_client(mod, fake):
    mod._cognito_client = fake


def _native_user(username="native-uuid", email="user@example.com", verified="true",
                 status="CONFIRMED"):
    attrs = [
        {"Name": "email", "Value": email},
        {"Name": "email_verified", "Value": verified},
    ]
    return {"Username": username, "UserStatus": status, "Attributes": attrs}


def _federated_user(username="Google_999", email="user@example.com"):
    return {
        "Username": username,
        "UserStatus": "CONFIRMED",
        "Attributes": [
            {"Name": "email", "Value": email},
            {"Name": "email_verified", "Value": "true"},
            {"Name": "identities", "Value": "[{\"providerName\":\"Google\"}]"},
        ],
    }


def _event(user_name="Google_1234567890", email="user@example.com",
           email_verified="true", trigger="PreSignUp_ExternalProvider"):
    return {
        "userName": user_name,
        "triggerSource": trigger,
        "request": {
            "userAttributes": {
                "email": email,
                "email_verified": email_verified,
            }
        },
        "response": {},
    }


def test_verified_trusted_google_single_match_links(mod):
    fake = FakeCognito([_native_user()])
    _install_client(mod, fake)

    out = mod.handler(_event())

    assert len(fake.link_calls) == 1
    call = fake.link_calls[0]
    assert call["DestinationUser"]["ProviderAttributeValue"] == "native-uuid"
    assert call["SourceUser"]["ProviderName"] == "Google"
    assert call["SourceUser"]["ProviderAttributeValue"] == "1234567890"
    assert out["response"]["autoConfirmUser"] is True
    assert out["response"]["autoVerifyEmail"] is True


def test_verified_trusted_entra_single_match_links(mod):
    fake = FakeCognito([_native_user()])
    _install_client(mod, fake)

    out = mod.handler(_event(user_name="MicrosoftEntra_abc-oid"))

    assert len(fake.link_calls) == 1
    assert fake.link_calls[0]["SourceUser"]["ProviderName"] == "MicrosoftEntra"
    assert fake.link_calls[0]["SourceUser"]["ProviderAttributeValue"] == "abc-oid"
    assert out["response"]["autoConfirmUser"] is True


def test_unverified_email_no_link(mod):
    fake = FakeCognito([_native_user()])
    _install_client(mod, fake)

    out = mod.handler(_event(email_verified="false"))

    assert fake.link_calls == []
    assert "autoConfirmUser" not in out["response"]


def test_github_provider_no_link(mod):
    fake = FakeCognito([_native_user()])
    _install_client(mod, fake)

    out = mod.handler(_event(user_name="GitHub_42"))

    assert fake.link_calls == []
    assert "autoConfirmUser" not in out["response"]


def test_no_matching_user_no_link(mod):
    fake = FakeCognito([])  # ListUsers returns nothing
    _install_client(mod, fake)

    out = mod.handler(_event())

    assert fake.link_calls == []
    assert "autoConfirmUser" not in out["response"]


def test_multiple_matches_no_link(mod):
    fake = FakeCognito([
        _native_user(username="native-1"),
        _native_user(username="native-2"),
    ])
    _install_client(mod, fake)

    out = mod.handler(_event())

    assert fake.link_calls == []
    assert "autoConfirmUser" not in out["response"]


def test_match_is_federated_no_link(mod):
    # Only candidate carries an "identities" attr -> not native -> no unique match.
    fake = FakeCognito([_federated_user()])
    _install_client(mod, fake)

    out = mod.handler(_event())

    assert fake.link_calls == []
    assert "autoConfirmUser" not in out["response"]


def test_match_unconfirmed_no_link(mod):
    fake = FakeCognito([_native_user(status="UNCONFIRMED")])
    _install_client(mod, fake)

    out = mod.handler(_event())

    assert fake.link_calls == []
    assert "autoConfirmUser" not in out["response"]


def test_match_email_not_verified_no_link(mod):
    # Existing native user's OWN email is unverified -> not a safe target.
    fake = FakeCognito([_native_user(verified="false")])
    _install_client(mod, fake)

    out = mod.handler(_event())

    assert fake.link_calls == []
    assert "autoConfirmUser" not in out["response"]


def test_non_external_trigger_untouched(mod):
    fake = FakeCognito([_native_user()])
    _install_client(mod, fake)

    out = mod.handler(_event(trigger="PreSignUp_SignUp"))

    assert fake.link_calls == []
    assert out["response"] == {}


# ─── Invitation-only allowlist gate ──────────────────────────────────────────

def test_allowlist_unset_failopen_external_proceeds(mod, monkeypatch):
    # No SIGNUP_ALLOWLIST → enforcement disabled → behaves exactly as before
    # (here: verified Google + single match still auto-links).
    monkeypatch.delenv("SIGNUP_ALLOWLIST", raising=False)
    fake = FakeCognito([_native_user(email="user@example.com")])
    _install_client(mod, fake)

    out = mod.handler(_event(email="user@example.com"))

    assert len(fake.link_calls) == 1
    assert out["response"]["autoConfirmUser"] is True


def test_allowlist_blocks_unlisted_external(mod, monkeypatch):
    monkeypatch.setenv("SIGNUP_ALLOWLIST", "allowed@example.com")
    fake = FakeCognito([_native_user(email="stranger@example.com")])
    _install_client(mod, fake)

    with pytest.raises(Exception):
        mod.handler(_event(email="stranger@example.com"))
    assert fake.link_calls == []


def test_allowlist_allows_listed_external_and_links(mod, monkeypatch):
    monkeypatch.setenv("SIGNUP_ALLOWLIST", "allowed@example.com, other@example.com")
    fake = FakeCognito([_native_user(email="allowed@example.com")])
    _install_client(mod, fake)

    out = mod.handler(_event(email="allowed@example.com"))

    assert len(fake.link_calls) == 1
    assert out["response"]["autoConfirmUser"] is True


def test_allowlist_case_insensitive(mod, monkeypatch):
    monkeypatch.setenv("SIGNUP_ALLOWLIST", "Allowed@Example.com")
    fake = FakeCognito([_native_user(email="allowed@example.com")])
    _install_client(mod, fake)

    out = mod.handler(_event(email="ALLOWED@example.com"))

    assert len(fake.link_calls) == 1


def test_allowlist_blocks_unlisted_native_signup(mod, monkeypatch):
    # Belt-and-suspenders: a native self sign-up (PreSignUp_SignUp) by a
    # non-allowlisted email is also blocked (in addition to the pool's
    # AllowAdminCreateUserOnly=true).
    monkeypatch.setenv("SIGNUP_ALLOWLIST", "allowed@example.com")
    fake = FakeCognito([])
    _install_client(mod, fake)

    with pytest.raises(Exception):
        mod.handler(_event(email="stranger@example.com", trigger="PreSignUp_SignUp"))


def test_allowlist_admin_create_bypasses_gate(mod, monkeypatch):
    # Admin-created users (how Stefan is onboarded) bypass the allowlist even
    # if their email isn't on it.
    monkeypatch.setenv("SIGNUP_ALLOWLIST", "someoneelse@example.com")
    fake = FakeCognito([])
    _install_client(mod, fake)

    out = mod.handler(_event(email="stefan@example.com",
                             trigger="PreSignUp_AdminCreateUser"))

    assert fake.link_calls == []
    assert out["response"] == {}
