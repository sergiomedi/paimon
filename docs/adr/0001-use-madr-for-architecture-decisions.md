# ADR-0001: Use MADR for architecture decisions

- **Status:** Accepted
- **Date:** 2026-09-01
- **Phase:** 1 — Foundation

## Context and problem statement

This platform will be built across eight phases, with technology choices that are
expensive to reverse: an agent framework, a retrieval backend, an identity model, a
persistence strategy. Six months from now, neither the author nor any reader of the
repository will reliably remember why a given option won — and worse, will not remember
which alternatives were considered and rejected. Decisions then get re-litigated, or
silently violated by new code.

A second force applies specifically to this repository: it is a portfolio artifact.
A reader evaluating engineering judgement learns far more from the reasoning behind a
choice than from the choice itself.

## Decision drivers

- Reasoning must survive longer than the memory of the person who did it.
- The record must be reviewable in the same pull request as the code it justifies.
- Low ceremony: a format heavy enough to be skipped is worse than no format.
- Readable directly on GitHub, with no tooling.

## Considered options

1. **MADR** (Markdown Any Decision Records) files in `docs/adr/`.
2. **Nygard-style ADRs** — the original, lighter format.
3. **A wiki or Notion space.**
4. **No formal record**; rely on commit messages and pull request descriptions.

## Decision

MADR, one file per decision, numbered sequentially, stored in the repository next to the
code it governs.

MADR wins over the Nygard format because of its explicit *Considered options* and
*Consequences* sections. Those two sections carry almost all of the long-term value: they
record what was rejected and what price was accepted. Nygard's format allows both to be
collapsed into prose, and in practice they are.

## Consequences

### Positive

- Decisions are versioned, diffable and reviewable alongside the code.
- Onboarding cost drops sharply: the reasoning trail is readable in order.
- Forces the alternatives to be articulated *before* implementation, which is when
  articulating them can still change the outcome.

### Negative

- Real overhead per decision. Mitigated by applying it only to decisions that are
  expensive to reverse — not to every choice.
- Risk of drift: ADRs describing a system that has since changed. Mitigated by the
  supersede-never-edit rule, which makes drift visible rather than silent.

### Neutral

- The number of ADRs is not a quality metric. Eight well-reasoned records beat forty
  perfunctory ones.

## Alternatives in detail

### Option 3 — A wiki or Notion space

Better editing experience, worse everything else. The record separates from the code, so
it rots invisibly; it cannot be reviewed in a pull request; and it is unavailable to
anyone reading the repository. Would be the right choice for an organization-wide
decision log spanning many repositories.

### Option 4 — No formal record

The default state of most repositories, and the reason most repositories cannot explain
themselves. Commit messages record *what* changed; pull request descriptions record *how*.
Neither reliably records *why this and not that*, and both are hard to search a year later.
