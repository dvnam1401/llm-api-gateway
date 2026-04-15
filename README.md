# LLM API Gateway

Reverse proxy stateless cho Groq API. Nhận request chuẩn OpenAI từ chatbot, tự động chọn API key còn healthy, forward và trả response. Chatbot không cần biết về key rotation hay failover.

**Stack:** Python 3.11 + FastAPI + Redis  
**Model:** `qwen/qwen3-32b` (Groq)

## Cấu trúc project

```
gateway/
├── main.py            # FastAPI app, endpoints /v1/chat/completions & /health
├── key_manager.py     # Pool key, circuit breaker, quota tracking (Redis)
├── proxy.py           # Forward request đến Groq, failover loop, streaming
├── config.py          # Đọc env vars
requirements.txt
render.yaml            # Render deploy config
.env.example
```

## Chạy local

### 1. Yêu cầu

- Python 3.11+
- Redis server đang chạy (mặc định `localhost:6379`)

### 2. Tạo môi trường ảo và cài thư viện

```bash
python -m venv venv

# Linux / macOS
source venv/bin/activate

# Windows PowerShell
.\venv\Scripts\activate

pip install -r requirements.txt
```

### 3. Cấu hình biến môi trường

```bash
cp .env.example .env   # Linux/macOS
copy .env.example .env # Windows
```

Mở file `.env` và điền giá trị thật:

| Biến | Mô tả |
|------|-------|
| `GROQ_API_KEY_acc01`, `acc02`, ... | Mỗi Groq API key là một biến riêng. Suffix (phần sau `GROQ_API_KEY_`) quyết định thứ tự ưu tiên (sort alphabet) |
| `GATEWAY_SECRET` | Bearer token chatbot dùng để xác thực với gateway |
| `REDIS_URL` | URL kết nối Redis, mặc định `redis://localhost:6379` |
| `REQUEST_TIMEOUT` | Timeout mỗi request đến Groq (giây), mặc định `60` |
| `CIRCUIT_BREAKER_THRESHOLD` | Số lỗi liên tiếp để ngắt key, mặc định `3` |
| `CIRCUIT_BREAKER_COOLDOWN` | Thời gian giữ key ở trạng thái OPEN (giây), mặc định `300` |

### 4. Chạy server

Load biến môi trường rồi khởi động:

```bash
# Linux / macOS
export $(grep -v '^#' .env | xargs)
uvicorn gateway.main:app --host 0.0.0.0 --port 8000

# Windows PowerShell
Get-Content .env | ForEach-Object {
    if ($_ -and $_ -notmatch '^#') {
        $k, $v = $_ -split '=', 2
        [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), 'Process')
    }
}
uvicorn gateway.main:app --host 0.0.0.0 --port 8000
```

Server chạy tại `http://localhost:8000`.

## API

### `POST /v1/chat/completions`

Tương thích chuẩn OpenAI. Gateway override `model` thành `qwen/qwen3-32b`.

**Header:** `Authorization: Bearer <GATEWAY_SECRET>`

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer your_secret" \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role": "user", "content": "Xin chào"}],
    "stream": false
  }'
```

Streaming (SSE):

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer your_secret" \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role": "user", "content": "Xin chào"}],
    "stream": true
  }'
```

**Error responses:**

| Tình huống | Status | Body |
|-----------|--------|------|
| Sai secret | 401 | `{"error": "Unauthorized"}` |
| Hết key healthy | 503 | `{"error": "No healthy API keys available"}` |
| Groq lỗi không recover được | 502 | `{"error": "Upstream error", "detail": "..."}` |

### `GET /health`

Không cần xác thực. Trả trạng thái gateway và từng key.

```bash
curl http://localhost:8000/health
```

Response mẫu:

```json
{
  "status": "ok",
  "keys": [
    {
      "suffix": "acc01",
      "circuit_state": "closed",
      "daily_requests": 142,
      "daily_tokens": 89500,
      "healthy": true
    },
    {
      "suffix": "acc02",
      "circuit_state": "open",
      "daily_requests": 0,
      "daily_tokens": 0,
      "healthy": false
    }
  ],
  "healthy_key_count": 1
}
```

## Cơ chế hoạt động

### Key rotation & Failover

- Keys được sort theo suffix alphabet → thứ tự ưu tiên cố định.
- Mỗi request, gateway chọn key healthy đầu tiên. Nếu lỗi → thử key tiếp theo.
- Mỗi key chỉ thử **một lần** mỗi request (không retry cùng key).

### Quota

Mỗi key bị giới hạn/ngày (reset 00:00 UTC):
- **1,000 requests**
- **500,000 tokens**

### Circuit breaker

- Key gặp lỗi liên tiếp >= `CIRCUIT_BREAKER_THRESHOLD` (mặc định 3) → circuit **OPEN**, key bị loại.
- Sau `CIRCUIT_BREAKER_COOLDOWN` giây (mặc định 300) → Redis tự xóa → key được thử lại.
- Key bị lỗi auth (401/403 từ Groq) → circuit OPEN **vĩnh viễn** (key invalid).

### Streaming

- `"stream": true` → gateway forward SSE stream từ Groq trực tiếp về client.
- Nếu stream fail giữa chừng → **không failover** (đã gửi response rồi), chỉ log lỗi và đóng stream.

## Deploy lên Render

Project có sẵn `render.yaml`. Các bước:

1. Push code lên GitHub repo.
2. Trên Render Dashboard, chọn **New > Blueprint** và kết nối repo.
3. Render tự đọc `render.yaml`, tạo web service + Redis instance.
4. Vào **Environment** của web service, thêm các biến:
   - `GROQ_API_KEY_acc01`, `GROQ_API_KEY_acc02`, ... (mỗi key một biến)
   - `GATEWAY_SECRET`
5. `REDIS_URL` được Render tự inject từ Redis add-on.
6. Deploy.

## Logging

Log ra stdout (Render tự collect):

```
[2024-01-15 10:23:45] REQUEST  model=qwen/qwen3-32b stream=false
[2024-01-15 10:23:45] ATTEMPT  key=acc01
[2024-01-15 10:23:46] SUCCESS  key=acc01 tokens=1243 latency=1.2s
```

Khi failover:

```
[2024-01-15 10:23:45] REQUEST  model=qwen/qwen3-32b stream=false
[2024-01-15 10:23:45] ATTEMPT  key=acc01
[2024-01-15 10:23:46] FAILURE  key=acc01 reason=rate_limit_daily
[2024-01-15 10:23:46] ATTEMPT  key=acc02
[2024-01-15 10:23:48] SUCCESS  key=acc02 tokens=1243 latency=2.1s
```
