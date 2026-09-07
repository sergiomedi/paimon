"""Turning benchmark averages into measurements.

A mean on its own is not a measurement. This dataset has fifteen questions; the
difference between two configurations on fifteen questions is frequently smaller
than the noise, and a README claiming that "retrieval changes are accepted or
rejected on numbers" is only true once the numbers say how sure they are.

The approach is the one Anthropic's *Adding Error Bars to Evals* sets out. The
questions are a sample from the population of questions somebody might ask, so:

* report a standard error from the central limit theorem rather than bootstrapping;
* widen it when questions are **correlated** — ours cluster by source document,
  and treating eight questions about one runbook as eight independent
  observations overstates confidence, sometimes by a factor of three;
* and when comparing two configurations, take the difference **per question**
  rather than subtracting two aggregates.

That last one is the important one, and the reason is arithmetic:

    Var(paired) = Var(unpaired) - 2*Cov(A, B)/n

Two retrieval configurations agree about which questions are hard. That
correlation is positive and often large, so pairing removes most of the variance
— which is how a real three-point improvement becomes distinguishable from noise
on a dataset this size, and how a change that only looks like an improvement is
caught.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

#: 95% two-sided, the convention. Reported as an interval rather than a verdict:
#: a threshold turns a measurement into a yes/no and throws away the size of the
#: effect, which is usually the interesting part.
DEFAULT_CONFIDENCE = 0.95

#: Below two clusters there is nothing to compare, and below two observations
#: there is no spread to measure.
_MINIMUM_GROUPS: Final = 2

#: The continued fraction has converged once a step changes the result by
#: less than this.
_CONVERGENCE: Final = 1e-12


@dataclass(frozen=True, slots=True)
class Estimate:
    """A mean, and how much it can be trusted.

    Attributes:
        mean: The sample mean.
        standard_error: Standard error of that mean.
        n: Observations behind it.
        clusters: Independent groups, when the observations were clustered. None
            when every observation was treated as independent.
    """

    mean: float
    standard_error: float
    n: int
    clusters: int | None = None

    @property
    def degrees_of_freedom(self) -> int:
        """What the interval is computed against.

        The number of **clusters** minus one when the data is clustered, not the
        number of observations. Eight questions drawn from two documents carry
        about as much information as two independent observations, and using
        seven degrees of freedom would quietly claim otherwise.
        """
        groups = self.clusters if self.clusters is not None else self.n
        return max(groups - 1, 1)

    def interval(self, confidence: float = DEFAULT_CONFIDENCE) -> tuple[float, float]:
        """The confidence interval around the mean."""
        half = critical_t(confidence, self.degrees_of_freedom) * self.standard_error
        return (self.mean - half, self.mean + half)

    def format(self, *, percent: bool = False, confidence: float = DEFAULT_CONFIDENCE) -> str:
        """Render as ``mean +/- half-width``, which is how it should be read."""
        low, high = self.interval(confidence)
        half = (high - low) / 2
        if percent:
            return f"{self.mean:.1%} ± {half:.1%}"
        return f"{self.mean:.3f} ± {half:.3f}"


@dataclass(frozen=True, slots=True)
class PairedDifference:
    """The difference between two configurations, measured question by question.

    Attributes:
        mean: Average of the per-question differences, ``a - b``.
        standard_error: Standard error of that average.
        n: Questions compared.
        clusters: Independent groups behind them, when clustered.
        correlation: How closely the two configurations agreed about which
            questions were hard. Reported because it is what pairing exploits:
            near zero and pairing bought nothing, near one and it bought a great
            deal.
    """

    mean: float
    standard_error: float
    n: int
    clusters: int | None = None
    correlation: float = 0.0

    @property
    def degrees_of_freedom(self) -> int:
        """Clusters minus one when clustered, observations minus one otherwise."""
        groups = self.clusters if self.clusters is not None else self.n
        return max(groups - 1, 1)

    @property
    def t_statistic(self) -> float:
        """The difference in units of its own standard error.

        A difference with no spread is either certain or absent, and the two must
        not be collapsed. "Every question improved by exactly one point" has a
        standard error of zero and is as significant as a result gets; returning
        zero for it — as the first version of this did — reported it as
        indistinguishable from noise, which a test caught and nothing else would
        have.
        """
        if self.standard_error == 0.0:
            return 0.0 if self.mean == 0.0 else math.copysign(math.inf, self.mean)
        return self.mean / self.standard_error

    @property
    def p_value(self) -> float:
        """Two-sided probability of a difference this large if there were none.

        Reported alongside the interval rather than instead of it. A p-value says
        whether an effect is distinguishable from zero and nothing about whether
        it is worth having.
        """
        return 2.0 * student_t_sf(abs(self.t_statistic), self.degrees_of_freedom)

    def interval(self, confidence: float = DEFAULT_CONFIDENCE) -> tuple[float, float]:
        """The confidence interval around the difference."""
        half = critical_t(confidence, self.degrees_of_freedom) * self.standard_error
        return (self.mean - half, self.mean + half)

    def is_significant(self, alpha: float = 0.05) -> bool:
        """Whether the interval excludes zero at this level."""
        return self.p_value < alpha

    def format(self, *, percent: bool = False) -> str:
        """Render the difference, its interval and how sure it is."""
        low, high = self.interval()
        if percent:
            body = f"{self.mean:+.1%}  [{low:+.1%}, {high:+.1%}]"
        else:
            body = f"{self.mean:+.3f}  [{low:+.3f}, {high:+.3f}]"
        return f"{body}  p={self.p_value:.3f}"


def estimate(values: Sequence[float]) -> Estimate:
    """Mean and standard error of a sample, by the central limit theorem.

    Args:
        values: One observation per question.

    Returns:
        The estimate. A single observation has no measurable spread, so its
        standard error is zero — which is honest, and is why one question is not
        a benchmark.
    """
    n = len(values)
    if n == 0:
        return Estimate(mean=0.0, standard_error=0.0, n=0)
    mean = sum(values) / n
    if n == 1:
        return Estimate(mean=mean, standard_error=0.0, n=1)
    variance = sum((value - mean) ** 2 for value in values) / (n - 1)
    return Estimate(mean=mean, standard_error=math.sqrt(variance / n), n=n)


def clustered_estimate(values: Sequence[float], clusters: Sequence[str]) -> Estimate:
    """Mean and a cluster-robust standard error.

    Questions drawn from the same document are not independent observations: they
    share its wording, its structure and whatever the chunker did to it. Treating
    them as independent understates the standard error — by a factor of three or
    more in the literature — and produces confident conclusions about nothing.

    Args:
        values: One observation per question.
        clusters: The group each observation belongs to, same order and length.

    Returns:
        The estimate, carrying the number of independent groups behind it.

    Raises:
        ValueError: If the two sequences differ in length.
    """
    if len(values) != len(clusters):
        msg = f"{len(values)} values and {len(clusters)} cluster labels"
        raise ValueError(msg)

    n = len(values)
    if n == 0:
        return Estimate(mean=0.0, standard_error=0.0, n=0, clusters=0)

    mean = sum(values) / n
    grouped: dict[str, float] = {}
    for value, cluster in zip(values, clusters, strict=True):
        grouped[cluster] = grouped.get(cluster, 0.0) + (value - mean)

    group_count = len(grouped)
    if group_count < _MINIMUM_GROUPS:
        # One cluster is one observation. Claiming a spread from it would be
        # claiming to have measured something that was never varied.
        return Estimate(mean=mean, standard_error=0.0, n=n, clusters=group_count)

    # The sandwich estimator: sum the *cluster* residual totals, not the
    # individual ones. Within a cluster, errors are allowed to be as correlated
    # as they like — which is the whole point.
    correction = group_count / (group_count - 1)
    total = sum(residual**2 for residual in grouped.values())
    return Estimate(
        mean=mean,
        standard_error=math.sqrt(correction * total) / n,
        n=n,
        clusters=group_count,
    )


def paired_difference(
    a: Sequence[float], b: Sequence[float], clusters: Sequence[str] | None = None
) -> PairedDifference:
    """Compare two configurations question by question.

    Args:
        a: One configuration's per-question scores.
        b: The other's, for the **same questions in the same order**.
        clusters: The group each question belongs to, when they are correlated.

    Returns:
        The mean difference ``a - b`` with its uncertainty.

    Raises:
        ValueError: If the sequences differ in length. Pairing scores from
            different questions is not a paired comparison, and the failure would
            otherwise be a silently wrong number.
    """
    if len(a) != len(b):
        msg = f"paired comparison needs the same questions: {len(a)} against {len(b)}"
        raise ValueError(msg)

    differences = [first - second for first, second in zip(a, b, strict=True)]
    base = (
        clustered_estimate(differences, clusters) if clusters is not None else estimate(differences)
    )
    return PairedDifference(
        mean=base.mean,
        standard_error=base.standard_error,
        n=base.n,
        clusters=base.clusters,
        correlation=correlation(a, b),
    )


def compare_metric(
    mine: Mapping[str, Sequence[float]],
    theirs: Mapping[str, Sequence[float]],
    metric: str,
    clusters: Sequence[str] | None = None,
) -> PairedDifference:
    """Pair two runs' per-question scores for one metric.

    Shared by the retrieval and answering benchmarks, because the mistake it
    guards against is the same in both: comparing a metric one of the runs never
    recorded, which would otherwise be a KeyError three frames away from the
    thing that caused it.

    Raises:
        ValueError: If either run has no scores under that name.
    """
    ours, yours = mine.get(metric), theirs.get(metric)
    if ours is None or yours is None:
        available = ", ".join(sorted(mine)) or "none"
        msg = f"no per-question scores for '{metric}'; this run has: {available}"
        raise ValueError(msg)
    return paired_difference(ours, yours, clusters)


def correlation(a: Sequence[float], b: Sequence[float]) -> float:
    """Pearson correlation between two sets of per-question scores.

    Zero when either is constant, which is the honest answer: a configuration
    that scored identically everywhere agreed with nothing in particular.
    """
    n = len(a)
    if n < _MINIMUM_GROUPS or len(b) != n:
        return 0.0
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    covariance = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b, strict=True))
    spread_a = math.sqrt(sum((x - mean_a) ** 2 for x in a))
    spread_b = math.sqrt(sum((y - mean_b) ** 2 for y in b))
    if spread_a == 0.0 or spread_b == 0.0:
        return 0.0
    return covariance / (spread_a * spread_b)


def student_t_sf(t: float, degrees_of_freedom: int) -> float:
    """Upper tail of Student's t distribution: P(T > t).

    Implemented here rather than pulled in with SciPy. The platform needs one
    function from it; SciPy is tens of megabytes of compiled numerics, and a
    dependency that large for a tail probability is a dependency that shows up in
    every image build and every security advisory for the rest of the project.

    The identity is the standard one, in terms of the regularized incomplete
    beta function:

        P(T > t) = 0.5 * I_{d/(d + t^2)}(d/2, 0.5)   for t >= 0, with d the
        degrees of freedom
    """
    if degrees_of_freedom < 1:
        return 0.5
    if t <= 0.0:
        return 1.0 - student_t_sf(-t, degrees_of_freedom) if t < 0 else 0.5
    x = degrees_of_freedom / (degrees_of_freedom + t * t)
    return 0.5 * _incomplete_beta(x, degrees_of_freedom / 2.0, 0.5)


def critical_t(confidence: float, degrees_of_freedom: int) -> float:
    """The t value cutting off ``confidence`` of the distribution, two-sided.

    Found by bisection on the tail probability rather than from a table. A table
    would have to be interpolated for the degrees of freedom that actually occur
    — clusters minus one, which is whatever the dataset happens to contain.
    """
    target = (1.0 - confidence) / 2.0
    low, high = 0.0, 1000.0
    for _ in range(200):
        middle = (low + high) / 2.0
        if student_t_sf(middle, degrees_of_freedom) > target:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0


def _incomplete_beta(x: float, a: float, b: float) -> float:
    """Regularized incomplete beta function I_x(a, b).

    Continued fraction (Lentz's method), with the standard reflection that keeps
    the fraction in its fast-converging region.
    """
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    )
    if x > (a + 1.0) / (a + b + 2.0):
        return 1.0 - _incomplete_beta(1.0 - x, b, a)
    return front * _beta_continued_fraction(x, a, b) / a


def _beta_continued_fraction(x: float, a: float, b: float) -> float:
    """Evaluate the continued fraction for the incomplete beta function.

    Lentz's method, with the even and odd steps taken separately inside one
    iteration. Merging them looks tidier and shifts the recurrence by half a
    term, which converges to a plausible wrong answer — the first version of this
    returned negative probabilities, which is at least an obvious kind of wrong.
    """
    tiny = 1e-30
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0

    c = 1.0
    d = 1.0 - qab * x / qap
    d = tiny if abs(d) < tiny else d
    d = 1.0 / d
    result = d

    for m in range(1, 300):
        m2 = 2 * m

        # Even step.
        numerator = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + numerator * d
        d = tiny if abs(d) < tiny else d
        c = 1.0 + numerator / c
        c = tiny if abs(c) < tiny else c
        d = 1.0 / d
        result *= d * c

        # Odd step.
        numerator = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + numerator * d
        d = tiny if abs(d) < tiny else d
        c = 1.0 + numerator / c
        c = tiny if abs(c) < tiny else c
        d = 1.0 / d
        step = d * c
        result *= step

        if abs(step - 1.0) < _CONVERGENCE:
            break
    return result


def group_by_document(documents: Sequence[Sequence[str]]) -> list[str]:
    """Label each question with the cluster it belongs to.

    A question's cluster is the documents it draws on, joined — so two questions
    about the same runbook share a cluster and a question spanning two documents
    forms its own. Crude, and better than pretending the questions are
    independent.
    """
    return ["+".join(sorted(set(names))) or "unattributed" for names in documents]


def to_mapping(estimate_: Estimate) -> Mapping[str, float | int | None]:
    """Render an estimate for a JSON report."""
    low, high = estimate_.interval()
    return {
        "mean": estimate_.mean,
        "standard_error": estimate_.standard_error,
        "ci_low": low,
        "ci_high": high,
        "n": estimate_.n,
        "clusters": estimate_.clusters,
    }


__all__ = [
    "DEFAULT_CONFIDENCE",
    "Estimate",
    "PairedDifference",
    "clustered_estimate",
    "compare_metric",
    "correlation",
    "critical_t",
    "estimate",
    "group_by_document",
    "paired_difference",
    "student_t_sf",
    "to_mapping",
]
