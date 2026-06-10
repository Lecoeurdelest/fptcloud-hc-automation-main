# Báo cáo Phase 1 — Queue Core

**Ngày hoàn thành:** 27/05/2026  
**Dự án:** fptcloud-hc-automation  
**Phạm vi:** P1.T1 → P1.T8 (Queue core)

---

## 1. Tổng quan trạng thái Phase 1

| Hạng mục | Trạng thái |
|----------|------------|
| P1.T1 — RedisQueue.enqueue | Hoàn thành |
| P1.T2 — RedisQueue.consume | Hoàn thành |
| P1.T3 — ack / nack + backoff | Hoàn thành |
| P1.T4 — Scheduler | Hoàn thành |
| P1.T5 — Reaper | Hoàn thành |
| P1.T6 — DLQ | Hoàn thành |
| P1.T7 — CLI (stats, peek, dlq) | Hoàn thành |
| P1.T8 — Bộ test unit + integration | Hoàn thành |
| Definition of Done (DoD) | **4/4 mục xanh** |
| Review gate (chaos script) | **Chưa** — `scripts/chaos_kill_worker.sh` chưa có |

**Kết luận:** Phase 1 được coi là **hoàn thành về mặt triển khai và kiểm thử tự động**. Cổng review evaluator (chaos kill worker) vẫn chờ script và môi trường Docker đầy đủ.

Repo trước đây chỉ có specs; Phase 1 đã bổ sung mã nguồn Python 3.11, cấu trúc `src/hc/`, `tests/`, `Makefile`, `docker-compose.yml` (Redis/Postgres), và CLI `hc`.

---

## 2. Thành phần đã triển khai

### 2.1 RedisQueue (`src/hc/queue/redis_queue.py`)

- **enqueue:** `ZADD NX hc:dedup` → `XADD hc:tasks`; trả về `Enqueued` hoặc `Duplicate`.
- **consume:** `XREADGROUP` một entry mỗi lần.
- **ack:** `XACK` xóa khỏi PEL.
- **nack:** tăng `attempt`, lên lịch vào `hc:scheduled` (backoff exponential + jitter), hoặc chuyển DLQ khi `attempt > max_attempts`.
- **DLQ:** `XADD hc:dlq` kèm `last_error`, `failed_at`, `payload` đầy đủ; `XACK` bản gốc.
- **replay_dlq:** `task_id` mới (tránh dedup), `attempt=0`.

### 2.2 Scheduler (`src/hc/queue/scheduler.py`)

- Vòng lặp asyncio mỗi 1 giây (cấu hình được).
- `ZRANGEBYSCORE hc:scheduled -inf <now>` → `XADD hc:tasks` + `ZREM` atomically qua pipeline.

### 2.3 Reaper (`src/hc/queue/reaper.py`)

- Mỗi 60 giây (cấu hình được): `XPENDING` → `XCLAIM` khi idle > `HC_REAPER_IDLE_MS`.
- Tăng `attempt`, `XACK` entry cũ, `XADD` payload đã cập nhật.

### 2.4 CLI (`src/hc/cli/main.py`)

- `hc queue stats` — stream, PEL, DLQ, scheduled.
- `hc queue peek --count N`
- `hc dlq list`
- `hc dlq replay <entry_id>`

---

## 3. Kết quả chạy test

### 3.1 Unit tests (fakeredis) — 32 test

| Mã test | Mô tả ngắn | Kết quả |
|---------|-------------|---------|
| T-0101 → T-0104 | Enqueue, dedup, task_id | PASS |
| T-0105 → T-0106 | Consume / timeout | PASS |
| T-0107 → T-0112 | Ack, nack, backoff | PASS |
| T-0113 → T-0115 | Scheduler | PASS |
| T-0116 → T-0119 | Reaper | PASS |
| T-0120 → T-0124 | DLQ + replay | PASS |
| T-0701 → T-0706 | CLI | PASS |

**Lệnh:** `py -3.11 -m pytest tests/unit -m unit -v`  
**Kết quả:** 32 passed, 0 failed

### 3.2 Integration tests — 5 test

| Mã test | Mô tả ngắn | Kết quả |
|---------|-------------|---------|
| T-1001 | 1000 enqueue → 1000 ack, 0 mất | PASS |
| T-1002 | 100 duplicate → 1 stream entry | PASS |
| T-1003 | Consumer group idempotent | PASS |
| T-1004 | Hai consumer round-robin | PASS |
| T-1005 | Scheduler + Reaper đồng thời | PASS |

**Lệnh:** `HC_USE_FAKEREDIS=1 py -3.11 -m pytest tests/integration -m integration -v`  
**Kết quả:** 5 passed, 0 failed

**Ghi chú môi trường:** Docker Desktop không chạy trên máy dev lúc kiểm thử; integration chạy với **fakeredis** (fallback tự động khi Redis thật không ping được). Khi có `redis:7-alpine` qua `docker compose up -d redis`, bỏ `HC_USE_FAKEREDIS` để chạy integration trên Redis thật.

### 3.3 Tổng hợp một lần chạy đầy đủ

```
37 passed in ~3.2s
```

---

## 4. Độ phủ mã (code coverage)

| Thành phần | Mục tiêu | Thực tế |
|------------|----------|---------|
| `src/hc/queue/` | ≥ 85% | **95.0%** (189/199 dòng) |
| `src/hc/cli/` (Phase 1) | ≥ 70% | **~85%** |

**Chi tiết theo file (queue):**

| File | Coverage |
|------|----------|
| `redis_queue.py` | 92% |
| `reaper.py` | 92% |
| `scheduler.py` | 96% |
| `__init__.py` | 100% |

**Công cụ:** `pytest-cov` + `scripts/check_coverage.py --min-queue 85` → **PASS**

---

## 5. Definition of Done — đối chiếu

| DoD | Đáp ứng |
|-----|---------|
| 1000 task enqueue/ack, 0 mất | T-1001 PASS |
| Worker crash → reaper reclaim | T-0116–T-0118, T-1005 PASS |
| Coverage ≥ 85% `src/hc/queue/` | 95.0% |
| `cli queue stats` đúng PEL/DLQ | T-0702 PASS |

---

## 6. Dọn dẹp môi trường test

Mỗi fixture pytest gọi `RedisQueue.flush_all()` **trước và sau** mỗi test:

- Xóa streams: `hc:tasks`, `hc:dlq`
- Xóa ZSET: `hc:dedup`, `hc:scheduled`

**Xác nhận:** Không còn key queue thừa sau suite test; môi trường sạch giữa các subtask/test.

---

## 7. Hướng dẫn chạy lại

```powershell
cd fptcloud-hc-automation
py -3.11 -m pip install -e ".[dev]"

# Unit (fakeredis)
py -3.11 -m pytest tests/unit -m unit -v

# Integration với Redis thật
docker compose up -d redis
py -3.11 -m pytest tests/integration -m integration -v

# Hoặc integration với fakeredis (không cần Docker)
$env:HC_USE_FAKEREDIS="1"
py -3.11 -m pytest tests/integration -m integration -v

# Kiểm tra coverage queue
py -3.11 -m pytest tests/unit tests/integration --cov=src/hc/queue --cov-report=json
py -3.11 scripts/check_coverage.py --min-queue 85
```

---

## 8. Việc còn lại trước Phase 2

1. Thêm `scripts/chaos_kill_worker.sh` và chạy review gate.
2. Chạy integration trên `redis:7-alpine` thật trong CI khi Docker sẵn sàng.
3. Phase 0 (lint CI, Dockerfile đầy đủ) — chưa bắt buộc cho queue core nhưng nên hoàn thiện song song Phase 2.

---

*Báo cáo được tạo tự động sau vòng Agent Driven Development Phase 1.*
