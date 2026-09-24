"""Every sentence the platform writes when it will not answer.

These are not model output. They are constants the code substitutes when a run
cannot produce a grounded answer — no material, nothing citable, out of steps,
out of budget, going in circles — and they matter to evaluation twice over.

**Code grades them exactly**, by equality against these very strings, so a judge
asked to classify one is being measured on the single part of the job it was
never given. Counting its verdict on them inflates agreement with work a
`==` already did.

**And they give the system away.** Only certain agents can emit certain
sentences, so a labeller who recognises one knows which harness produced the
text and is no longer rating the response alone.

Both reasons say the same thing: a calibration sample must exclude them. The
Azure sample did not. Its filter was given one constant — the single-pass
path's — and three investigator refusals reached the labeller, who recognised
them and said so. This module exists so that the filter can be given *all* of
them, and so that a new one cannot be added without a test noticing.
"""

from paimon.agents import gaps, investigator, postmortem, triage
from paimon.application.use_cases.answer_question import NO_MATERIAL

#: Every canned refusal, from every agent and from the single-pass path.
#:
#: Assembled by importing rather than by copying, so a change to any of these
#: sentences travels here by itself; the risk this guards is a *new* constant
#: nobody adds, which is what the catalogue test in
#: ``tests/unit/agents/test_refusals.py`` walks the modules to catch.
CANNED_REFUSALS: frozenset[str] = frozenset(
    {
        NO_MATERIAL,
        investigator.RETRIEVAL_FAILED,
        investigator.UNSUPPORTED,
        investigator.OUT_OF_STEPS,
        investigator.OUT_OF_BUDGET,
        investigator.WENT_IN_CIRCLES,
        triage.UNSUPPORTED,
        triage.RETRIEVAL_FAILED,
        postmortem.UNSUPPORTED,
        gaps.NOTHING_INDEXED,
        gaps.SURVEY_FAILED,
    }
)

__all__ = ["CANNED_REFUSALS"]
