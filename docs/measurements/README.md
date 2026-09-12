# Measurements

What a deployed environment actually did, on the day it existed.

Each file here is written by `./scripts/azure/measure.sh` against a live
environment and committed afterwards. They are kept because the environment is
not: this platform is provisioned to be measured and then destroyed (see
`docs/deployment.md`), so every figure in them stops being obtainable the moment
the resource group does. A measurement that outlives its environment is the whole
point of having had one.

Read them as what they are. Each is one run, of one revision, in one region, at
one replica, with no load beside it. A cold start recorded here is a real cold
start — image pull, process start, connection pool — and it is *a* cold start,
not a distribution. Nothing here is a performance benchmark, and
`docs/deployment.md` lists in full what a deployment like this cannot tell you.

The hybrid benchmark that accompanies a run lands in `evaluation/reports/` as
`azure-<date>.json`, and that one file is committed while every other report in
that directory is ignored. The distinction is reproducibility rather than
importance: a local run can be repeated on any laptop in ten minutes, and a run
against Azure needs a deployed environment that existed for an afternoon and has
since been deleted.

One thing in each file is filled in by hand afterwards, because no script can
read it: **actual spend**, from Cost Management, which is the only authority on
what a run cost. Azure's own figures lag by hours, so it is written in the day
after rather than the day of.

Nothing secret goes in these files. They carry endpoints, timings and counts —
never a token, and never a connection string.
