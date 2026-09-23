from challengeforge.domain.enums import IngestionStatus

ALLOWED_TRANSITIONS: dict[IngestionStatus, frozenset[IngestionStatus]] = {
    IngestionStatus.QUEUED: frozenset({IngestionStatus.RUNNING}),
    IngestionStatus.RUNNING: frozenset(
        {
            IngestionStatus.SUCCEEDED,
            IngestionStatus.FAILED,
            IngestionStatus.QUEUED,  # stale / transient requeue
        }
    ),
    IngestionStatus.SUCCEEDED: frozenset(),
    IngestionStatus.FAILED: frozenset(),
}


def assert_transition(
    current: IngestionStatus, target: IngestionStatus
) -> IngestionStatus:
    allowed = ALLOWED_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise ValueError(f"Illegal ingestion transition {current} → {target}")
    return target
