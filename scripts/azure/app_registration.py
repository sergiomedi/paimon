#!/usr/bin/env python3
"""Work out what an app registration still needs before it can issue tokens.

Reads the application object Microsoft Graph returns on standard input and
writes, on standard output, **the next** PATCH body it needs. Prints nothing at
all when there is nothing left to change, which is how ``token.sh`` stays
idempotent without comparing objects in bash.

One step at a time, and that is not tidiness. Graph validates
``preAuthorizedApplications`` against the scopes it has already **stored**, not
against the ones in the same request, so a body that adds a scope and
pre-authorises a client for it is rejected outright:

    Property api.preAuthorizedApplications.delegatedPermissionIds has a
    Permission Id that cannot be found in the AppPermissions sets.

It is a known and long-standing limitation, not a malformed request. So the
caller applies what this prints, re-reads the application, and asks again —
which also means a step that half-applied is simply planned once more.

The scope's id is derived from the application id rather than generated, so
asking twice produces the same id. A random one regenerated after a read that had
not caught up yet would replace the scope with a different one and detach every
consent that referred to it.

Three things have to be true, and none of them is the default for an application
created by ``az ad app create``:

* **A delegated scope exists.** A resource with no scopes cannot be asked for a
  delegated token at all; the request fails before any consent question.
* **The Azure CLI is pre-authorised for it.** Consent is per client application,
  and nobody owns the Azure CLI's registration, so the ordinary consent flow has
  nowhere to happen. Pre-authorisation is the mechanism Microsoft provides for
  exactly this: the resource names a client it trusts, and that client's users
  are never asked. It is also why this needs no client secret anywhere — the
  same argument as ADR-0037, extended from the services to the person calling
  them.
* **The token version is 2.** The API pins the v2.0 issuer
  (``…/v2.0``), and a v1.0 token is issued by ``sts.windows.net`` and fails
  verification on the issuer rather than on anything that names the cause.

This is a file rather than a string inside token.sh so that a gate can run it:
``scripts/check.sh`` feeds it three fixtures and checks what comes back.

Usage::

    az ad app show --id "$APP_ID" -o json | python3 scripts/azure/app_registration.py
"""

import json
import sys
import uuid
from typing import Any

#: The Azure CLI's own application id. A well-known constant, the same in every
#: tenant, and published by Microsoft: pre-authorising it is what lets
#: ``az account get-access-token`` ask for this API without a consent prompt that
#: has no owner to answer it.
AZURE_CLI_APPLICATION_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"

#: Name of the delegated scope. ``user_impersonation`` is the convention
#: Microsoft's own tooling creates and expects, and the API does not read it: it
#: authorises on the tenant and the object id, not on a scope name.
SCOPE = "user_impersonation"

#: v2.0. See the module docstring — this is the property that decides both the
#: issuer and the spelling of the audience.
TOKEN_VERSION = 2


def scope_id(application: dict[str, Any], existing: dict[str, Any] | None) -> str:
    """The id this scope has, or will have.

    An existing one wins: it is what pre-authorisation and any granted consent
    refer to, and replacing it would silently detach both. Otherwise it is
    derived from the application id, so that two runs agree on it — a random id
    regenerated between the write and a read that had not caught up would create
    a second scope and orphan the first.
    """
    if existing and existing.get("id"):
        return str(existing["id"])
    seed = f"api://{application.get('appId', '')}/{SCOPE}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def scope_definition(identifier: str) -> dict[str, Any]:
    """The delegated scope this API exposes."""
    return {
        "id": identifier,
        "value": SCOPE,
        "type": "User",
        "isEnabled": True,
        "adminConsentDisplayName": "Access the Paimon API",
        "adminConsentDescription": "Allows the application to call the Paimon API as the signed-in user.",
        "userConsentDisplayName": "Access the Paimon API",
        "userConsentDescription": "Allows the application to call the Paimon API on your behalf.",
    }


def body(application: dict[str, Any]) -> dict[str, Any] | None:
    """The next PATCH body this application needs, or None when it needs none.

    Args:
        application: The application object as Microsoft Graph returns it.

    Returns:
        A body for ``PATCH /applications/{id}``, or None.
    """
    api = application.get("api") or {}
    scopes = list(api.get("oauth2PermissionScopes") or [])
    preauthorised = list(api.get("preAuthorizedApplications") or [])

    current = next((scope for scope in scopes if scope.get("value") == SCOPE), None)
    wanted = scope_definition(scope_id(application, current))

    # Compared field by field rather than whole: Graph returns keys this does not
    # set (``origin``, for one), and an equality check against the object it
    # returns would rewrite the registration on every run.
    scope_is_current = current is not None and all(
        current.get(key) == value for key, value in wanted.items()
    )
    version_is_current = api.get("requestedAccessTokenVersion") == TOKEN_VERSION

    # Step one: the scope has to exist *and be stored* before anything can be
    # pre-authorised for it. The token version rides along because it is a
    # property rather than a collection and costs nothing here.
    if not (scope_is_current and version_is_current):
        remaining = [scope for scope in scopes if scope.get("value") != SCOPE]
        return {
            "api": {
                "requestedAccessTokenVersion": TOKEN_VERSION,
                # The whole collection, because Graph replaces rather than merges
                # it. Anything already there is carried across rather than
                # dropped: this registration may well be used by something other
                # than these scripts, and a deployment tool that silently removes
                # another client's authorisation is a worse failure than one that
                # does nothing.
                "oauth2PermissionScopes": [*remaining, wanted],
            }
        }

    # Step two, on the next pass, once Graph has the scope.
    cli = next(
        (entry for entry in preauthorised if entry.get("appId") == AZURE_CLI_APPLICATION_ID), None
    )
    if cli is not None and wanted["id"] in (cli.get("delegatedPermissionIds") or []):
        return None

    others = [entry for entry in preauthorised if entry.get("appId") != AZURE_CLI_APPLICATION_ID]
    return {
        "api": {
            "preAuthorizedApplications": [
                *others,
                {
                    "appId": AZURE_CLI_APPLICATION_ID,
                    "delegatedPermissionIds": [wanted["id"]],
                },
            ],
        }
    }


def main() -> int:
    """Read an application object and write the next body it needs, if any."""
    try:
        application = json.load(sys.stdin)
    except json.JSONDecodeError as error:
        sys.stderr.write(f"the application object is not JSON: {error}\n")
        return 1
    if not isinstance(application, dict):
        sys.stderr.write("the application object is not an object\n")
        return 1

    needed = body(application)
    if needed is None:
        return 0
    sys.stdout.write(json.dumps(needed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
