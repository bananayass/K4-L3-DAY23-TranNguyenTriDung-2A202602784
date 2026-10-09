"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import datetime
import json
import pathlib
import statistics
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Append one timestamped runbook checkpoint."""
    now = time.time()
    event = {"ts": now,
             "iso": datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat(),
             "step": n, "name": name, **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(event, ensure_ascii=False) + "\n")
    print(json.dumps(event, ensure_ascii=False), flush=True)
    return event


def confirm(auto: bool, msg: str) -> bool:
    """Require an explicit operator acknowledgement unless automation was requested."""
    if auto:
        return True
    try:
        return input(f"{msg} [y/N] ").strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def _probe(region: str, attempts: int = 3, interval: float = 1.5) -> dict:
    observations = []
    for attempt in range(attempts):
        try:
            response = httpx.get(f"{URL[region]}/readyz", timeout=2.0)
            body = response.json()
            observations.append({"ready": response.status_code == 200 and body.get("ready", True),
                                 "status": response.status_code, "reasons": body.get("reasons", [])})
        except (httpx.HTTPError, ValueError) as exc:
            observations.append({"ready": False, "error": type(exc).__name__})
        if attempt + 1 < attempts:
            # Space confirmations out so a transient failure cannot race the
            # independent health-check threshold and trigger an early cutover.
            time.sleep(interval)
    # A region is considered down only when every confirmation failed. One
    # successful probe breaks the failure streak and prevents a transient from
    # authorizing failover.
    return {"region": region, "ready": any(item["ready"] for item in observations),
            "observations": observations}


def _latest_outage(primary: str):
    path = pathlib.Path("chaos/chaos-events.jsonl")
    if not path.exists():
        return None
    events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    kills = [event for event in events if event.get("action") == "kill" and event.get("region") == primary]
    return kills[-1] if kills else None


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """Execute the seven-step, operator-gated region failover runbook."""
    started = time.time()
    primary_state = _probe(primary)
    target_state = _probe(target)
    outage_confirmed = not primary_state["ready"]
    step(1, "xac_nhan_outage", primary=primary_state, target=target_state,
         outage_confirmed=outage_confirmed)
    if not outage_confirmed:
        return {"ok": False, "reason": "primary_not_confirmed_down"}
    if not confirm(auto, f"Region {primary} is unavailable. Fail over to region {target}?"):
        step(2, "thong_bao_incident", confirmed=False, reason="operator_declined")
        return {"ok": False, "reason": "operator_declined"}

    outage = _latest_outage(primary)
    incident = step(2, "thong_bao_incident", confirmed=True,
                    outage_ts=outage.get("ts") if outage else None,
                    outage_iso=outage.get("iso") if outage else None,
                    notification_delay_s=None if not outage else round(time.time() - outage["ts"], 2))

    result = fo.failover(target, backend, wait=60.0)
    step(3, "scale_gpu_pool", failover_called_once=True, ok=result.get("ok"),
         waited_s=result.get("waited_s"), reason=result.get("reason"))
    if not result.get("ok"):
        step(7, "post_incident", ok=False, elapsed_s=round(time.time() - started, 2),
             reason=result.get("reason"), measure_command=(
                 "python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300"))
        return result

    state = result.get("state", {})
    step(4, "verify_state_replica", vector_count=state.get("vectors", {}).get("count"),
         weights=not any("model_weights_missing" in reason for reason in state.get("reasons", [])),
         embed_model_version=result.get("embed_model_version"), rpo=result.get("rpo"))
    step(5, "dns_cutover", ok=result.get("active_region") == target,
         active_region=result.get("active_region"))

    latencies = []
    errors = 0
    for _ in range(10):
        request_started = time.monotonic()
        try:
            response = httpx.get(f"{URL[target]}/v1/infer", timeout=3.0)
            body = response.json()
            if response.status_code != 200 or body.get("error"):
                errors += 1
        except (httpx.HTTPError, ValueError):
            errors += 1
        latencies.append((time.monotonic() - request_started) * 1000)
    p95_ms = round(statistics.quantiles(latencies, n=20)[18], 2)
    error_rate = errors / len(latencies)
    step(6, "verify_golden_signals", requests=len(latencies), p95_latency_ms=p95_ms,
         error_rate=error_rate, ok=errors == 0)

    elapsed = round(time.time() - started, 2)
    command = "python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300"
    step(7, "post_incident", ok=errors == 0, elapsed_s=elapsed,
         incident_ts=incident["ts"], measure_command=command)
    return {**result, "golden_signals": {"p95_latency_ms": p95_ms,
                                          "error_rate": error_rate},
            "elapsed_s": elapsed}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
