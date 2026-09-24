# Platform limits reference

Sample material written for this repository's held-out benchmark. Not a
description of any real system.

The single place these numbers live. Procedures reference this file rather than
restating it, because a number written in two places disagrees with itself
within a quarter.

## Certificates

The mesh rotation window is **six hours**. A rotation may not begin if the
shortest remaining leaf lifetime is below it.

Leaf certificates are issued for 72 hours and re-issued at one third of
remaining life. The root is valid for one year.

## Mesh

The control plane counts 41 services. Batch size for re-issue is ten; a larger
batch has been tried and saturates the issuing service.

## Change windows

The Thursday change window is four hours, 22:00 to 02:00 UTC. A change expected
to exceed it needs the change advisory board, not an operator's judgement.

## Retention

Certificate issuance logs are kept for 400 days, which is longer than the root's
validity on purpose.
