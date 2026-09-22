import pytest

from scripts.interactive_capacity_experiment import (
    InteractiveObservation,
    parse_evaluation_mix,
    parse_mix,
    summarize_interactive,
)


def test_interactive_mix_must_be_complete_and_total_100():
    parsed = parse_mix(
        "challenge_reads=60,evaluation_status=15,participant_history=10,"
        "submission_creation=10,organizer_operations=5"
    )
    assert sum(parsed.values()) == 100
    with pytest.raises(ValueError):
        parse_mix("challenge_reads=100")


def test_evaluation_mix_is_normalized():
    parsed = parse_evaluation_mix("light=50,medium=25,heavy=25")
    assert parsed == {"light": 0.5, "medium": 0.25, "heavy": 0.25}


def test_interactive_summary_distinguishes_errors_and_timeouts():
    observations = [
        InteractiveObservation("challenge_reads", 200, 10.0),
        InteractiveObservation("challenge_reads", 500, 20.0),
        InteractiveObservation(
            "evaluation_status",
            None,
            30.0,
            timed_out=True,
            error="ReadTimeout",
        ),
    ]
    result = summarize_interactive(observations, elapsed=1.0)
    assert result["successful"] == 1
    assert result["errors"] == 2
    assert result["timeouts"] == 1
    assert result["achieved_requests_per_second"] == 3.0
