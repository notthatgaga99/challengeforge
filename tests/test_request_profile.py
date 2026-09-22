from challengeforge.request_profile import RequestProfile, timed_stage


def test_request_profile_exclusive_pool_and_sql_accounting():
    profile = RequestProfile()
    profile.pool_wait_ms = 2.5
    profile.mark_sql_start("SELECT id FROM users WHERE id = $1")
    profile.mark_sql_end(rowcount=1)
    with timed_stage("identity"):
        pass
    # Force identity wall without relying on current context.
    profile.identity_ms = 1.0
    summary = profile.summary()
    assert summary["pool_wait_ms"] == 2.5
    assert summary["query_count"] == 1
    assert "identity" in summary["sql_by_category"]
    assert summary["sql_ms"] >= 0.0
    assert "non_db_ms" in summary
