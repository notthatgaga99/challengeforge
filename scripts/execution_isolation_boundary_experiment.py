"""Safe isolation-boundary probes (no exploits, no real secret theft).

Demonstrates what the *current* subprocess + Job Object plane can and cannot
prevent. Decision documentation lives in docs/execution-isolation-boundary.md.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from challengeforge.execution.execution_environment import (  # noqa: E402
    FORBIDDEN_SECRET_NAMES,
    assert_no_forbidden_secrets,
    build_execution_env,
    env_contract_summary,
)
from challengeforge.execution.limits import ExecutionLimits  # noqa: E402
from challengeforge.execution.process_executor import ProcessExecutor  # noqa: E402


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_probe_stdout(preview: str) -> dict:
    for line in preview.splitlines():
        if line.startswith("cf_boundary_probe "):
            return json.loads(line[len("cf_boundary_probe ") :])
    return {}


def run_boundary_probe(ws: Path) -> dict:
    ws.mkdir(parents=True, exist_ok=True)
    # Forbidden marker *outside* the execution workspace.
    with tempfile.NamedTemporaryFile(
        "w",
        prefix="cf-forbidden-",
        suffix=".txt",
        delete=False,
        encoding="utf-8",
    ) as fh:
        fh.write("FORBIDDEN_MARKER_CONTENT")
        forbidden = fh.name

    host = dict(os.environ)
    host["DATABASE_URL"] = "postgresql://should-not-leak"
    host["AWS_SECRET_ACCESS_KEY"] = "should-not-leak"

    ex = ProcessExecutor(
        limits=ExecutionLimits(wall_timeout_seconds=8.0, max_stdout_bytes=64 * 1024),
        workspace_root=ws,
        extra_env={"CF_FORBIDDEN_PROBE": forbidden},
        include_platform_pythonpath=True,
    )
    # Inject hostile host env into build path via monkeypatch of os.environ
    # already present — build_execution_env reads os.environ by default.
    # Ensure secrets are in os.environ for this process during the run:
    old_db = os.environ.get("DATABASE_URL")
    old_aws = os.environ.get("AWS_SECRET_ACCESS_KEY")
    os.environ["DATABASE_URL"] = host["DATABASE_URL"]
    os.environ["AWS_SECRET_ACCESS_KEY"] = host["AWS_SECRET_ACCESS_KEY"]
    try:
        result = ex.run("BOUNDARY_PROBE")
    finally:
        if old_db is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old_db
        if old_aws is None:
            os.environ.pop("AWS_SECRET_ACCESS_KEY", None)
        else:
            os.environ["AWS_SECRET_ACCESS_KEY"] = old_aws

    probe = parse_probe_stdout(result.evidence.get("stdout_preview") or "")
    # Also parse full stdout from evidence if truncated oddly
    if not probe and result.evidence.get("stdout_preview"):
        probe = parse_probe_stdout(str(result.evidence.get("stdout_preview")))

    child_env = build_execution_env(
        ws / "dummy",
        host_environ=host,
        extra={"CF_FORBIDDEN_PROBE": forbidden},
        include_platform_pythonpath=True,
    )

    try:
        Path(forbidden).unlink(missing_ok=True)  # type: ignore[arg-type]
    except OSError:
        pass

    return {
        "executor_status": result.status,
        "leaked_pids": result.leaked_pids_after_cleanup,
        "workspace_cleaned": result.workspace_cleaned,
        "child_env_contract": env_contract_summary(child_env),
        "child_env_forbidden": assert_no_forbidden_secrets(child_env),
        "probe": probe,
        "gaps_observed": {
            "secrets_not_in_child_env": not probe.get("has_database_url")
            and not probe.get("has_aws_secret"),
            "forbidden_file_readable": bool(probe.get("forbidden_readable")),
            "parent_dir_listable": bool(probe.get("parent_listable")),
            "loopback_network_stack_usable": bool(
                probe.get("loopback_reachable_stack")
            ),
            "dns_resolvable": bool(probe.get("dns_resolved")),
            "network_stack_usable": bool(probe.get("network_stack_usable")),
            "platform_importable_via_pythonpath": bool(
                probe.get("can_import_challengeforge")
            ),
        },
    }


def decide(probe_result: dict) -> dict:
    gaps = probe_result.get("gaps_observed") or {}
    # Expected under current architecture:
    # - secrets scrubbed (good)
    # - forbidden file readable (bad for hostile)
    # - parent listable (bad)
    # - network usable (bad)
    # - platform importable (bad for hostile; ok for trusted)
    return {
        "gate": "Keep subprocess + Job Object for trusted/internal execution only",
        "portfolio": "A",
        "hostile_code_prerequisite": "B_container_minimum",
        "rationale": (
            "Allowlisted env prevents accidental secret inheritance in the tested "
            "envelope, but FS/net/host boundaries remain open: forbidden files, "
            "parent dirs, loopback/DNS, and platform PYTHONPATH are reachable. "
            "Resource governance ≠ sandbox isolation. Do not enable participant "
            "code until a stronger execution boundary (containers on Linux, or "
            "stronger) exists."
        ),
        "participant_code_allowed": False,
        "public_sandbox_ready": False,
        "gaps_confirm_hostile_unsafe": True,
        "observed": gaps,
        "forbidden_secret_names_checked": sorted(FORBIDDEN_SECRET_NAMES),
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--results-path",
        default="docs/execution-isolation-boundary-results.json",
    )
    args = p.parse_args()
    ws = ROOT / ".cf_gov_ws" / "boundary"
    output = {
        "metadata": {
            "started_at": utc_iso(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "claim": "Boundary probes — not a sandbox proof",
        },
        "probe": run_boundary_probe(ws),
    }
    output["metadata"]["completed_at"] = utc_iso()
    output["decision"] = decide(output["probe"])
    path = ROOT / args.results_path
    path.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")
    print(f"wrote {path}")
    print(f"decision={output['decision']['gate']}")
    print(f"gaps={json.dumps(output['decision']['observed'])}")


if __name__ == "__main__":
    main()
