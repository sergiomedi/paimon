#!/usr/bin/env bash
# Run the same gates CI runs, in the same order, and stop at the first failure.
#
#   ./scripts/check.sh                  backend, frontend and infrastructure
#   ./scripts/check.sh backend          backend only
#   ./scripts/check.sh infrastructure   the Azure templates only
#
# This exists because a change once reached CI having passed its tests but not
# its linter: the checks were run individually and one was forgotten. One
# command removes that possibility. Keep it in step with .github/workflows/ci.yml
# — CI remains the authority, this is the local mirror of it.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# bicep_cli and have_bicep, defined once for this script and for the Azure ones.
source "$ROOT/scripts/_bicep.sh"
TARGET="${1:-all}"

step() { printf '\n\033[1m▸ %s\033[0m\n' "$1"; }

if [[ "$TARGET" == "all" || "$TARGET" == "backend" ]]; then
    cd "$ROOT/backend"
    # --all-groups, because a bare `uv run` installs only the default groups and
    # would leave pytest out — which mypy then reports as thirty missing-import
    # errors rather than as the missing dependency it is. CI syncs the same way.
    # --all-extras too: the Azure credential path is an optional extra, and
    # mypy cannot verify code whose imports are not installed.
    step "uv sync";        uv sync --all-groups --all-extras
    # --no-sync from here on: the environment is already correct, and re-checking
    # it before every gate only costs time.
    step "ruff check";     uv run --no-sync ruff check .
    step "ruff format";    uv run --no-sync ruff format --check .
    step "mypy --strict";  uv run --no-sync mypy
    step "import-linter";  uv run --no-sync lint-imports
    step "pytest";         uv run --no-sync pytest
fi

if [[ "$TARGET" == "all" || "$TARGET" == "frontend" ]]; then
    if [[ -d "$ROOT/frontend/node_modules" ]]; then
        cd "$ROOT/frontend"
        step "eslint";     pnpm lint
        step "tsc";        pnpm typecheck
        step "next build"; pnpm build
    else
        printf '\n\033[33mSkipping frontend: run pnpm install in frontend/ first.\033[0m\n'
    fi
fi

if [[ "$TARGET" == "all" || "$TARGET" == "infrastructure" ]]; then
    cd "$ROOT"

    # The deployment scripts, and the programs inside them.
    #
    # `bash -n` alone is what let a broken deploy.sh ship: it checks the shell
    # and says nothing about a Python program passed to `python3 -c` as a string,
    # because to the shell that string is an argument. So the Python lives in a
    # file now, and this compiles it and then *runs* it against a fixture —
    # compiling would have caught that bug, and running is what catches the next
    # one.
    step "scripts"
    find scripts -name '*.sh' -print0 | xargs -0 -n1 bash -n
    python3 -m py_compile scripts/azure/outputs.py
    printf '{"aB": {"value": "x"}}' | python3 scripts/azure/outputs.py | grep -qx 'AZURE_A_B=x'

    # The collector's configuration, parsed rather than trusted. Bicep loads it
    # with loadTextContent(), which checks that the file exists and nothing else.
    # Compiled first and then *run*, for the reason in the comment above this one.
    # The quota checker, compiled and then run against a fixture built from real
    # Azure output — including the row whose name disagrees with the model's
    # (`gpt4.1-mini` against `gpt-4.1-mini`) and the dot inside it, which a naive
    # split drops. Both of those cost a day each; neither can cost another.
    # The Bicep helper, exercised against both installations rather than only the
    # one on this machine. A path is positional for the standalone compiler and
    # `--file` for the Azure CLI's, and a caller written for the wrong one fails
    # with a non-zero exit and nothing on stdout — which read as "the template
    # will not compile" on a machine where this very script had just compiled it.
    # The workflow file, parsed. It is YAML containing shell containing Python,
    # and an inner block indented one column too far silently ends the outer one:
    # a `run: |` step whose script becomes a sibling key. Nothing local reads this
    # file, so a broken one is found by pushing it — which is how a broken one was
    # pushed.
    step "workflows"
    python3 - <<'PYTHON'
import pathlib
import re
import sys

try:
    import yaml
except ImportError:  # pragma: no cover - CI installs it; a laptop may not have it
    print("  PyYAML is not installed, skipping")
    sys.exit(0)

files = sorted(pathlib.Path(".github/workflows").glob("*.yml"))
assert files, "no workflows found; has the directory moved?"

jobs = 0
for path in files:
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    for name, job in workflow["jobs"].items():
        jobs += 1
        for step in job["steps"]:
            assert "uses" in step or "run" in step, f"{path.name}:{name}: a step does nothing"

delivery = yaml.safe_load(pathlib.Path(".github/workflows/delivery.yml").read_text("utf-8"))

# The two properties of this workflow that cost money to get wrong, checked here
# because neither is visible in a diff and both are one careless edit away.
#
# A teardown that does not run on failure leaves an entire environment billing —
# and the runs that fail are the ones nobody is watching. `always()` is the only
# condition that covers failure *and* cancellation.
steps = delivery["jobs"]["verify"]["steps"]
teardown = [step for step in steps if "destroy.sh" in str(step.get("run", ""))]
assert len(teardown) == 1, "the verification job must tear down exactly once"
# `always()` has to appear, and may be qualified — the real condition is
# `always() && steps.login.outcome == 'success'`, because a teardown with no
# session to use adds a second error on top of the real one. What must never
# happen is a condition without always() in it at all: success() alone leaves a
# failed run's database billing.
assert "always()" in str(teardown[0].get("if", "")), (
    "the teardown must run on failure too — success() leaves a failed run's database billing"
)
assert steps.index(teardown[0]) == len(steps) - 1, "and it must be the last step"

# Cancelling between the deployment and the teardown is the same failure with a
# person's finger on it.
assert delivery["concurrency"]["cancel-in-progress"] is False, (
    "cancelling mid-run kills the job before the teardown"
)

# Without this GitHub mints no token and azure/login fails on an empty assertion.
assert delivery["permissions"]["id-token"] == "write", "OIDC needs id-token: write"

# Promotion's invariants, which are the opposite ones: it must be gated, it must
# migrate before it releases, and it must never destroy anything. A promotion
# workflow that tore down what it had just released would be a very expensive
# copy-paste from the file above.
promote = yaml.safe_load(pathlib.Path(".github/workflows/promote.yml").read_text("utf-8"))
job = promote["jobs"]["promote"]

assert job.get("environment"), (
    "promotion must name a GitHub Environment: it is the approval gate, and it is also "
    "the subject the federated credential was created for"
)
# `promote[True]`, and that is not a typo either. YAML 1.1 — which PyYAML
# implements — reads the bare word `on` as the boolean true, so a workflow's
# trigger block is under the key True rather than "on". GitHub's parser does not
# do this, so the two disagree about a file they both read, and a check written
# the obvious way fails on a file that is perfectly correct.
triggers = promote.get("on", promote.get(True, {}))
assert "workflow_dispatch" in triggers, "promotion is a decision, not a consequence of a merge"
assert "push" not in triggers, "a merge must not spend money"

names = [str(step.get("run", "")) for step in job["steps"]]


def invokes(script, run):
    """Whether a step *runs* a script, rather than mentioning it.

    The distinction is not pedantry: the last step of promotion prints the
    teardown command into the job summary, so that whoever approved the release
    knows how to stop paying for it. A check that matched the name anywhere would
    read that helpful line as the disaster it is warning about.
    """
    return any(line.strip().startswith(f"./scripts/azure/{script}") for line in run.splitlines())


assert not any(invokes("destroy.sh", step) for step in names), "promotion must not tear down"
migrate = next(i for i, step in enumerate(names) if invokes("migrate.sh", step))
release = next(i for i, step in enumerate(names) if invokes("release.sh", step))
assert migrate < release, (
    "the schema moves before the traffic does. Reversing these two puts a revision in "
    "front of users against a schema it does not have yet."
)

# Every action pinned to a full version, and to the *same* version everywhere.
#
# This cannot check that a version exists — that needs the network, and GitHub
# answers it in five seconds for free. It checks the two things an offline gate
# can: that nothing floats on a moving major tag, and that two workflows never
# disagree about the same action, which is how one of them keeps working while
# the other is upgraded and breaks.
#
# The five-second failure is worth knowing by sight: an unresolvable `uses:`
# fails a run before any step executes, so a workflow that dies that fast is
# almost never the deployment. azure/login@v2.4.0 — a version invented rather
# than looked up — cost three runs that way.
pinned = {}
for path in files:
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            reference = step.get("uses")
            if not reference or "@" not in reference:
                continue
            action, version = reference.rsplit("@", 1)
            assert re.fullmatch(r"v\d+\.\d+\.\d+", version), (
                f"{path.name}: {reference} is not pinned to an exact version"
            )
            assert pinned.setdefault(action, version) == version, (
                f"{action} is pinned to two different versions across the workflows"
            )

print(f"  {len(files)} workflows, {jobs} jobs, {len(pinned)} actions pinned exactly")
print("  the teardown cannot be skipped; promotion is gated, migrates first, destroys nothing")
PYTHON

    step "bicep invocation"
    (
        stub="$(mktemp -d)"
        trap 'rm -rf "$stub"' EXIT
        # An `az` that accepts --file and refuses a positional path, as the real
        # one does.
        cat > "$stub/az" <<'STUB'
#!/usr/bin/env bash
[[ "$1" == "bicep" ]] || exit 0
shift
[[ "$1" == "version" ]] && exit 0
shift
while [[ $# -gt 0 ]]; do
    case "$1" in
        --file) [[ -n "$2" ]] || exit 2; shift 2;;
        --*) shift;;
        *) exit 2;;
    esac
done
exit 0
STUB
        # A standalone `bicep` that wants the path positionally and nothing else.
        cat > "$stub/bicep" <<'STUB'
#!/usr/bin/env bash
shift
[[ "$1" == --* || -z "$1" ]] && exit 2
exit 0
STUB
        chmod +x "$stub/az" "$stub/bicep"
        source "$ROOT/scripts/_bicep.sh"

        PATH="$stub:/usr/bin:/bin" bicep_cli build x.bicep --stdout ||
            { printf '  the standalone compiler is invoked wrongly\n'; exit 1; }
        rm "$stub/bicep"
        PATH="$stub:/usr/bin:/bin" bicep_cli build x.bicep --stdout ||
            { printf '  the Azure CLI compiler is invoked wrongly\n'; exit 1; }
        printf '  both installations are invoked correctly\n'
    )

    step "model quota"
    python3 -m py_compile scripts/azure/model_quota.py
    python3 - <<'PYTHON'
import json
import pathlib
import sys

sys.path.insert(0, "scripts/azure")
from model_quota import Wanted, missing, normalise  # noqa: E402

assert normalise("gpt-4.1-mini") == normalise("gpt4.1-mini"), "the two spellings must agree"

usage = [
    {"name": {"value": "OpenAI.GlobalStandard.gpt4.1-mini"}, "limit": 200},
    {"name": {"value": "OpenAI.Standard.text-embedding-3-large"}, "limit": 350},
    {"name": {"value": "OpenAI.GlobalStandard.text-embedding-3-large"}, "limit": 0},
]
assert not missing(usage, [Wanted("GlobalStandard", "gpt-4.1-mini")]), "dotted name must match"
assert missing(usage, [Wanted("GlobalStandard", "text-embedding-3-large")]), "limit 0 is not quota"
assert missing(usage, [Wanted("Standard", "gpt-4.1-mini")]), "the deployment type matters"
assert not missing(usage, [Wanted("Standard", "text-embedding-3-large")])
print("  quota matching holds across both spellings")
PYTHON

    # The application owns the PAIMON_ prefix and refuses any variable in it that
    # is not one of its settings — deliberately, so a typo in a deployment stops
    # the process. The deployment scripts therefore must not name their own
    # variables in it: exporting one made `alembic upgrade head` in the same
    # shell fail on a variable that had nothing to do with the database.
    #
    # Scoped to the scripts and the parameter file. modules/api.bicep sets real
    # application settings for the containers and belongs in that namespace.
    #
    # benchmark.sh is excluded, and the exclusion is the rule rather than an
    # exception to it: that script *runs the application*, so the application's
    # namespace is precisely where its variables belong. What must hold for them
    # is something this gate cannot check — that each name is a real setting —
    # and the backend's own test suite checks it, against the settings model,
    # which is the only thing that knows.
    step "deployment variable namespace"
    if squatting=$(grep -rhoE '\bPAIMON_[A-Z0-9_]+' \
        $(ls scripts/azure/*.sh | grep -v benchmark.sh) infrastructure/main.bicepparam | sort -u); then
        printf '%s\n' "$squatting" | sed 's/^/  /'
        printf '  deployment variables must be AZURE_PAIMON_*, not PAIMON_*\n'
        exit 1
    fi
    printf '  the deployment scripts stay out of the application namespace\n'

    step "collector config"
    python3 -m py_compile scripts/azure/collector_config.py
    python3 scripts/azure/collector_config.py

    step "access tokens"
    python3 -m py_compile scripts/azure/claims.py scripts/azure/app_registration.py \
        scripts/azure/measurement.py
    python3 - <<'PYTHON'
import sys

sys.path.insert(0, "scripts/azure")
from measurement import describe_ingestion  # noqa: E402

# The cheap outcome is the interesting one, and the report claimed the expensive
# one regardless: it said a document had been chunked, embedded and indexed on a
# run where the content hash matched and nothing whatsoever was done.
repeat = describe_ingestion({"document_id": "d", "chunks_indexed": 0, "unchanged": True})
assert "unchanged" in repeat and "idempotent" in repeat, "a repeat must say it did nothing"
assert "embedded through" not in repeat, "and must not claim work it did not do"
first = describe_ingestion({"document_id": "d", "chunks_indexed": 6, "unchanged": False})
assert "6 chunks indexed" in first and "embedded through" in first
PYTHON
    python3 - <<'PYTHON'
import json
import sys

sys.path.insert(0, "scripts/azure")
from app_registration import body  # noqa: E402
from claims import audience_matches, issuer_version  # noqa: E402

# One API, two spellings. The app registration decides which arrives and the
# caller cannot influence it, so a check that insisted on either would report a
# working token as wrong.
assert audience_matches("2858bfe0", "api://2858bfe0"), "a v2.0 token names the bare id"
assert audience_matches("api://2858bfe0", "2858bfe0"), "and a v1.0 token the URI"
assert not audience_matches("api://something-else", "api://2858bfe0")
assert issuer_version("https://sts.windows.net/t/") == "v1.0", "which this API refuses"

# One change per pass. Graph validates a pre-authorised client against the scopes
# it has already stored, so a body carrying both is refused with a permission id
# that cannot be found — the scope has to land in its own request first.
application = {"id": "obj", "appId": "an-app-id", "api": {}}
first = body(application)
assert first is not None, "a fresh registration needs the scope and v2.0"
assert "preAuthorizedApplications" not in first["api"], "not in the same request as the scope"

# Graph is then fed its own output, as the next pass reads it back: it returns
# keys the patch did not set, and an equality check against the whole object
# would rewrite the registration — and with it the scope id consent refers to —
# on every single run.
application["api"] = json.loads(json.dumps(first["api"]))
application["api"]["oauth2PermissionScopes"][0]["origin"] = "Application"
second = body(application)
assert second is not None and "preAuthorizedApplications" in second["api"], "then the client"

application["api"]["preAuthorizedApplications"] = second["api"]["preAuthorizedApplications"]

# And then the application role, which is a separate property and a separate
# pass: client credentials get no token at all for a resource the caller holds no
# app role on, whatever the delegated scopes say.
third = body(application)
assert third is not None and "appRoles" in third, "then the role for non-human callers"
assert third["appRoles"][0]["allowedMemberTypes"] == ["Application"], "not for people"
application["appRoles"] = third["appRoles"]
assert body(application) is None, "and then there is nothing left to do"

# A stale read must not produce a second scope, so the id is derived rather than
# generated: the same application always plans the same id.
assert body({"id": "obj", "appId": "an-app-id", "api": {}}) == first, "the scope id is stable"
print("  the token helpers agree on both spellings and settle in three passes")
PYTHON

    # A bearer token printed to a terminal is a credential to rotate: this
    # output is pasted into issues and chat windows as a matter of course, and
    # one shown by accident is indistinguishable from one shown on purpose. The
    # scripts capture tokens and never display them, and this is what keeps that
    # true.
    # Writing one *into* another program is how it is used at all, so the rule is
    # narrower than "never printf a token": a line that renders it has to pipe it
    # onwards on the same line, and may not tee it — the report these scripts
    # write is a file that gets committed.
    step "no script prints a token"
    if leaking=$(grep -nE '^[^#]*(printf|echo)[^|]*\$\{?TOKEN[^|]*$' scripts/azure/*.sh; \
                 grep -nE '(printf|echo).*\$\{?TOKEN.*\|.*tee' scripts/azure/*.sh); then
        printf '%s\n' "$leaking" | sed 's/^/  /'
        printf '  a token must be piped onwards, never shown or written to a file\n'
        exit 1
    fi
    printf '  tokens are captured and piped, never displayed\n'

    # Either the standalone CLI or the one the Azure CLI manages. Compiling is
    # the gate: the linter runs inside it, and infrastructure/bicepconfig.json
    # raises the rules that matter here to errors, so a template that builds is a
    # template that passed them.
    if have_bicep; then
        cd "$ROOT/infrastructure"
        step "bicep build"
        # Through the shared helper, which absorbs the difference between the
        # standalone compiler and the one the Azure CLI manages: they take the
        # file differently, and this script having its own copy of that knowledge
        # is exactly how a second caller came to be written without it.
        bicep_cli build main.bicep --stdout >/dev/null
        bicep_cli build-params main.bicepparam --stdout >/dev/null
    else
        printf '\n\033[33mSkipping infrastructure: install Bicep (az bicep install) to check the templates.\033[0m\n'
    fi
fi

printf '\n\033[32mAll checks passed.\033[0m\n'
