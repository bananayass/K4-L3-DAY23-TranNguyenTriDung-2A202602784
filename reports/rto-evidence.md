# RTO/RPO Evidence — Lab 23

Quy tắc duy nhất: mỗi con số ở đây phải trỏ được về **một dòng log thật**
(`đường/dẫn.jsonl:số_dòng`). `pytest tests/test_rto_evidence.py` sẽ mở từng file ra kiểm tra.
Con số không có evidence = trượt, bất kể các phần khác.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-10-09T05:23:37Z` | chaos kill | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | `+0.2s` | dòng `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:17` |
| Request thành công sau đó | không có | không có dòng `ok:true` nào sau t_outage | `reports/measure-drill-1.json` |
| RTO | `NO_RECOVERY` | `tools/measure_rto.py` | `reports/measure-drill-1.json` |

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0 | `action:kill` | `chaos/chaos-events.jsonl:7` |
| User thấy lỗi đầu tiên | 0.5s | dòng `ok:false` đầu | `reports/drill-2-withdr.jsonl:22` |
| Health check phát hiện | 16.7s | `to:UNHEALTHY, region:a` | `reports/health-events.jsonl:2` |
| Snapshot restore xong | 12.6s | `step:2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Region phụ ready | 18.7s | `step:4_wait_ready` | `reports/failover-events.jsonl:4` |
| DNS cutover | 18.7s | `5_dns_cutover` | `reports/failover-events.jsonl:5` |
| **RTO đo được** | **18.9s** | dòng `ok:true` đầu sau lỗi | `reports/drill-2-withdr.jsonl:31` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | `18.9s` | 300s (5 phút) | PASS |
| RPO — Vector DB | `23.59s` / `13 docs` | 300s (5 phút) | PASS |

## 3. RTO của tôi gồm những gì (bắt buộc — đây là phần chấm điểm hiểu bài)

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect floor | 16.660s (floor 15.0s) | `interval_s × threshold` trong `reports/health-events.jsonl:2` | Giảm interval sau khi đánh giá false-positive/flapping |
| Snapshot restore | 0.000s | 2_restore → 3_scale | Wall time 0.005s, chạy hoàn toàn trong detection window nên contribution bằng 0 |
| GPU pool warm-up | 2.045s còn lại sau detection | `waited_s` ở `4_wait_ready` | Giữ warm capacity hoặc preload model |
| DNS/LB TTL cache | 0.204s | t_recovered − t_cutover | TTL thấp hơn hoặc global LB active probing |
