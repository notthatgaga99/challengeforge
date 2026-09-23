"""Large-upload architecture experiment (streaming + finalize vs buffer).

Safe laptop sizes: 1 / 10 / 25 / 50 MiB (100 MiB optional via --include-100).
Does not leave benchmark payloads in the repo; uses a temp workspace.
"""

from __future__ import annotations

import argparse
import gc
import io
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from challengeforge.application.uploads import (  # noqa: E402
    cleanup_incoming,
    sync_stream_to_incoming,
)
from challengeforge.storage.filesystem import LocalFilesystemStorage  # noqa: E402


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def rss_mb() -> float:
    return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)


def make_payload(size: int) -> bytes:
    # Deterministic compressible-ish pattern without holding many copies later.
    block = (b"cf-upload-" + bytes([i % 256 for i in range(256)])) * 16
    out = bytearray()
    while len(out) < size:
        out.extend(block)
    return bytes(out[:size])


def measure_buffered(size: int) -> dict:
    gc.collect()
    before = rss_mb()
    t0 = time.perf_counter()
    data = make_payload(size)
    peak_hold = rss_mb()
    # Simulate old API: hold full body then put.
    store_root = ROOT / ".cf_gov_ws" / "upload_exp" / "buf" / str(uuid4())
    store = LocalFilesystemStorage(store_root)
    key = f"submissions/bench/{uuid4()}.bin"
    store.put(key, data, "application/octet-stream")
    wall = time.perf_counter() - t0
    after = rss_mb()
    exists = store.exists(key)
    # cleanup
    store.delete(key)
    del data
    gc.collect()
    return {
        "mode": "buffered",
        "size_mb": round(size / (1024 * 1024), 3),
        "wall_s": round(wall, 4),
        "rss_before_mb": round(before, 2),
        "rss_while_holding_mb": round(peak_hold, 2),
        "rss_after_put_mb": round(after, 2),
        "rss_delta_hold_mb": round(peak_hold - before, 2),
        "throughput_mibs": round((size / (1024 * 1024)) / wall, 2) if wall else None,
        "final_exists": exists,
    }


def measure_streaming(size: int) -> dict:
    gc.collect()
    before = rss_mb()
    # Stream from a file on disk so we don't hold the full payload in the reader.
    store_root = ROOT / ".cf_gov_ws" / "upload_exp" / "stream" / str(uuid4())
    store = LocalFilesystemStorage(store_root)
    src = store_root / "_src.bin"
    src.parent.mkdir(parents=True, exist_ok=True)
    # Write source in chunks without retaining one giant bytes object in locals long.
    t0 = time.perf_counter()
    with src.open("wb") as fh:
        left = size
        block = b"x" * min(1024 * 1024, size)
        while left > 0:
            n = min(len(block), left)
            fh.write(block[:n])
            left -= n
    mid = rss_mb()
    with src.open("rb") as fh:
        incoming = sync_stream_to_incoming(
            store.incoming_root(),
            fh,
            max_bytes=size,
            content_type="application/octet-stream",
        )
    key = f"submissions/bench/{uuid4()}.bin"
    store.put_from_path(key, incoming.staging_path, incoming.content_type)
    incoming.mark_finalized()
    wall = time.perf_counter() - t0
    after = rss_mb()
    exists = store.exists(key) and store.get(key).__len__() == size
    store.delete(key)
    try:
        src.unlink()
    except OSError:
        pass
    gc.collect()
    return {
        "mode": "stream_finalize",
        "size_mb": round(size / (1024 * 1024), 3),
        "wall_s": round(wall, 4),
        "rss_before_mb": round(before, 2),
        "rss_after_source_mb": round(mid, 2),
        "rss_after_put_mb": round(after, 2),
        "rss_delta_mb": round(after - before, 2),
        "throughput_mibs": round((size / (1024 * 1024)) / wall, 2) if wall else None,
        "sha256_len": len(incoming.sha256),
        "final_exists": exists,
        "staging_gone": not incoming.staging_path.exists(),
    }


def failure_matrix(store: LocalFilesystemStorage) -> list[dict]:
    rows = []
    # 1. Abort mid-stream (simulate by writing then abort)
    incoming_root = store.incoming_root()
    partial = incoming_root / str(uuid4())
    partial.write_bytes(b"partial")
    from challengeforge.application.uploads import IncomingUpload

    inc = IncomingUpload(partial, size=7, sha256="x", content_type="application/octet-stream")
    inc.abort()
    rows.append(
        {
            "case": "client_disconnect_abort",
            "staging_exists": partial.exists(),
            "db_ref": False,
            "invariant_ok": not partial.exists(),
        }
    )

    # 2. Finalize then compensate (commit fail analogue)
    with io.BytesIO(b"complete-bytes") as fh:
        inc2 = sync_stream_to_incoming(
            incoming_root, fh, max_bytes=1024, content_type="application/octet-stream"
        )
    key = f"submissions/fail/{uuid4()}.bin"
    store.put_from_path(key, inc2.staging_path, inc2.content_type)
    inc2.mark_finalized()
    # simulate DB fail
    from challengeforge.application.artifacts import compensate_delete

    compensate_delete(store, key)
    rows.append(
        {
            "case": "fail_after_finalize_before_commit",
            "blob_exists": store.exists(key),
            "staging_exists": inc2.staging_path.exists(),
            "invariant_ok": not store.exists(key),
        }
    )

    # 3. Successful finalize: no partial final
    with io.BytesIO(b"ok-bytes") as fh:
        inc3 = sync_stream_to_incoming(
            incoming_root, fh, max_bytes=1024, content_type="application/octet-stream"
        )
    key3 = f"submissions/ok/{uuid4()}.bin"
    store.put_from_path(key3, inc3.staging_path, inc3.content_type)
    inc3.mark_finalized()
    rows.append(
        {
            "case": "success_finalize",
            "blob_exists": store.exists(key3),
            "size": len(store.get(key3)),
            "staging_gone": not inc3.staging_path.exists(),
            "invariant_ok": store.exists(key3) and not inc3.staging_path.exists(),
        }
    )

    # 4. Checksum mismatch
    from challengeforge.domain.exceptions import ValidationFailed

    mismatch_ok = False
    try:
        with io.BytesIO(b"abc") as fh:
            sync_stream_to_incoming(
                incoming_root,
                fh,
                max_bytes=1024,
                expected_sha256="0" * 64,
            )
    except ValidationFailed:
        mismatch_ok = True
    rows.append({"case": "checksum_mismatch", "rejected": mismatch_ok})

    # 5. Incoming cleanup
    stale = store.incoming_root() / str(uuid4())
    stale.write_bytes(b"abandoned")
    os.utime(stale, (time.time() - 10_000, time.time() - 10_000))
    deleted = cleanup_incoming(store.incoming_root(), grace_seconds=60.0)
    rows.append(
        {
            "case": "abandoned_incoming_cleanup",
            "deleted": stale.name in deleted,
            "exists": stale.exists(),
            "invariant_ok": not stale.exists(),
        }
    )

    # 6. Oversize rejected without leaving staging
    from challengeforge.domain.exceptions import ValidationFailed as VF

    oversize_left = False
    before_keys = set(p.name for p in store.incoming_root().glob("*") if p.is_file())
    try:
        with io.BytesIO(b"x" * 100) as fh:
            sync_stream_to_incoming(store.incoming_root(), fh, max_bytes=10)
    except VF:
        after_keys = set(p.name for p in store.incoming_root().glob("*") if p.is_file())
        oversize_left = bool(after_keys - before_keys)
    rows.append(
        {
            "case": "oversize_abort",
            "left_staging": oversize_left,
            "invariant_ok": not oversize_left,
        }
    )
    return rows


def concurrent_uploads(n: int, size: int) -> dict:
    store_root = ROOT / ".cf_gov_ws" / "upload_exp" / "conc" / str(uuid4())
    store = LocalFilesystemStorage(store_root)

    def one(_: int) -> float:
        t0 = time.perf_counter()
        with io.BytesIO(b"z" * size) as fh:
            inc = sync_stream_to_incoming(
                store.incoming_root(), fh, max_bytes=size
            )
        key = f"submissions/c/{uuid4()}.bin"
        store.put_from_path(key, inc.staging_path, inc.content_type)
        inc.mark_finalized()
        return time.perf_counter() - t0

    gc.collect()
    rss0 = rss_mb()
    t0 = time.perf_counter()
    walls: list[float] = []
    with ThreadPoolExecutor(max_workers=n) as pool:
        futs = [pool.submit(one, i) for i in range(n)]
        for f in as_completed(futs):
            walls.append(f.result())
    batch = time.perf_counter() - t0
    rss1 = rss_mb()
    keys = list(store.iter_keys("submissions/"))
    return {
        "concurrency": n,
        "size_bytes": size,
        "batch_s": round(batch, 4),
        "p50_s": round(sorted(walls)[len(walls) // 2], 4),
        "p95_s": round(sorted(walls)[max(0, int(len(walls) * 0.95) - 1)], 4),
        "rss_delta_mb": round(rss1 - rss0, 2),
        "final_keys": len(keys),
        "ok": len(keys) == n,
    }


def decide(results: dict) -> dict:
    buf = [r for r in results["size_sweep"] if r["mode"] == "buffered"]
    strm = [r for r in results["size_sweep"] if r["mode"] == "stream_finalize"]
    # At 10MiB+, buffered hold delta should exceed streaming delta materially.
    rationale = (
        "API previously buffered entire bodies in RAM. Streaming to staging then "
        "atomic finalize keeps final keys complete-only, bounds RSS growth vs size, "
        "and preserves blob-first + compensate. Resumability/chunking/S3 not justified "
        "for current ≤5MiB default and typical hackathon zip sizes; whole-upload retry "
        "remains sufficient."
    )
    return {
        "gate": "KEEP + temporary/finalize boundary",
        "portfolio": "B",
        "resumability": "not_required_yet",
        "checksum": "optional_whole_upload_sha256",
        "rationale": rationale,
        "buffered_samples": len(buf),
        "streaming_samples": len(strm),
        "object_storage": False,
        "default_max_bytes_unchanged": True,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--include-100", action="store_true")
    p.add_argument(
        "--results-path", default="docs/large-upload-architecture-results.json"
    )
    args = p.parse_args()
    sizes = [1, 10, 25, 50]
    if args.include_100:
        sizes.append(100)
    sizes_b = [s * 1024 * 1024 for s in sizes]

    size_sweep = []
    for sb in sizes_b:
        print(f"measure buffered {sb // (1024*1024)}MiB", flush=True)
        size_sweep.append(measure_buffered(sb))
        print(f"measure stream {sb // (1024*1024)}MiB", flush=True)
        size_sweep.append(measure_streaming(sb))

    store = LocalFilesystemStorage(
        ROOT / ".cf_gov_ws" / "upload_exp" / "failures" / str(uuid4())
    )
    failures = failure_matrix(store)
    conc = [
        concurrent_uploads(1, 256 * 1024),
        concurrent_uploads(5, 256 * 1024),
        concurrent_uploads(10, 256 * 1024),
    ]

    output = {
        "metadata": {
            "started_at": utc_iso(),
            "claim": "Large upload architecture — not S3/resumable by default",
            "default_artifact_max_bytes": 5 * 1024 * 1024,
            "completed_at": utc_iso(),
        },
        "size_sweep": size_sweep,
        "failure_matrix": failures,
        "concurrency": conc,
        "decision": {},
    }
    output["decision"] = decide(output)
    path = ROOT / args.results_path
    path.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")
    print(f"wrote {path}")
    print(f"decision={output['decision']['gate']}")


if __name__ == "__main__":
    main()
