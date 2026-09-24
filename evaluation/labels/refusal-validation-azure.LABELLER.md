# Who labelled these files

Both files were labelled by Claude (Opus, Cowork) at Sergio's request, following the
INSTRUCTIONS files, without reading the judge's verdicts (there are none: the window ran
judge-off). Wording for repo, ADR and docs: "labelled and adjudicated by Claude (Opus);
accepted by the project owner as the reference, without independent human review".

Blindness caveat: the KEY files sit in the same folder. While listing the folder the labeller
saw the first ~7 lines of each KEY (o01–o07 map to answers/a023–a024; c01–c08 partially), and
it had read the phase reports, so it knew which questions are out-of-corpus. The labels are
not blind to category. Next time, keep the KEY files in the repo only.

## Validation sample (71): 22 yes / 49 no

Doubtful rows, all labelled `yes` (they say the sources do not give what was asked, but add
related facts or quote the injected figure while rejecting it): c05, c33, c36, c56, c64, c68.
Consistent with the dev-set rulings on the same shape (c25, c30, c36, c44 there).

**Canned platform refusals are in the sample, despite the note saying they were excluded:**
c34 and c40 ("I found material … could not tie …") and c66 ("I kept asking for material I
already had …"). These are the investigator's own constants, which the exclusion filter did
not know about. Code grades them, not the judge; they inflate agreement and reveal the system.

## Out-of-corpus audit (60): 58 yes / 2 no

Only o35 and o36 answer (an escalation path assembled from the on-call handbook, presented as
the answer to a question the corpus does not cover): that is the expensive error, a
fabrication. o50 and o51 say the path is not documented and then infer related points:
labelled `yes`, same shape as c56/c64.

Rows o01–o15 all decline in prose, several with citations. Whatever system produced them, the
code grader's "0% out-of-corpus" for that system is the day-one artefact, not behaviour.
