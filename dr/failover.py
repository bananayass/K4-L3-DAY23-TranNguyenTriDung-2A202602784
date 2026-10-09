"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import datetime
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """Append one timestamped JSONL event and mirror it to stdout."""
    now = time.time()
    event = {
        "ts": now,
        "iso": datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat(),
        **kw,
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(event, ensure_ascii=False) + "\n")
    print(json.dumps(event, ensure_ascii=False), flush=True)
    return event


def state_of(region: str) -> dict:
    response = httpx.get(f"{URL[region]}/v1/state", timeout=2.0)
    response.raise_for_status()
    return response.json()


def failover(target: str, backend: str, wait: float) -> dict:
    """Restore and warm the target before atomically changing edge routing."""
    if target not in URL:
        raise ValueError(f"unknown target region: {target}")
    if wait <= 0:
        raise ValueError("wait must be positive")

    primary = "b" if target == "a" else "a"
    try:
        try:
            before = state_of(target)
            emit(step="1_verify_target", target=target, state=before)
        except (httpx.HTTPError, ValueError) as exc:
            # A failed inspection must not prevent restore; readiness remains the
            # authoritative gate that protects the later DNS cutover.
            before = None
            emit(step="1_verify_target", target=target, state=None,
                 error=type(exc).__name__, reason=str(exc))

        restored = snapshot.get(target, backend)
        rpo = snapshot.rpo(
            pathlib.Path(f"state/region-{primary}/vectors.sqlite"),
            pathlib.Path(f"state/region-{target}/vectors.sqlite"),
        )
        emit(
            step="2_restore_snapshot",
            target=target,
            backend=backend,
            rpo_seconds=rpo["rpo_seconds"],
            docs_lost=rpo["docs_lost"],
            embed_model_version=restored.get("embed_model_version"),
            snapshot_at=restored.get("snapshot_at"),
        )

        pool_file = pathlib.Path(f"state/region-{target}/pool_state")
        pool_file.parent.mkdir(parents=True, exist_ok=True)
        pool_file.write_text("full\n", encoding="utf-8")
        emit(step="3_scale_pool", target=target, pool_state="full")

        started = time.monotonic()
        deadline = started + wait
        last_reason = "not_probed"
        ready_state = None
        while time.monotonic() < deadline:
            try:
                response = httpx.get(f"{URL[target]}/readyz", timeout=min(2.0, wait))
                ready_state = response.json()
                if response.status_code == 200 and ready_state.get("ready", True):
                    waited = round(time.monotonic() - started, 2)
                    emit(step="4_wait_ready", target=target, ready=True, waited_s=waited,
                         state=ready_state)
                    break
                last_reason = ",".join(ready_state.get("reasons", [])) or f"http_{response.status_code}"
            except (httpx.HTTPError, ValueError) as exc:
                last_reason = type(exc).__name__
            time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
        else:
            waited = round(time.monotonic() - started, 2)
            emit(step="4_wait_ready", target=target, ready=False, waited_s=waited,
                 reason=last_reason)
            return {"ok": False, "target": target, "reason": "target_not_ready",
                    "waited_s": waited, "rpo": rpo}

        active_file = pathlib.Path("edge/active_region")
        active_file.parent.mkdir(parents=True, exist_ok=True)
        active_file.write_text(target + "\n", encoding="utf-8")
        emit(step="5_dns_cutover", target=target, active_region=target)
        return {"ok": True, "target": target, "state": ready_state, "rpo": rpo,
                "embed_model_version": restored.get("embed_model_version"),
                "waited_s": waited, "active_region": target}
    except (httpx.HTTPError, OSError, SystemExit, ValueError) as exc:
        emit(step="abort", target=target, ok=False, reason=str(exc), error=type(exc).__name__)
        return {"ok": False, "target": target, "reason": str(exc), "error": type(exc).__name__}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
