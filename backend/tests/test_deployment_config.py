"""
Audit P0 #1 regression guard: the engine container must run exactly one
uvicorn worker.

Running --workers 2 (or more) spawns multiple independent EngineLoop /
MasterTradeDetector / WebSocket / CopyEnginePipeline instances against the
same master broker account. Order creation and fills are protected by
EventValidator's Redis-backed dedup, but handle_master_modification() and
handle_master_cancellation() are dispatched directly from
execute_replication() and never call EventValidator.validate() - they have
no cross-process dedup at all. With more than one worker, a single master
modification or cancellation can be independently detected and submitted to
the broker twice (once per worker).

This test does not start Docker or uvicorn - it only guards the checked-in
Dockerfile CMD against a future regression back to multiple workers.
"""
import re
from pathlib import Path


def test_engine_dockerfile_runs_exactly_one_uvicorn_worker():
    dockerfile_path = Path(__file__).resolve().parents[1] / "Dockerfile"
    content = dockerfile_path.read_text(encoding="utf-8")

    match = re.search(r'"--workers",\s*"(\d+)"', content)
    assert match is not None, "backend/Dockerfile CMD must specify --workers explicitly"
    assert match.group(1) == "1", (
        "backend/Dockerfile must run exactly one uvicorn worker - more than one spawns "
        "independent EngineLoop instances against the same master account with no "
        "cross-process dedup on master order modification/cancellation."
    )
