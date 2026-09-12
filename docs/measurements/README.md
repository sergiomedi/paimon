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

Two things in each file are filled in by hand afterwards, because no script can
read them:

- **actual spend**, from Cost Management, which is the only authority on what a
  run cost;
- **the hybrid benchmark** against the real models and search service, which is
  the number worth quoting because it was measured against what a deployment
  would actually use.

Nothing secret goes in these files. They carry endpoints, timings and counts —
never a token, and never a connection string.
