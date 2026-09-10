"""Telling somebody that a long run is still a run.

An answering benchmark over fifteen questions makes fifteen generation calls and
three judgements each, and on a local model that is fifteen minutes. Fifteen
minutes of silence is indistinguishable from a hung process, and the reasonable
thing for somebody to do with a hung process is kill it — which is how a run
that was working gets abandoned four cases from the end.

A callback rather than printing from inside the runner. The evaluation package
knows what it has finished; it does not know whether anybody is watching, where
that output belongs, or whether the caller is a terminal, a CI job or a test.
Those are the command-line interface's business, and it is the one that supplies
this.
"""

from typing import Protocol


class Progress(Protocol):
    """Notified once per finished case, in the order the cases were run."""

    def __call__(self, *, done: int, total: int, case_id: str) -> None:
        """Report that a case has finished.

        Args:
            done: Cases finished so far, counting this one.
            total: Cases in the run.
            case_id: The case that just finished.
        """
        ...


__all__ = ["Progress"]
