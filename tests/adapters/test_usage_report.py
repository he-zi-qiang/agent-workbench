"""The arithmetic behind the usage page, with no database in it.

The SQL half of `adapters/persistence/usage.py` is exercised in
`tests/persistence`, which skips without a DSN -- and CI's quality job runs
offline, so anything only reachable through a live PostgreSQL is not covered
there. The mistakes worth catching are not in the joins anyway; they are in the
folding: double counting a delegated run, quietly bucketing an unknown session
mode, and reporting an unpriced total as though the model were free.

`build_report` takes rows, so all three are reachable from a list of tuples.
"""

from __future__ import annotations

from datetime import UTC, datetime

from agent_workbench.adapters.persistence.usage import build_report

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def usage(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cost_micro_usd: int = 0,
) -> dict[str, object]:
    """A terminal event's payload, shaped the way the runtime writes it."""

    return {
        "usage": {
            "steps": 1,
            "tool_calls": 0,
            "tokens": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_tokens": cache_read_tokens,
                "cache_write_tokens": cache_write_tokens,
            },
            "cost_micro_usd": cost_micro_usd,
        }
    }


def report(**overrides: object):
    kwargs: dict[str, object] = {
        "task_payloads": [],
        "session_payloads": [],
        "profiles": {},
        "delegated_ids": set(),
        "runs_in_flight": 0,
        "since": None,
        "until": NOW,
    }
    kwargs.update(overrides)
    return build_report(**kwargs)  # type: ignore[arg-type]


class TestModes:
    def test_the_three_modes_land_in_three_buckets(self) -> None:
        result = report(
            task_payloads=[("run_t", usage(input_tokens=100, output_tokens=10))],
            session_payloads=[
                ("run_c", usage(input_tokens=200, output_tokens=20), "chat"),
                ("run_k", usage(input_tokens=300, output_tokens=30), "code"),
            ],
        )

        assert result.by_mode["task"].tokens.input_tokens == 100
        assert result.by_mode["chat"].tokens.input_tokens == 200
        assert result.by_mode["code"].tokens.input_tokens == 300

    def test_a_session_mode_this_build_does_not_know_is_dropped(self) -> None:
        """Not folded into chat.

        A wrong column reads as a real number and gets quoted; a missing one
        gets asked about. This is the difference between a total a reader can
        check against the log and one they cannot.
        """

        result = report(
            session_payloads=[
                ("run_c", usage(input_tokens=200), "chat"),
                ("run_x", usage(input_tokens=999), "notebook"),
            ]
        )

        assert result.by_mode["chat"].tokens.input_tokens == 200
        assert "notebook" not in result.by_mode
        assert sum(slice_.runs for slice_ in result.by_mode.values()) == 1

    def test_runs_are_counted_so_a_total_says_what_it_rests_on(self) -> None:
        result = report(
            task_payloads=[
                ("run_a", usage(input_tokens=10)),
                ("run_b", usage(input_tokens=10)),
            ]
        )

        assert result.by_mode["task"].runs == 2


class TestDelegation:
    def test_a_delegated_run_counts_in_the_mode_total(self) -> None:
        """It is real spend on this bill even though no parent budget saw it."""

        result = report(
            task_payloads=[
                ("run_parent", usage(input_tokens=100)),
                ("run_child", usage(input_tokens=400)),
            ],
            delegated_ids={"run_child"},
        )

        assert result.by_mode["task"].tokens.input_tokens == 500

    def test_and_is_reported_again_beside_it_rather_than_subtracted(self) -> None:
        result = report(
            task_payloads=[
                ("run_parent", usage(input_tokens=100)),
                ("run_child", usage(input_tokens=400)),
            ],
            delegated_ids={"run_child"},
        )

        assert result.delegated.tokens.input_tokens == 400
        assert result.delegated.runs == 1

    def test_a_delegation_whose_child_is_outside_the_window_adds_nothing(self) -> None:
        """Under-report the split rather than invent a row for it."""

        result = report(
            task_payloads=[("run_parent", usage(input_tokens=100))],
            delegated_ids={"run_child_not_in_window"},
        )

        assert result.delegated.runs == 0
        assert result.by_mode["task"].tokens.input_tokens == 100


class TestModels:
    def test_spend_is_attributed_to_the_profile_the_run_declared(self) -> None:
        result = report(
            task_payloads=[("run_a", usage(input_tokens=100, cost_micro_usd=7))],
            session_payloads=[
                ("run_b", usage(input_tokens=50, cost_micro_usd=3), "chat")
            ],
            profiles={"run_a": "deep", "run_b": "main"},
        )

        assert result.by_model["deep"].cost_micro_usd == 7
        assert result.by_model["main"].cost_micro_usd == 3

    def test_a_run_whose_start_is_outside_the_window_keeps_its_mode_total(self) -> None:
        """It is missing a name, not missing spend.

        Dropping it from the mode total too would make the by-model column sum
        to less than the by-mode column with nothing saying why.
        """

        result = report(
            task_payloads=[("run_a", usage(input_tokens=100))],
            profiles={},
        )

        assert result.by_mode["task"].tokens.input_tokens == 100
        assert result.by_model == {}


class TestPricing:
    def test_a_profile_that_recorded_no_cost_is_named_rather_than_shown_free(
        self,
    ) -> None:
        result = report(
            task_payloads=[("run_a", usage(input_tokens=1_000_000))],
            profiles={"run_a": "unpriced-profile"},
        )

        assert result.by_model["unpriced-profile"].cost_micro_usd == 0
        assert result.unpriced_profiles == ("unpriced-profile",)

    def test_a_priced_profile_is_not_named(self) -> None:
        result = report(
            task_payloads=[("run_a", usage(input_tokens=100, cost_micro_usd=42))],
            profiles={"run_a": "priced-profile"},
        )

        assert result.unpriced_profiles == ()

    def test_cost_is_summed_as_recorded_and_never_recomputed(self) -> None:
        """Two runs on one profile add to what the two runs said, exactly."""

        result = report(
            task_payloads=[
                ("run_a", usage(input_tokens=10, cost_micro_usd=11)),
                ("run_b", usage(input_tokens=10, cost_micro_usd=31)),
            ],
            profiles={"run_a": "deep", "run_b": "deep"},
        )

        assert result.by_model["deep"].cost_micro_usd == 42


class TestTokenShape:
    def test_cache_reads_stay_separate_from_input(self) -> None:
        """They are a subset of the prompt, not an addition to it.

        Folding them in here would double count every cached prompt, and the
        page that renders the sum would report a bigger prompt than was sent.
        """

        result = report(
            task_payloads=[("run_a", usage(input_tokens=1_000, cache_read_tokens=800))],
        )

        bucket = result.by_mode["task"]
        assert bucket.tokens.input_tokens == 1_000
        assert bucket.tokens.cache_read_tokens == 800

    def test_a_payload_written_before_a_field_existed_reads_as_zero(self) -> None:
        """Replay must not fail on history."""

        result = report(task_payloads=[("run_a", {"usage": {"tokens": {}}})])

        assert result.by_mode["task"].tokens.output_tokens == 0
        assert result.by_mode["task"].runs == 1

    def test_a_terminal_event_with_no_usage_at_all_still_counts_as_a_run(self) -> None:
        result = report(task_payloads=[("run_a", {"stop_reason": "stop"})])

        assert result.by_mode["task"].runs == 1
        assert result.by_mode["task"].tokens.input_tokens == 0


class TestWindow:
    def test_the_window_is_echoed_back_so_a_page_can_title_itself_truthfully(
        self,
    ) -> None:
        since = datetime(2026, 8, 1, tzinfo=UTC)
        result = report(since=since)

        assert result.since == since
        assert result.until == NOW

    def test_in_flight_runs_are_a_caveat_not_a_usage_figure(self) -> None:
        result = report(
            task_payloads=[("run_a", usage(input_tokens=10))],
            runs_in_flight=3,
        )

        assert result.runs_in_flight == 3
        assert result.by_mode["task"].runs == 1
