# Runbook 1 trang — Region chính down (TEMPLATE, sinh viên điền)

Runbook phải chạy được lúc 3h sáng bởi người KHÔNG viết nó. Mỗi bước: lệnh copy-paste
được + cách biết bước đó xong.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python chaos/kill_region.py status` | `a.alive=false` 3 lần liên tiếp | on-call |
| 2 | Mở incident + bấm giờ RTO | `python3 dr/runbook.py --primary a --target b --backend fs` | ts ghi vào `reports/runbook-run.jsonl` | on-call |
| 3 | Restore state ở region phụ | `python state/snapshot.py get --region b --backend fs` | Có `rpo_seconds`, `docs_lost`, model version | storage on-call |
| 4 | Scale pool warm→full | `curl -f localhost:8002/readyz` | `/readyz` của b trả 200 | ML platform on-call |
| 5 | DNS/LB cutover | `curl -s localhost:8080/edge/state` | `curl localhost:8080/edge/state` cho `active_region=b` | incident commander |
| 6 | Verify golden signals | `for i in {1..10}; do curl -fsS localhost:8002/v1/infer >/dev/null || echo FAIL; done` | p95 < 100ms, error rate < 0% | SRE on-call |
| 7 | Đo RTO + postmortem | `python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl` | `rto_verdict` != null | incident commander |

**Rollback (failover ngược):** điều kiện nào thì trả traffic về region A? Ai quyết định?
(§4 Anti-Patterns: full-auto không có circuit breaker → 2 region flap qua lại.)

Chỉ rollback khi Region A `/readyz` trả 200 ổn định 3 lần, replication catch-up hoàn tất,
và Region B vi phạm golden signals (`error_rate > 1%` hoặc `p95 > 100ms`) trong 5 phút.
Incident commander là người duy nhất phê duyệt. ML platform on-call thực thi cùng quy trình
với `--primary b --target a`; không sửa `edge/active_region` trực tiếp. Nếu A chưa ready,
giữ traffic tại B và điều tra, tránh flap hai chiều.
