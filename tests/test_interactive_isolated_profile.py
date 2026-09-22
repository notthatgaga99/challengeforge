from scripts.interactive_isolated_profile import (
    clients_for_rate,
    parse_float_list,
    saturation_reason,
    summarize_for_comparison,
)


def test_parse_float_list():
    assert parse_float_list("10,20,40") == [10.0, 20.0, 40.0]


def test_clients_for_rate_scales_and_bounds():
    assert clients_for_rate(10) == 8
    assert clients_for_rate(5) == 8
    assert clients_for_rate(40) == 40
    assert clients_for_rate(200) == 100


def test_saturation_reason_detects_low_achieved_and_timeouts():
    healthy = {
        "attempted": 80,
        "error_count": 0,
        "timeout_count": 0,
        "achieved_requests_per_second": 9.5,
        "client_latency_ms": {"p95_ms": 200},
        "server_total_ms": {"p95_ms": 80},
    }
    assert saturation_reason(healthy, 10.0) is None
    assert "timeout_rate" in (
        saturation_reason({**healthy, "timeout_count": 8}, 10.0) or ""
    )
    assert "achieved_rps" in (
        saturation_reason({**healthy, "achieved_requests_per_second": 3.0}, 40.0) or ""
    )
    assert "client_p95_ms" in (
        saturation_reason(
            {
                **healthy,
                "achieved_requests_per_second": 38.0,
                "client_latency_ms": {"p95_ms": 2500},
            },
            40.0,
        )
        or ""
    )


def test_summarize_for_comparison_keeps_key_fields():
    summary = summarize_for_comparison(
        {
            "offered_requests_per_second": 10,
            "achieved_requests_per_second": 9.8,
            "error_count": 0,
            "timeout_count": 0,
            "client_latency_ms": {"p50_ms": 100},
            "server_total_ms": {"p50_ms": 40},
            "pool_wait_ms": {"p50_ms": 4},
            "sql_ms": {"p50_ms": 10},
            "non_db_ms": {"p50_ms": 24},
            "client_process": {"cpu_percent": 50},
            "server_process": {"cpu_percent": 30},
            "load_generator_note": "isolated",
        }
    )
    assert summary["achieved_requests_per_second"] == 9.8
    assert summary["non_db_ms"]["p50_ms"] == 24
