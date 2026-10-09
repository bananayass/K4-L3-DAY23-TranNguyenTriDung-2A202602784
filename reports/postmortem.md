# Postmortem — DR Drill Lab 23 (TEMPLATE)

Theo đúng template §4 "Sau Failover: Blameless Postmortem". Blameless: câu hỏi là
"hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

## 1. Timeline (mọi dòng phải có evidence path:line)

| ISO time | Sự kiện | Evidence |
|---|---|---|
| 2026-10-09T05:29:24Z | outage bắt đầu | `chaos/chaos-events.jsonl:7` |
| 2026-10-09T05:29:24.565Z | user đầu tiên bị ảnh hưởng | `reports/drill-2-withdr.jsonl:22` |
| 2026-10-09T05:29:40.770Z | health check alert | `reports/health-events.jsonl:2` |
| 2026-10-09T05:29:42.815Z | operator confirm cutover | `reports/failover-events.jsonl:5` |
| 2026-10-09T05:29:43.019Z | resolved (request đầu tiên OK từ region phụ) | `reports/drill-2-withdr.jsonl:31` |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: `18.9s` · gap: `281.1s`
- RPO mục tiêu: 300s · đo được: `23.59s` (`13` doc bị mất) · gap: `276.41s`
- **Bước tốn nhiều giây nhất:** `detection/confirmation` — vì sao?

  Vì floor cấu hình 15s, quan sát 16.7s; nó chiếm 88.4% RTO. GPU warm-up 6.06s chồng lấp
  một phần với detection.

## 3. Root cause (5 whys)

Không phải "vì tôi chạy chaos script". Câu hỏi: *nếu đây là outage thật, bước nào
trong runbook của tôi sẽ thất bại?*

1. User lỗi vì edge vẫn route đến Region A đã bị netblock.
2. Edge chưa đổi ngay vì cần xác nhận nhiều lần để tránh flapping.
3. Region B chưa thể nhận traffic ngay vì pool warm, state/model phải restore.
4. B dựa vào snapshot định kỳ nên replica chậm 23.59s và thiếu 13 docs mới nhất.
5. Kiến trúc active-passive ưu tiên chi phí thấp, chưa duy trì compute và state hot ở B.

Điểm dễ thất bại trong outage thật: snapshot/object store có thể không truy cập được hoặc
model version lệch với vector index. Runbook chặn DNS cutover khi `/readyz` chưa đạt 200 và
ghi model version để biến lỗi đó thành abort an toàn thay vì double outage.

## 4. Action items (có owner + deadline)

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Thử interval 3s, threshold 3 trong game day 5 lần; giữ nếu không false-positive | SRE | 2026-10-16 | giảm detection floor 6s |
| 2 | Replicate mỗi 15s và alert khi lag >30s | Storage on-call | 2026-10-23 | giảm RPO tối đa khoảng 15s |

## 5. Ba câu hỏi bắt buộc trả lời

1. `interval × threshold` của bạn là bao nhiêu giây? Nó chiếm bao nhiêu % RTO?

   `5s × 3 = 15s`, bằng 79.4% RTO đo được; detection thực tế là 16.7s (88.4%).

2. Nếu hạ interval xuống 1s, RTO giảm mấy giây — và bạn trả giá gì (§4 flapping)?

   Floor giảm từ 15s còn 3s, lý thuyết giảm khoảng 12s. Đổi lại tăng 5× request
   health-check, dễ phản ứng với lỗi thoáng qua và tăng nguy cơ flapping.

3. Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` của
   bạn có nghĩa gì với khách hàng?

   `docs_lost=13` nghĩa là 13 tài liệu đã ingest ở A nhưng chưa vào snapshot khôi phục.
   Khách hàng có thể không truy vấn được chúng; cần replay từ source-of-truth/WAL hoặc
   yêu cầu nạp lại, rồi đối soát theo document ID.
