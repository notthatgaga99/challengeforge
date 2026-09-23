# ADR 0007: Interactive p95 closed-loop feedback (experimental)

## Status

Accepted as **experimental / opt-in**. Product defaults keep thresholds at **0**
(disabled). Hint publication defaults to **async** (off the interactive request path).

## Context

Hard-mix pressure (see `docs/hard-mix-pressure-results.md`) showed resource-aware
runtime protects CPU/memory/queue bounds but did not explicitly protect interactive
latency. Thresholds `resource_interactive_p95_warn_ms` /
`resource_interactive_p95_critical_ms` existed but were disabled.

A closed-loop A/B/C portfolio then showed the mechanism works but did **not**
improve mean client p95 vs feedback-off; the important failure was client
saturation while server wall p95 stayed below the warn threshold (missed
intervention). Sync persist-on-request was also a contamination risk.

## Decision

1. Measure **server wall time** on designated interactive HTTP paths (candidate A).
2. Also record **pool wait** and **in-flight** for correlation (candidates B/D);
   do **not** promote them to controller inputs without evidence.
3. Publish rolling hints via `evaluation_scheduler_state` (migrations `0008`,
   `0009` for `pool_wait_p95_ms`) using an **async background publisher** by
   default (`resource_interactive_publish_mode=async`). Sync mode remains for
   experiments only.
4. Workers apply a **hysteresis** feedback controller that raises pressure floor
   (WARN→PRESSURED, CRITICAL→DEGRADED / max_heavy=0; optional hold-all).
5. Never cancel in-flight work; never reject submissions.
6. On publish failure: keep last good hint; do not invent HEALTHY from missing data.
7. Keep product feedback thresholds **off** until evidence shows interactive benefit.
8. Do **not** introduce Redis/Kafka/LLM because the signal is imperfect.

## Alternatives

1. Do nothing — leave interactive unprotected beyond CPU/mem  
2. Enable raw threshold without hysteresis — flap risk  
3. Keep sync persist on every cadence — hot-path contamination  
4. cgroups / process priority / Redis — infra without proving the signal works  
5. Kill running HEAVY on CRITICAL — destructive, rejected  
6. Immediately switch controller to pool-wait — premature without correlation win  

## Evidence

- Design + results: `docs/interactive-feedback-control.md`
- Portfolio A/B/C: mode A had **better** mean client p95 than B/C on the laptop run;
  correctness 100%; stranded 0; missed intervention documented
- Hot-path / correlation: `docs/interactive-hotpath-results.json`
- Unit tests: `tests/test_interactive_feedback.py`, `tests/test_interactive_publisher.py`

## Trade-offs

**+** Inspectable signal; opt-in; async publish; reuses existing pressure/admission  
**−** Server wall may not match client saturation; async adds bounded publish delay;
host noise; experimental complexity

## Reconsideration

Promote toward product only if:

- a server-side signal reliably tracks client p95 under hard eval, **and**
- feedback mode beats feedback-off on hard cells without correctness/durability
  regression, **and**
- async publication cost is shown not to re-introduce hot-path harm.

Otherwise keep **EXPERIMENTAL ONLY** or **DROP** — both are successful outcomes if
they eliminate a bad architectural direction.

## Related

ADR 0005 (resource-aware runtime), hard-mix pressure experiment,
`scripts/interactive_hotpath_experiment.py`
