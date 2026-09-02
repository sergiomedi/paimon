# Node maintenance

Sample material written for this repository's benchmark. Not a description of any
real system.

## Draining a node

Cordon the node first so the scheduler stops placing new pods on it. Cordoning is
not eviction: pods already running keep running, and the node keeps serving
traffic until they are moved.

Once the node is cordoned, evict the running pods and wait for them to reschedule
elsewhere. Eviction respects pod disruption budgets, so a workload that declares
one will be moved a replica at a time.

## Kernel upgrades

Nodes are drained before any kernel upgrade, without exception. An in-place kernel
upgrade on a node that is still serving traffic has caused two of our three
longest outages.

Reboot the node after the packages are installed. A kernel that is installed but
not booted reports the new version to the package manager and the old version to
the running system, which makes every subsequent audit wrong.

## Returning a node to service

Uncordon the node once it has rejoined the cluster and reports Ready. Do not
uncordon a node that is Ready but has not yet pulled its DaemonSet images: it will
accept pods it cannot run.

## Known issues

Eviction stalls indefinitely when a pod has no disruption budget and its
controller will not create a replacement — a single-replica StatefulSet is the
usual cause. Delete the pod directly after confirming the workload tolerates it.
