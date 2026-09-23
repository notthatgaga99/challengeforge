"""Deterministic synthetic workloads for the execution-isolation prototype.

Invoked as: python -m challengeforge.execution.workloads <NAME> [args...]
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
)


def _light() -> int:
    x = 0
    for i in range(50_000):
        x ^= i * i
    print(f"light_ok checksum={x}", flush=True)
    return 0


def _cpu_heavy() -> int:
    # ~bounded CPU burn (deterministic iteration count).
    x = 0
    for i in range(3_000_000):
        x = (x + i * i) & 0xFFFFFFFF
    print(f"cpu_heavy_ok checksum={x}", flush=True)
    return 0


def _memory_heavy() -> int:
    # ~32 MiB allocation, touch pages, hold briefly.
    size = 32 * 1024 * 1024
    buf = bytearray(size)
    for i in range(0, size, 4096):
        buf[i] = i & 0xFF
    time.sleep(0.2)
    print(f"memory_heavy_ok bytes={len(buf)}", flush=True)
    return 0


def _sleep() -> int:
    time.sleep(1.0)
    print("sleep_ok", flush=True)
    return 0


def _large_output() -> int:
    chunk = b"x" * 1024
    # Write a lot quickly; executor should cap.
    for _ in range(10_000):
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
    return 0


def _child_process() -> int:
    # Spawn a child that would outlive a short parent if not tree-cleaned.
    # Use a neutral cwd so the executor workspace is not locked by the child.
    child_cwd = tempfile.gettempdir()
    if os.name == "nt":
        import subprocess

        subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=child_cwd,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,  # type: ignore[attr-defined]
        )
    else:
        pid = os.fork()
        if pid == 0:
            try:
                os.chdir(child_cwd)
            except OSError:
                pass
            time.sleep(30)
            os._exit(0)
    time.sleep(0.2)
    print(f"child_spawned parent={os.getpid()}", flush=True)
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


def _failure() -> int:
    print("failure_expected", file=sys.stderr, flush=True)
    return 7


HANDLERS = {
    "LIGHT": _light,
    "CPU_HEAVY": _cpu_heavy,
    "MEMORY_HEAVY": _memory_heavy,
    "SLEEP": _sleep,
    "LARGE_OUTPUT": _large_output,
    "CHILD_PROCESS": _child_process,
    "TIMEOUT": _timeout,
    "MANY_FILES": _many_files,
    "FAILURE": _failure,
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
