"""The catalogue of canned refusals, and the walk that keeps it complete.

`CANNED_REFUSALS` exists so a calibration sample can exclude every sentence the
platform writes for itself. A catalogue is only worth having if it cannot fall
behind the code, and the way it falls behind is the obvious one: somebody adds a
refusal to an agent and does not think about evaluation.

So this does not check the catalogue against a list written here — that would be
the same list twice. It walks each agent module's source, finds every
module-level string constant, and requires each one to be **classified**: either
it is in the catalogue, or it is named here as something other than a refusal,
with a reason. A new constant is in neither, and the test fails until somebody
decides which it is.

The cost of getting this wrong is measured. The Azure calibration sample was
built with a filter that knew one constant, the single-pass path's; three of the
investigator's reached the labeller, who recognised them as canned and said so.
They inflate agreement, because code already grades them by equality, and they
reveal which harness produced the text.
"""

import ast
from pathlib import Path

import pytest

from paimon.agents import gaps, investigator, postmortem, triage
from paimon.agents.refusals import CANNED_REFUSALS
from paimon.application.use_cases.answer_question import NO_MATERIAL

MODULES = {
    "investigator": investigator,
    "triage": triage,
    "postmortem": postmortem,
    "gaps": gaps,
}

#: Module-level strings that are not refusals, and why. Every name here is a
#: decision somebody made on purpose; a name that is in neither this mapping nor
#: the catalogue fails the walk below.
NOT_A_REFUSAL = {
    "AGENT_NAME": "the agent's name",
    "V1_AGENT_NAME": "the superseded agent's name",
    "SYSTEM_PROMPT": "instructions to the model",
    "SOURCES_HEADER": "formatting for retrieved passages",
    "INSTRUCTION": "instructions to the model",
    "PROCEDURE_FRAMING": "how a symptom is put to retrieval",
    "HISTORY_FRAMING": "how a symptom is put to retrieval",
    "TIMELINE_DOCUMENT_ID": "a document id",
    "PRECEDENT": "a section appended to a draft",
    "REJECTED": "a prefix on a reviewer's note",
    "REVIEW_QUESTION": "what the reviewer is asked",
    "ASPECTS": "the aspects the gaps agent surveys",
    "ALREADY_HAVE": "a reminder injected mid-run, never a final answer",
    "FOUND_NOTHING_BEFORE": "a reminder injected mid-run, never a final answer",
    "USE_THEM": "a reminder injected mid-run, never a final answer",
    "ENOUGH": "a reminder injected mid-run, never a final answer",
    "WITHDRAWN_EXCERPT": "how much of a withdrawn draft is recorded",
    "TOKEN_BUDGET": "a stop reason, not a sentence shown to anyone",
}


def string_constants(module_path: Path) -> dict[str, str]:
    """Every module-level name bound to a plain string, from the source.

    Read with `ast` rather than by importing and inspecting, because the thing
    being guarded against is a constant that exists in the file and that nobody
    wired into the catalogue. Importing would find the same names; parsing keeps
    the test honest about where it is looking.
    """
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign | ast.AnnAssign):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        if value is None:
            continue
        for target in targets:
            if not isinstance(target, ast.Name) or not target.id.isupper():
                continue
            try:
                literal = ast.literal_eval(value)
            except ValueError:
                continue
            if isinstance(literal, str):
                found[target.id] = literal
    return found


class TestTheCatalogueIsComplete:
    @pytest.mark.parametrize("name", sorted(MODULES))
    def test_every_string_constant_is_classified(self, name: str) -> None:
        source = Path(MODULES[name].__file__ or "")
        constants = string_constants(source)
        assert constants, f"{name}: found no constants, so this test proves nothing"

        unclassified = {
            constant: text
            for constant, text in constants.items()
            if constant not in NOT_A_REFUSAL and text not in CANNED_REFUSALS
        }

        assert not unclassified, (
            f"{name} has string constants that are neither in CANNED_REFUSALS nor "
            f"named in NOT_A_REFUSAL: {sorted(unclassified)}. If one is a sentence "
            f"the platform shows instead of an answer, add it to the catalogue — a "
            f"calibration sample that does not exclude it measures the judge on work "
            f"a `==` already does, and tells the labeller which agent produced it."
        )

    def test_the_single_pass_refusal_is_catalogued(self) -> None:
        assert NO_MATERIAL in CANNED_REFUSALS

    @pytest.mark.parametrize(
        "sentence",
        [
            investigator.RETRIEVAL_FAILED,
            investigator.UNSUPPORTED,
            investigator.OUT_OF_STEPS,
            investigator.OUT_OF_BUDGET,
            investigator.WENT_IN_CIRCLES,
        ],
    )
    def test_every_investigator_refusal_is_catalogued(self, sentence: str) -> None:
        # Named one by one because these are the three that escaped: the Azure
        # sample's filter knew NO_MATERIAL and nothing else.
        assert sentence in CANNED_REFUSALS

    def test_the_catalogue_holds_no_empty_strings(self) -> None:
        # An empty string in the exclusion set would drop every attempt whose
        # text is empty, which is a failed run rather than a canned refusal.
        assert all(sentence.strip() for sentence in CANNED_REFUSALS)
