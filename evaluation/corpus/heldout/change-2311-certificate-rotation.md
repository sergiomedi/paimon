# Change record CR-2311: rotate the mesh certificate authority

Sample material written for this repository's held-out benchmark. Not a
description of any real system.

## What changed

The service mesh certificate authority was rotated on 2026-07-02 during the
Thursday change window. All 41 services re-issued leaf certificates against the
new root within the rotation window.

## How it was done

The operator followed **RB-207** end to end and did not deviate. The step that
decides whether a rotation is safe to start is the one that compares the
shortest remaining leaf lifetime against the rotation window; this change record
does not restate it, because a procedure copied into a change record is a
procedure that stops being updated.

## What went wrong

Two services missed the window and served an expired leaf for nine minutes. Both
had been excluded from the mesh inventory by a label typo, so RB-207's
pre-flight count reported 39 services rather than 41.

## Follow-up

The pre-flight count is now taken from the mesh control plane rather than from
the inventory label. The rotation window itself was not changed; it is recorded
in the platform limits reference and is the same window RB-207 refers to.
