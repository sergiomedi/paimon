#!/usr/bin/env bash
# How this repository invokes the Bicep compiler. Sourced, never run.
#
# Sourced by scripts/check.sh and by scripts/azure/_common.sh, which is the whole
# point: the two ways of having Bicep installed take their arguments differently,
# and that knowledge lived in two places until one of them was written without it.
#
#   bicep build-params main.bicepparam --stdout        # the standalone binary
#   az bicep build-params --file main.bicepparam --stdout   # the Azure CLI's
#
# A path passed positionally to `az bicep` is simply not seen. The failure is a
# non-zero exit with nothing useful on stdout, which read as "the parameter file
# will not compile" on a machine where `./scripts/check.sh` compiled it happily
# thirty seconds earlier — because that script had the other branch.

# Whether a Bicep compiler is available at all.
have_bicep() {
    command -v bicep >/dev/null 2>&1 ||
        { command -v az >/dev/null 2>&1 && az bicep version >/dev/null 2>&1; }
}

# Run a Bicep subcommand against a file, whichever compiler is installed.
#
# Usage: bicep_cli <subcommand> <file> [flags...]
bicep_cli() {
    local subcommand="$1" file="$2"
    shift 2

    if command -v bicep >/dev/null 2>&1; then
        bicep "$subcommand" "$file" "$@"
    elif command -v az >/dev/null 2>&1 && az bicep version >/dev/null 2>&1; then
        # --file, not positional. This line is the reason this file exists.
        az bicep "$subcommand" --file "$file" "$@"
    else
        return 127
    fi
}
