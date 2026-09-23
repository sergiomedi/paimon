# RB-207: rotating the mesh certificate authority

Sample material written for this repository's held-out benchmark. Not a
description of any real system.

## When to use this

A scheduled root rotation, or an emergency rotation after a suspected key
compromise. For an expired *leaf* certificate on a single service, this is the
wrong procedure and RB-208 is the right one.

## Steps

1. Take the service count from the mesh control plane. Do not use the inventory
   label: a service missing its label is a service that will not be counted and
   will not be rotated.

2. Read the shortest remaining leaf lifetime across every counted service.

3. **Compare that lifetime against the rotation window.** If the shortest
   remaining lifetime is less than the window, stop: a rotation started now will
   expire leaves before they re-issue. The window is recorded in the platform
   limits reference and is not restated here.

4. Publish the new root, then trigger re-issue in batches of ten services.

5. Verify every counted service is serving a leaf signed by the new root before
   retiring the old one.

## What not to do

Do not retire the old root before step five completes. A service that has not
re-issued will fail every mutual TLS handshake the moment the old root goes, and
the failure looks like a network partition rather than a certificate problem.

## Related

RB-208 covers a single expired leaf. CR-2311 is the rotation this runbook was
revised after.
