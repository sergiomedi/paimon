"""The guard that stands between the integration suite and a real database.

Integration tests truncate tables. Twice now they have truncated tables that
held a measurement: a corpus in the middle of a benchmark, which produced a
plausible and entirely false result, and the `agent_runs` rows holding the
withdrawn drafts of a finished run, found missing only when somebody looked.

The first was answered by guarding one more table where it was used. That did
not generalise — the next destructive test truncated a different table — so the
check is on the name of the database instead. Whatever a test truncates, it can
only truncate it somewhere disposable.

This test lives in the unit suite on purpose: it must run even when no database
is reachable, because it is about refusing, not about connecting.
"""

import pytest

from tests.integration.conftest import (
    TEST_DATABASE_SUFFIX,
    WrongDatabaseError,
    require_disposable,
)


class TestRefusingANonDisposableDatabase:
    @pytest.mark.parametrize("name", ["paimon", "paimon_prod", "postgres", "paimon_test_copy"])
    def test_a_name_that_does_not_end_in_test_is_refused(self, name: str) -> None:
        with pytest.raises(WrongDatabaseError) as refusal:
            require_disposable(name)

        assert name in str(refusal.value), "the refusal must name the database it refused"
        assert "TRUNCATE" in str(refusal.value), "it must say why"

    @pytest.mark.parametrize("name", ["paimon_test", "ci_test", "anything_test"])
    def test_a_disposable_name_is_allowed(self, name: str) -> None:
        require_disposable(name)

    def test_the_suffix_is_what_the_message_tells_people_to_use(self) -> None:
        # Otherwise the error explains one rule and the code enforces another.
        with pytest.raises(WrongDatabaseError) as refusal:
            require_disposable("paimon")

        assert TEST_DATABASE_SUFFIX in str(refusal.value)

    def test_the_refusal_says_how_to_fix_it(self) -> None:
        # A guard that blocks a contributor without telling them the next
        # command is a guard that gets deleted.
        with pytest.raises(WrongDatabaseError) as refusal:
            require_disposable("paimon")

        message = str(refusal.value)
        assert "CREATE DATABASE" in message
        assert "check.sh" in message
