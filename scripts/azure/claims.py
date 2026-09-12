#!/usr/bin/env python3
"""Report what is inside an access token, without printing the token.

Reads a JWT on standard input and writes the few claims that decide whether a
deployment will accept it: the audience, the issuer, the tenant, the object id
and how long it has left.

**Nothing here verifies a signature, and that is deliberate.** Verification is
the API's job and it does it properly (``EntraIdentityProvider``). This is the
operator's side of the same question — *why did that call come back 401* — and
the only honest way to answer it is to read what the token actually says rather
than what the app registration was supposed to make it say.

**It never prints the token, and no script in this repository does.** The output
of these scripts is pasted into terminals, issues and chats as a matter of
course; a bearer token that reaches any of those is a credential to be rotated,
and one printed by mistake is indistinguishable from one printed on purpose. The
claims are the useful part anyway.

Usage::

    az account get-access-token --scope 'api://<app id>/.default' --query accessToken -o tsv |
        python3 scripts/azure/claims.py --expect-audience 'api://<app id>' --expect-tenant '<tid>'
"""

import argparse
import base64
import binascii
import json
import sys
import time
from typing import Any

#: Claims worth printing, in the order that answers questions fastest.
REPORTED = ("aud", "iss", "tid", "oid", "appid", "scp", "roles", "exp")


class MalformedTokenError(ValueError):
    """The input is not a JWT this can read."""


def payload(token: str) -> dict[str, Any]:
    """Decode a JWT's payload.

    Args:
        token: The compact-serialised token.

    Returns:
        The payload's claims.

    Raises:
        MalformedTokenError: The input is not three base64url segments carrying
            a JSON object.
    """
    parts = token.strip().split(".")
    if len(parts) != 3:  # noqa: PLR2004 - a JWT has exactly three segments
        msg = f"not a JWT: expected three dot-separated segments, found {len(parts)}"
        raise MalformedTokenError(msg)

    # Base64url without padding is what the spec says and what every issuer
    # sends; Python's decoder insists on the padding anyway.
    segment = parts[1]
    segment += "=" * (-len(segment) % 4)
    try:
        decoded = base64.urlsafe_b64decode(segment)
    except (binascii.Error, ValueError) as error:
        msg = f"the payload is not base64url: {error}"
        raise MalformedTokenError(msg) from error

    try:
        claims = json.loads(decoded)
    except json.JSONDecodeError as error:
        msg = f"the payload is not JSON: {error}"
        raise MalformedTokenError(msg) from error

    if not isinstance(claims, dict):
        msg = "the payload is not a JSON object"
        raise MalformedTokenError(msg)
    return claims


def audience_matches(claim: object, expected: str) -> bool:
    """Whether an ``aud`` claim names the same API as the expected value.

    Both spellings count. Entra puts the application ID URI in a v1.0 token and
    the bare application id in a v2.0 one, decided by the app registration's
    ``requestedAccessTokenVersion`` rather than by anything the caller asked for
    — so a check that insisted on one spelling would report a working token as
    wrong. The API accepts both for the same reason (``accepted_audiences``).

    Args:
        claim: The token's ``aud`` claim.
        expected: The audience the deployment was configured with.

    Returns:
        True when the two name one API.
    """
    if not isinstance(claim, str) or not expected:
        return False
    return {claim, claim.removeprefix("api://")} & {expected, expected.removeprefix("api://")} != set()


def issuer_version(claim: object) -> str:
    """Name the token version an issuer implies.

    Worth reporting because this deployment pins the v2.0 issuer, and a v1.0
    token fails verification on the issuer — with a message about the issuer,
    which reads as a tenant problem rather than as the app registration property
    that actually caused it.
    """
    if not isinstance(claim, str):
        return "unknown"
    if claim.endswith("/v2.0"):
        return "v2.0"
    if claim.startswith("https://sts.windows.net/"):
        return "v1.0"
    return "unrecognised"


def report(claims: dict[str, Any]) -> str:
    """Render the claims worth looking at."""
    lines = []
    for name in REPORTED:
        if name not in claims:
            continue
        value = claims[name]
        if name == "exp" and isinstance(value, int):
            value = f"{value}  ({(value - int(time.time())) // 60} minutes left)"
        elif isinstance(value, list):
            value = " ".join(str(item) for item in value)
        lines.append(f"  {name:<6} {value}")
    lines.append(f"  {'format':<6} {issuer_version(claims.get('iss'))}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Read a token from standard input and report on it."""
    parser = argparse.ArgumentParser(
        prog="claims",
        description="Print the claims of an access token read from standard input.",
    )
    parser.add_argument("--expect-audience", default="", help="Audience the deployment requires.")
    parser.add_argument("--expect-tenant", default="", help="Tenant the deployment requires.")
    arguments = parser.parse_args(argv)

    try:
        claims = payload(sys.stdin.read())
    except MalformedTokenError as error:
        sys.stderr.write(f"{error}\n")
        return 2

    sys.stdout.write(report(claims) + "\n")

    problems = []
    if arguments.expect_audience and not audience_matches(
        claims.get("aud"), arguments.expect_audience
    ):
        problems.append(
            f"the token's audience is {claims.get('aud')!r} and this deployment requires "
            f"{arguments.expect_audience!r}. The audience is baked into the container, so the "
            f"fix is to export AZURE_PAIMON_API_AUDIENCE and deploy again."
        )
    if arguments.expect_tenant and claims.get("tid") != arguments.expect_tenant:
        problems.append(
            f"the token was issued by tenant {claims.get('tid')!r} and this deployment trusts "
            f"{arguments.expect_tenant!r}. Check which directory `az login` signed into."
        )
    if issuer_version(claims.get("iss")) == "v1.0":
        problems.append(
            "this is a v1.0 token and the API pins the v2.0 issuer. Set the app registration's "
            "requestedAccessTokenVersion to 2 — scripts/azure/token.sh does it."
        )

    for problem in problems:
        sys.stderr.write(f"  ! {problem}\n")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
