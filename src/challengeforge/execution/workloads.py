"""Deterministic synthetic workloads for the execution plane.

Invoked as: python workloads.py <NAME>
Never used for participant uploads.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path


WORKLOAD_NAMES = (
    "LIGHT",
    "CPU_HEAVY",
    "MEMORY_HEAVY",
    "SLEEP",
    "LARGE_OUTPUT",
    "CHILD_PROCESS",
    "TIMEOUT",
    "MANY_FILES",
    "FAILURE",
    "PROCESS_HEAVY",
    "DISK_HEAVY",
    "MEMORY_GROW",
    "BOUNDARY_PROBE",
)


def _light() -> int:
    x = 0
    for i in range(50_000):
        x ^= i * i
    print(f"light_ok checksum={x}", flush=True)
    return 0


def _cpu_heavy() -> int:
    x = 0
    for i in range(3_000_000):
        x = (x + i * i) & 0xFFFFFFFF
    print(f"cpu_heavy_ok checksum={x}", flush=True)
    return 0


def _memory_heavy() -> int:
    size = 32 * 1024 * 1024
    buf = bytearray(size)
    for i in range(0, size, 4096):
        buf[i] = i & 0xFF
    time.sleep(0.2)
    print(f"memory_heavy_ok bytes={len(buf)}", flush=True)
    return 0


def _memory_grow() -> int:
    """Step allocations (~4 MiB) until host/job stops us — keep steps small."""
    chunks: list[bytearray] = []
    step = 4 * 1024 * 1024
    try:
        for n in range(64):  # hard stop at ~256 MiB locally
            buf = bytearray(step)
            for i in range(0, step, 4096):
                buf[i] = (n + i) & 0xFF
            chunks.append(buf)
            print(f"memory_grow step={n} total_mb={(n + 1) * 4}", flush=True)
            time.sleep(0.05)
    except MemoryError:
        print("cf_memory_limit", flush=True)
        return 76
    print(f"memory_grow_done chunks={len(chunks)}", flush=True)
    return 0


def _sleep() -> int:
    time.sleep(1.0)
    print("sleep_ok", flush=True)
    return 0


def _large_output() -> int:
    chunk = b"x" * 1024
    for _ in range(10_000):
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
    return 0


def _child_process() -> int:
    child_cwd = tempfile.gettempdir()
    if os.name == "nt":
        import subprocess

        subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(8)"],
            cwd=child_cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
    else:
        pid = os.fork()
        if pid == 0:
            try:
                os.chdir(child_cwd)
            except OSError:
                pass
            time.sleep(8)
            os._exit(0)
    time.sleep(0.2)
    print(f"child_spawned parent={os.getpid()}", flush=True)
    return 0


def _process_heavy() -> int:
    """Attempt many short-lived children; report quota failures explicitly."""
    import subprocess

    target = int(os.environ.get("CF_PROCESS_HEAVY_N", "20"))
    spawned = 0
    children: list[subprocess.Popen[bytes]] = []
    for i in range(target):
        try:
            p = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(2)"],
                cwd=tempfile.gettempdir(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
            children.append(p)
            spawned += 1
            print(f"spawned {i} pid={p.pid}", flush=True)
            time.sleep(0.05)
        except OSError as exc:
            win = getattr(exc, "winerror", None)
            print(
                f"cf_process_limit spawned={spawned} winerror={win} err={exc}",
                flush=True,
            )
            time.sleep(0.3)
            return 75
    time.sleep(0.4)
    print(f"process_heavy_ok spawned={spawned}", flush=True)
    return 0


def _timeout() -> int:
    while True:
        time.sleep(1.0)


def _many_files() -> int:
    root = Path(os.environ.get("CF_WORKSPACE", "."))
    d = root / "many"
    d.mkdir(parents=True, exist_ok=True)
    for i in range(50):
        (d / f"f{i}.txt").write_text("x" * 64, encoding="utf-8")
    print(f"many_files_ok n=50 dir={d}", flush=True)
    return 0


def _disk_heavy() -> int:
    """Write progressively larger files under CF_WORKSPACE."""
    root = Path(os.environ.get("CF_WORKSPACE", "."))
    d = root / "disk"
    d.mkdir(parents=True, exist_ok=True)
    chunk = b"y" * (64 * 1024)
    target_mb = int(os.environ.get("CF_DISK_HEAVY_MB", "8"))
    written = 0
    n = 0
    while written < target_mb * 1024 * 1024:
        path = d / f"blob_{n}.bin"
        with path.open("wb") as fh:
            for _ in range(16):  # 1 MiB per file
                fh.write(chunk)
                written += len(chunk)
        n += 1
        print(f"disk_heavy written_mb={written // (1024 * 1024)} files={n}", flush=True)
        time.sleep(0.05)
    print(f"disk_heavy_ok bytes={written}", flush=True)
    return 0


def _failure() -> int:
    print("failure_expected", file=sys.stderr, flush=True)
    return 7


def _boundary_probe() -> int:
    """Harmless probes of FS / env / network reachability (no exploits)."""
    import json
    import socket

    report: dict[str, object] = {
        "cwd": os.getcwd(),
        "pid": os.getpid(),
        "env_key_count": len(os.environ),
        "has_database_url": "DATABASE_URL" in os.environ,
        "has_aws_secret": "AWS_SECRET_ACCESS_KEY" in os.environ,
        "has_pythonpath": "PYTHONPATH" in os.environ,
        "pythonpath_set": bool(os.environ.get("PYTHONPATH")),
        "workspace": os.environ.get("CF_WORKSPACE"),
        "attempt_id_set": bool(os.environ.get("CF_EXECUTION_ATTEMPT_ID")),
    }

    forbidden = os.environ.get("CF_FORBIDDEN_PROBE")
    report["forbidden_probe_configured"] = bool(forbidden)
    report["forbidden_readable"] = False
    report["forbidden_error"] = None
    if forbidden:
        try:
            with open(forbidden, encoding="utf-8") as fh:
                data = fh.read(64)
            report["forbidden_readable"] = True
            report["forbidden_len"] = len(data)
        except OSError as exc:
            report["forbidden_error"] = f"{type(exc).__name__}:{exc.errno}"

    ws = Path(os.environ.get("CF_WORKSPACE") or ".")
    parent = ws.parent
    report["parent_listable"] = False
    try:
        names = os.listdir(parent)
        report["parent_listable"] = True
        report["parent_entry_count"] = len(names)
    except OSError as exc:
        report["parent_list_error"] = f"{type(exc).__name__}"

    # Loopback TCP — connection refused still proves the network stack is usable.
    report["loopback_socket_attempted"] = True
    report["loopback_reachable_stack"] = False
    try:
        with socket.create_connection(("127.0.0.1", 9), timeout=0.5):
            report["loopback_reachable_stack"] = True
            report["loopback_connected"] = True
    except ConnectionRefusedError:
        report["loopback_reachable_stack"] = True
        report["loopback_connected"] = False
    except OSError as exc:
        report["loopback_error"] = f"{type(exc).__name__}"

    # DNS resolve — proves outbound name resolution exists (no HTTP fetch).
    report["dns_resolve_attempted"] = True
    report["dns_resolved"] = False
    try:
        socket.getaddrinfo("example.com", 80, type=socket.SOCK_STREAM)
        report["dns_resolved"] = True
    except OSError as exc:
        report["dns_error"] = f"{type(exc).__name__}"

    report["network_stack_usable"] = bool(
        report["loopback_reachable_stack"] or report["dns_resolved"]
    )
    # Platform package import via PYTHONPATH (trusted-path leakage signal).
    report["can_import_challengeforge"] = False
    try:
        import challengeforge  # noqa: F401

        report["can_import_challengeforge"] = True
    except Exception as exc:  # noqa: BLE001 — probe must never crash hard
        report["import_error"] = type(exc).__name__

    print("cf_boundary_probe " + json.dumps(report, default=str), flush=True)
    return 0


HANDLERS = {
    "LIGHT": _light,
    "CPU_HEAVY": _cpu_heavy,
    "MEMORY_HEAVY": _memory_heavy,
    "MEMORY_GROW": _memory_grow,
    "SLEEP": _sleep,
    "LARGE_OUTPUT": _large_output,
    "CHILD_PROCESS": _child_process,
    "PROCESS_HEAVY": _process_heavy,
    "TIMEOUT": _timeout,
    "MANY_FILES": _many_files,
    "DISK_HEAVY": _disk_heavy,
    "FAILURE": _failure,
    "BOUNDARY_PROBE": _boundary_probe,
}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in HANDLERS:
        print(
            f"usage: workloads NAME; known={','.join(WORKLOAD_NAMES)}",
            file=sys.stderr,
        )
        return 2
    return int(HANDLERS[args[0]]())


if __name__ == "__main__":
    raise SystemExit(main())
