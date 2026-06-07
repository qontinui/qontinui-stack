"""Cognito PreSignUp_ExternalProvider trigger — auto-link a federated sign-in to
an existing native account, but ONLY when it is provably safe.

Threat model
------------
When a user signs in through a federated IdP (Google, Microsoft Entra, GitHub),
Cognito creates a *new, distinct* user in the pool unless the external identity
is explicitly linked to an existing user. Naive auto-linking by email is an
account-takeover vector: an attacker who controls an IdP that lets them assert
an arbitrary (or unverified) email could federate in and get merged into a
victim's native account.

We therefore auto-link ONLY when ALL of the following hold:

  1. The incoming external identity's email is verified
     (userAttributes["email_verified"] == "true"), AND
  2. The source provider is TRUSTED — Google or Microsoft Entra. GitHub is
     explicitly NOT trusted for auto-link (GitHub does not reliably assert a
     *verified*-email semantics we accept here), AND
  3. There is EXACTLY ONE existing pool user that is a CONFIRMED, native
     (non-federated) account with the same email, whose OWN email is verified.

If any condition fails — unverified email, untrusted provider (GitHub), no
matching native user, or more than one match — we DO NOTHING and return the
event unchanged. Cognito then proceeds with its default behaviour (creating a
separate federated user), which preserves the account-takeover guard. Users can
still link such identities later through an explicit, authenticated linking flow
in the web app (which uses the web task role's AdminLinkProviderForUser grant).

This handler is wired to the pool's PreSignUp Lambda trigger out-of-band (the
pool is manually managed and not in Terraform). See the module README / the
update-user-pool step documented in the Terraform.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Providers we trust enough to auto-link on a verified email. GitHub is
# deliberately excluded. Microsoft Entra is included, but note: Entra may not
# emit email_verified at all — in which case condition (1) fails and auto-link
# simply never fires for Entra. That is the safe default; we never link on a
# missing/unverified email.
TRUSTED_PROVIDERS = frozenset({"Google", "MicrosoftEntra"})

_cognito_client = None


def _client():
    global _cognito_client
    if _cognito_client is None:
        _cognito_client = boto3.client("cognito-idp")
    return _cognito_client


def _provider_name_from_event(event: dict[str, Any]) -> str | None:
    """Derive the source IdP name from the Cognito federated userName.

    Cognito federated usernames look like ``Google_1234567890``,
    ``MicrosoftEntra_<oid>``, ``GitHub_<id>`` — i.e. ``<ProviderName>_<subject>``.
    We split on the FIRST underscore so provider names are matched exactly.
    Returns None if the userName has no provider prefix (e.g. a native flow,
    which should not reach the ExternalProvider trigger anyway).
    """
    user_name = event.get("userName") or ""
    if "_" not in user_name:
        return None
    return user_name.split("_", 1)[0]


def _is_external_provider_trigger(event: dict[str, Any]) -> bool:
    return event.get("triggerSource") == "PreSignUp_ExternalProvider"


def _allowlist() -> set[str] | None:
    """Parse SIGNUP_ALLOWLIST — comma-separated, case-insensitive emails
    permitted to self-service / federate sign-up.

    Returns None when the env var is absent or empty, which DISABLES
    enforcement (fail-open). This is deliberate: a missing/cleared config must
    never silently lock every new login out of the platform. Enforcement is
    active only when the list is explicitly populated.
    """
    raw = os.environ.get("SIGNUP_ALLOWLIST", "").strip()
    if not raw:
        return None
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def _enforce_signup_allowlist(email: str | None) -> None:
    """Invitation-only gate: raise (→ Cognito denies the sign-up) when the
    incoming email is not on the allowlist. No-op when the allowlist is unset
    (fail-open). Callers must NOT invoke this for PreSignUp_AdminCreateUser —
    admin-provisioned accounts are the trusted path and always bypass the gate.
    """
    allow = _allowlist()
    if allow is None:
        logger.warning(
            "presignup: SIGNUP_ALLOWLIST unset — invitation-only gate DISABLED "
            "(fail-open)"
        )
        return
    if (email or "").strip().lower() not in allow:
        logger.info("presignup: BLOCK sign-up — email not on allowlist")
        # Raising fails the PreSignUp trigger; Cognito rejects the sign-up.
        raise Exception(
            "Sign-up is by invitation only. Contact the administrator for access."
        )
    logger.info("presignup: allow sign-up — email on allowlist")


def _find_unique_native_match(user_pool_id: str, email: str) -> dict[str, Any] | None:
    """Return the single existing CONFIRMED, native, email-verified user with
    this email, or None if there is no such user OR more than one candidate
    (any ambiguity → no link).
    """
    resp = _client().list_users(
        UserPoolId=user_pool_id,
        Filter=f'email = "{email}"',
        Limit=10,
    )
    users = resp.get("Users", [])

    candidates = []
    for user in users:
        attrs = {a["Name"]: a["Value"] for a in user.get("Attributes", [])}

        # Must be a CONFIRMED account.
        if user.get("UserStatus") != "CONFIRMED":
            continue

        # Must be NATIVE, not itself a federated identity. Federated users in
        # Cognito carry an "identities" attribute; native users do not. Linking
        # one external identity onto another federated user is never what we
        # want here.
        if attrs.get("identities"):
            continue

        # The existing native user's OWN email must be verified.
        if attrs.get("email_verified") != "true":
            continue

        candidates.append(user)

    if len(candidates) != 1:
        # Zero matches → nothing to link to. More than one → ambiguous; refuse.
        return None
    return candidates[0]


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """PreSignUp_ExternalProvider entrypoint. Mutates event["response"] in place
    and returns the event (Cognito contract)."""
    response = event.setdefault("response", {})
    trigger = event.get("triggerSource")
    attrs = event.get("request", {}).get("userAttributes", {}) or {}

    # Invitation-only gate. Block any self-service or federated sign-up whose
    # email is not on the allowlist. Admin-created users
    # (PreSignUp_AdminCreateUser) are the trusted provisioning path and are
    # never gated. No-op when SIGNUP_ALLOWLIST is unset (fail-open).
    if trigger != "PreSignUp_AdminCreateUser":
        _enforce_signup_allowlist(attrs.get("email"))

    if not _is_external_provider_trigger(event):
        # Not a federated sign-up — leave untouched (gate already applied).
        return event

    user_pool_id = os.environ["USER_POOL_ID"]

    email = attrs.get("email")
    email_verified = attrs.get("email_verified")
    provider = _provider_name_from_event(event)

    # Condition (1): incoming email must be verified.
    if email_verified != "true" or not email:
        logger.info(
            "presignup-autolink: skip (email_verified=%r, has_email=%s)",
            email_verified,
            bool(email),
        )
        return event

    # Condition (2): provider must be trusted (Google / MicrosoftEntra; NOT GitHub).
    if provider not in TRUSTED_PROVIDERS:
        logger.info("presignup-autolink: skip untrusted provider=%r", provider)
        return event

    # Condition (3): exactly one confirmed, native, email-verified match.
    match = _find_unique_native_match(user_pool_id, email)
    if match is None:
        logger.info(
            "presignup-autolink: skip (no unique native match for email)"
        )
        return event

    # All conditions hold → link the new external identity onto the existing
    # native user, and auto-confirm so Cognito does not create a duplicate.
    destination_username = match["Username"]
    external_user_name = event["userName"]  # e.g. "Google_1234567890"

    _client().admin_link_provider_for_user(
        UserPoolId=user_pool_id,
        DestinationUser={
            "ProviderName": "Cognito",
            "ProviderAttributeValue": destination_username,
        },
        SourceUser={
            "ProviderName": provider,
            # For OIDC/social providers the linking attribute is the Cognito
            # subject ("Cognito_Subject"); its value is the federated subject —
            # the part of userName after "<Provider>_".
            "ProviderAttributeName": "Cognito_Subject",
            "ProviderAttributeValue": external_user_name.split("_", 1)[1],
        },
    )

    response["autoConfirmUser"] = True
    # The incoming email is verified (condition 1), so verify it on the linked
    # account too — avoids a redundant verification prompt.
    response["autoVerifyEmail"] = True

    logger.info(
        "presignup-autolink: linked provider=%s onto native user", provider
    )
    return event
