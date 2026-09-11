# ADR-0035: Bicep, and templates small enough to read

- **Status:** Accepted
- **Date:** 2026-09-11
- **Phase:** 7 — Cloud

## Context and problem statement

Everything this phase creates has to be created the same way twice, because the
environment is built to be destroyed (ADR-0036). That makes infrastructure as code a
requirement rather than a preference, and leaves two questions: which language, and
whether to assemble it from somebody else's modules.

## Decision drivers

- The environment is created and destroyed repeatedly, so provisioning has to be one
  command that is safe to re-run.
- A reader should be able to see **exactly what is granted to whom**. This deployment's
  security model is entirely role assignments; if those are not reviewable, nothing is.
- The build must not depend on the network more than it has to. A template that cannot be
  compiled offline cannot be compiled in a hurry.
- Azure, and only Azure. This platform's portability story is its ports and adapters, not
  its cloud provider.

## Considered options

### Language: Terraform or Bicep

Microsoft declines to recommend either, and maintains a neutral comparison rather than a
preference. So the trade-off is the honest one:

**Terraform** brings a real `plan`, a mature module ecosystem, and portability to other
clouds. It also brings **state**: a file that has to live somewhere, be locked, and be
kept in step with reality, which is an operational component this project would be adding
for a single-cloud deployment that already has enough moving parts.

**Bicep** has no state — the deployed resources are the state — compiles to ARM so new
resource types and API versions are available the day they ship, and needs nothing
installed beyond a compiler. Its weakness is real and worth naming: `what-if` is a poorer
preview than `terraform plan`. It cannot resolve `reference()` expressions and reports
them as changes, and service-applied defaults appear as deletions of properties nobody
set. It is a tool that has to be read rather than trusted.

**Bicep**, on the grounds that a single-cloud project should not take on a state store,
and that the preview weakness is a thing to know rather than a thing that breaks.

### Assembly: Azure Verified Modules, or resources written here

**Azure Verified Modules** are Microsoft's sanctioned module layer — Well-Architected
defaults encoded once, consumed from a public registry, and now the basis of the platform
landing zone. Using them is the current mainstream answer and it is a defensible one.

They are not used here, for three reasons that apply to this template and would not apply
to a larger one:

1. **The resources are small and the modules are not.** This template creates an identity,
   a workspace, a vault, a registry and an environment. Each is between ten and thirty
   lines written directly. The corresponding modules are general-purpose, several hundred
   lines each, and configured through a parameter surface that has to be learned in order
   to know what the defaults are.
2. **The security model is the whole deliverable, and it should be readable in one pass.**
   Three role assignments with the role names beside their GUIDs, at the scope they apply
   to, is the entire authorization story of this environment. Expressed through a module's
   `roleAssignments` array it is the same grant with one more indirection between the
   reader and it.
3. **They pin the build to a registry.** Modules are restored from `mcr.microsoft.com` at
   compile time, so compiling becomes a network operation — in CI, and for anyone who
   clones this. Written directly, the template compiles from the file.

There is a fourth, quieter reason: the resource modules are still **0.x**, and minor
versions break. That is a manageable cost when a module is saving hundreds of lines and a
poor trade when it is saving twenty.

**This is decided per template, not as a rule.** The next batch adds PostgreSQL with a
private endpoint and a private DNS zone, which is exactly the kind of multi-resource
wiring that is fiddly to get right and that these modules encode well. That batch
re-evaluates rather than inheriting this answer.

### Workflow: the Azure Developer CLI, or scripts

`azd` wraps provisioning and deployment, scaffolds templates, and manages named
environments. It is not used: the workflow here is four scripts, each of which is shorter
than the explanation of what `azd` would do in its place, and one of them — the teardown —
needs behaviour that is specific to this project and would have to be written anyway.
`azd` also brings its own environment model, and this project already has one in its
settings. Not every workflow needs a workflow tool.

## Decision

Bicep, written directly, compiled in CI, with four shell scripts around it.

The template is deployed at **subscription scope** so the resource group is part of it
rather than a prerequisite somebody has to remember, and parameters come from the
environment through `readEnvironmentVariable` rather than from a committed file — two of
the three are facts about whoever is deploying, and a parameter file carrying those is one
that gets committed by accident.

`infrastructure/bicepconfig.json` raises the linter rules that matter to **errors**:
unused parameters, hardcoded locations, interpolation, and secrets in outputs. The linter
runs inside the compiler, so a template that builds is a template that passed them, and
`scripts/check.sh` and CI both build it. Infrastructure that nothing checks rots exactly
like code that nothing checks — and unlike code, nobody notices until the day it is needed.

## Consequences

**Positive.** The whole environment is one file plus one module, readable end to end in a
few minutes, with no external versions to track. It compiles offline. The role assignments
can be audited by reading them.

**Negative.** Well-Architected defaults that a verified module would have applied — a
diagnostic setting here, a network restriction there — are now this project's to remember.
That is a real cost and it is paid in review rather than in dependency management. When a
resource arrives whose safe configuration is genuinely intricate, the answer is to use the
module for that resource, not to defend this decision.

**Negative.** `what-if` output has to be read with its limitations in mind. They are
documented at the top of `scripts/azure/preview.sh` rather than in a wiki nobody opens.

**Not done here.** Deployment stacks, which manage a set of resources as a unit and can
refuse deletions outside the template, are the right tool for a permanent environment and
are documented as the successor to Blueprints. They are not used for an environment whose
teardown is a supported operation performed regularly — the feature exists to prevent
exactly what this project does on purpose. If a long-lived environment is ever added, it
gets a stack with `actionOnUnmanage: detachAll`, and this paragraph is the note saying so.
