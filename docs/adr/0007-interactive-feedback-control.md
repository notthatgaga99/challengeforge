# ADR 0007: Interactive p95 closed-loop feedback (experimental)

## Status

Accepted as **experimental / opt-in**. Product defaults keep thresholds at **0** (disabled).

## Context

Hard-mix pressure (see `docs/hard-mix-pressure-results.md`) showed resource-aware
runtime protects CPU/memory/queue bounds but did not explicitly protect interactive
latency. Thresholds `resource_interactive_p95_warn_ms` /
`resource_interactive_p95_critical_ms` existed but were disabled.

## Decision

1. Measure **server wall time** on designated interactive HTTP paths.
2. Publish rolling p95 via `evaluation_scheduler_state` (migration `0008`).
3. Workers apply a **hysteresis** feedback controller that raises pressure floor
   (WARN→PRESSURED, CRITICAL→DEGRADED / max_heavy=0; optional hold-all).
4. Never cancel in-flight work; never reject submissions.
5. Keep product defaults **off** until evidence shows interactive benefit.

## Alternatives

1. Do nothing — leave interactive unprotected beyond CPU/mem  
2. Enable raw threshold without hysteresis — flap risk  
3. cgroups / process priority / Redis — infra without proving the signal works  
4. Kill running HEAVY on CRITICAL — destructive, rejected  

## Evidence

- Design + results: `docs/interactive-feedback-control.md`
- Portfolio A/B/C: mode A had **better** mean client p95 than B/C on this laptop run;
  correctness 100%; stranded 0
- Unit tests: `tests/test_interactive_feedback.py`

## Trade-offs

**+** Inspectable signal; opt-in; reuses existing pressure/admission  
**−** Server wall may not match client saturation; persist-on-path cost; noisy host

## Reconsideration

Promote toward product only if a re-run shows B protecting interactive better than A
when A actually saturates, without correctness/durability regression.

## Related

ADR 0005 (resource-aware runtime), hard-mix pressure experiment
