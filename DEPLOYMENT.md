# ACM LLM Lab — Server Migration & Deployment Plan

> One document, two languages. **[English](#english)** first, **[Tiếng Việt](#tiếng-việt)** below.
> Goal: bring the whole stack up on a fresh server smoothly, in the right order, with nothing forgotten.

---

## English

### 0. What this stack is (so nothing is missed)

Not everything runs from `docker compose`. The stack is a mix of compose services and
manually-run (`docker run`) containers plus host services. Full map lives in
[`docs/architecture.md`](docs/architecture.md). Components to reproduce on the new server:

| # | Component | How it runs today | Port(s) | Notes |
|---|-----------|-------------------|---------|-------|
| 1 | **vLLM** `qwen3.6-35b-a3b` | `docker run` (GPU) | host `8002` | Main LLM, OpenAI-compatible, no auth. Needs GPU + model weights. |
| 2 | **Ollama** `qwen2.5:3b-instruct` | host systemd | `11434` | Intent router + WebUI background tasks. |
| 3 | **Orchestrator** + **sim-server** | `docker-compose.orchestrator.yml` | `8100`, `9000` | FastAPI + LangGraph + ngspice. Hosts Telegram + LINE in-process. |
| 4 | **Open WebUI** | `docker run` (bridge net) | `3010→8080` | Chat UI; "ACM Assistant" pipe → orchestrator. |
| 5 | **Caddy** `caddy-proxy` | `docker run` (host net) | `3000`, `8081` | Fronts WebUI + authed LLM gateway. |
| 6 | **LiteLLM** `litellm-proxy` | `docker run` (host net) | `8003` | Loopback logging passthrough. |
| 7 | **SearXNG** | `docker-compose.yml` | `5050` | Private search for WebUI. |
| 8 | **Webhook tunnel** (LINE) | `ngrok` fixed domain (or cloudflared `line-tunnel.sh`) | – | Delivers LINE webhooks to `:8100`. |
| 9 | **Monitoring** | `monitoring/docker-compose.monitoring.yml` | `3001`, … | Grafana/Prometheus/Loki/cAdvisor/exporters. |

**Golden rule:** never `docker compose down` the manually-run containers (vLLM, Open WebUI,
Caddy, LiteLLM). They are outside compose on purpose.

### 1. Target server prerequisites

- **Hardware:** NVIDIA GPU with enough VRAM for `Qwen3.6-35B-A3B-FP8` at 131k ctx
  (current box = 2× RTX PRO 6000, 96 GB). One GPU holds one model at a time.
- **OS:** Ubuntu/Debian-like, recent kernel, `sudo`.
- **NVIDIA driver** installed and working (`nvidia-smi` shows the GPU).
- Outbound internet for image pulls + model downloads.
- Disk for model weights (HDD/SSD path, e.g. current `/mnt/HDD4/...`). Plan tens of GB.

Install the base with the repo helper (idempotent — safe to re-run):

```bash
cd acm-llm
./setup.sh      # installs Docker, NVIDIA Container Toolkit, creates app-net, bootstraps SearXNG
newgrp docker   # or log out/in so the docker group applies
```

`setup.sh` does: Docker → NVIDIA Container Toolkit → `docker network create app-net` →
SearXNG default config. Confirm with `docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi`.

### 2. Capture the exact run commands from the OLD server first

Compose files are in git, but the four **manually-run** containers are not. Before you
touch the new server, dump their exact flags on the current one so the new box is a faithful copy:

```bash
# On the OLD server — record everything, don't trust memory
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Ports}}' | tee ~/stack-containers.txt
for c in qwen3.6-35b-a3b open-webui caddy-proxy litellm-proxy; do
  echo "===== $c =====";
  docker inspect "$c" --format '{{json .Config}}{{json .HostConfig}}';
done | tee ~/stack-run-configs.json
# Optional convenience: `runlike <container>` reconstructs the docker run line.
```

Also note the **host paths / volumes** those containers mount (model cache, `open-webui`
named volume, Caddy config/data, LiteLLM config + logs) — those must be recreated or copied.

### 3. Get the code onto the new server

The repo is git-tracked but has **no remote yet** (we add one in §8 / at the end of this task).
Once pushed:

```bash
git clone <your-github-url> acm-llm
cd acm-llm
```

`.env` and `*.secret.md` are gitignored — they are **not** in the clone. Move them separately (§4).

### 4. Secrets — copy securely, then ROTATE

`.env` holds live keys and is never committed. Copy it over an encrypted channel (scp/rsync
over SSH), never via git or chat:

```bash
scp acm-llm/.env         newserver:~/acm-llm/.env
scp -r acm-llm/searxng   newserver:~/acm-llm/searxng      # if you want the same UUID/secret
# Also copy any docs/*.secret.md if you keep them.
```

Then start `.env` from `.env.example` if you prefer a clean file, and fill in:
`OPENAI_API_KEY`, `LLM_BASE_URL`/`LLM_MODEL`, `SIM_API_KEY`, `LINE_CHANNEL_SECRET`,
`LINE_CHANNEL_ACCESS_TOKEN`, `LINE_PUBLIC_BASE`, `TELEGRAM_BOT_TOKEN`, `NGROK_AUTHTOKEN`,
router/hermes endpoints, abuse-guard limits.

> **Rotate these on migration** (they were live on the old host): `SIM_API_KEY`,
> `LINE_CHANNEL_ACCESS_TOKEN`, `TELEGRAM_BOT_TOKEN`, `OPENAI_API_KEY`, `NGROK_AUTHTOKEN`.
> Update `.env` with the new values before bringing services up.

### 5. Bring the stack up — in order

**5.1 Host Ollama (router / background tasks)**

```bash
curl -fsSL https://ollama.com/install.sh | sh   # if not present; runs as systemd *:11434
ollama pull qwen2.5:3b-instruct
```

**5.2 vLLM main model** (GPU, `docker run` — adapt flags from §2 dump; reference form):

```bash
docker run -d --name qwen3.6-35b-a3b --restart unless-stopped \
  --gpus '"device=1"' --ipc=host \
  -p 8002:8000 \
  -v /mnt/HDD4/hf-cache:/root/.cache/huggingface \
  -e HF_TOKEN=$HF_TOKEN \
  vllm/vllm-openai:latest \
  --model Qwen/Qwen3.6-35B-A3B-FP8 --served-model-name qwen3.6-35b-a3b \
  --max-model-len 131072 --reasoning-parser qwen3 \
  --enable-auto-tool-choice --tool-call-parser qwen3_xml
# Wait until: curl -s localhost:8002/v1/models  returns the model.
```

**5.3 SearXNG** (compose):

```bash
docker compose up -d searxng
```

**5.4 Orchestrator + sim-server** (compose, builds locally):

```bash
docker compose -f docker-compose.orchestrator.yml up -d --build
curl -s localhost:8100/health          # orchestrator
curl -s localhost:9000/health          # sim-server
```

**5.5 Open WebUI, Caddy, LiteLLM** (`docker run` — recreate from the §2 dumps).
Restore the `open-webui` named volume if you want existing chats/users, otherwise it
starts fresh. Apply branding with `webui-assets/apply-branding.sh` and install the
`webui-assets/acm_assistant_pipe.py` pipe pointing at the orchestrator.

**5.6 Monitoring** (optional but recommended):

```bash
docker compose -f monitoring/docker-compose.monitoring.yml up -d
./monitoring/setup-dashboards.sh
```

### 6. Public webhook (LINE) + Telegram

- **Telegram** works as soon as the orchestrator is up (long-poll, no inbound URL). Just
  ensure `TELEGRAM_BOT_TOKEN` is set and `TELEGRAM_ALLOWED_CHAT_IDS` is correct.
- **LINE** needs a public HTTPS webhook to `:8100/line/webhook`. Current setup uses an
  **ngrok fixed domain** (set `NGROK_AUTHTOKEN` + `LINE_PUBLIC_BASE`); the older
  `line-tunnel.sh` (cloudflared quick tunnel) still works as a fallback. After the tunnel
  is live, **re-register the webhook URL** in the LINE Developers console (or the script
  does it automatically). Verify with LINE's "Verify" button.

### 7. Verify end-to-end (smoke test)

```bash
# 1. LLM reachable
curl -s localhost:8002/v1/models | jq '.data[].id'
# 2. Router model present
ollama list | grep qwen2.5
# 3. Orchestrator flow (chat)
curl -s localhost:8100/flow/start -H 'content-type: application/json' \
  -d '{"text":"hello","user_id":"smoke"}' | head
# 4. Circuit flow via sim-server (attach/paste a small netlist through the API)
# 5. Open WebUI → "ACM Assistant" model answers in the browser
# 6. Telegram bot replies; LINE "Verify" succeeds
# 7. Run the eval suite:
python -m pytest tests/llm_eval   # or the project's documented entrypoint
# 8. Grafana dashboards populate at :3001
```

### 8. Push code to GitHub (done at the end of this task)

```bash
git remote add origin <your-github-url>
git add -A && git commit -m "docs: server migration & deployment plan"
git push -u origin main
```

`.env`, `*.bak*`, `__pycache__/`, `.venv/` are gitignored, so **no secrets leave the box**.
Double-check with `git status --ignored` before pushing.

### 9. Rollback / safety

- Keep the old server running until the new one passes §7 fully.
- DNS/webhook cutover (LINE `LINE_PUBLIC_BASE`) is the real switch — flip it last.
- Model weights are the slowest step; pre-download on the new box before cutover.

---

## Tiếng Việt

### 0. Stack gồm những gì (để không sót)

Không phải mọi thứ đều chạy bằng `docker compose`. Stack là hỗn hợp của service compose +
container chạy tay (`docker run`) + service trên host. Bản đồ đầy đủ ở
[`docs/architecture.md`](docs/architecture.md). Các thành phần cần dựng lại trên server mới:

| # | Thành phần | Cách chạy hiện tại | Cổng | Ghi chú |
|---|------------|--------------------|------|---------|
| 1 | **vLLM** `qwen3.6-35b-a3b` | `docker run` (GPU) | host `8002` | LLM chính, chuẩn OpenAI, không auth. Cần GPU + weights. |
| 2 | **Ollama** `qwen2.5:3b-instruct` | systemd trên host | `11434` | Router phân loại ý định + task nền WebUI. |
| 3 | **Orchestrator** + **sim-server** | `docker-compose.orchestrator.yml` | `8100`, `9000` | FastAPI + LangGraph + ngspice. Chạy Telegram + LINE in-process. |
| 4 | **Open WebUI** | `docker run` (bridge net) | `3010→8080` | Giao diện chat; pipe "ACM Assistant" → orchestrator. |
| 5 | **Caddy** `caddy-proxy` | `docker run` (host net) | `3000`, `8081` | Front WebUI + cổng LLM có auth. |
| 6 | **LiteLLM** `litellm-proxy` | `docker run` (host net) | `8003` | Passthrough loopback để log. |
| 7 | **SearXNG** | `docker-compose.yml` | `5050` | Search riêng cho WebUI. |
| 8 | **Tunnel webhook** (LINE) | `ngrok` fixed domain (hoặc cloudflared `line-tunnel.sh`) | – | Đẩy webhook LINE về `:8100`. |
| 9 | **Monitoring** | `monitoring/docker-compose.monitoring.yml` | `3001`, … | Grafana/Prometheus/Loki/cAdvisor/exporters. |

**Quy tắc vàng:** tuyệt đối không `docker compose down` các container chạy tay (vLLM, Open
WebUI, Caddy, LiteLLM). Chúng nằm ngoài compose là có chủ đích.

### 1. Điều kiện cần trên server đích

- **Phần cứng:** GPU NVIDIA đủ VRAM cho `Qwen3.6-35B-A3B-FP8` ở 131k ctx
  (máy hiện tại = 2× RTX PRO 6000, 96 GB). Một GPU chỉ giữ một model tại một thời điểm.
- **OS:** kiểu Ubuntu/Debian, kernel mới, có `sudo`.
- **Driver NVIDIA** đã cài và chạy (`nvidia-smi` thấy GPU).
- Có internet để pull image + tải model.
- Ổ đĩa cho weights (đường dẫn HDD/SSD, ví dụ `/mnt/HDD4/...`). Dự trù vài chục GB.

Cài nền bằng script sẵn có (idempotent — chạy lại an toàn):

```bash
cd acm-llm
./setup.sh      # cài Docker, NVIDIA Container Toolkit, tạo app-net, khởi tạo SearXNG
newgrp docker   # hoặc đăng xuất/đăng nhập lại để nhóm docker có hiệu lực
```

`setup.sh` làm: Docker → NVIDIA Container Toolkit → `docker network create app-net` →
config mặc định SearXNG. Kiểm tra: `docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi`.

### 2. Trước tiên: chụp lại lệnh chạy chính xác từ server CŨ

Các file compose có trong git, nhưng 4 container **chạy tay** thì không. Trước khi đụng
server mới, hãy dump chính xác flag của chúng ở máy hiện tại để máy mới là bản sao trung thực:

```bash
# Trên server CŨ — ghi lại mọi thứ, đừng tin trí nhớ
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Ports}}' | tee ~/stack-containers.txt
for c in qwen3.6-35b-a3b open-webui caddy-proxy litellm-proxy; do
  echo "===== $c =====";
  docker inspect "$c" --format '{{json .Config}}{{json .HostConfig}}';
done | tee ~/stack-run-configs.json
# Tiện lợi: `runlike <container>` dựng lại nguyên dòng docker run.
```

Ghi lại cả **đường dẫn host / volume** mà chúng mount (cache model, volume `open-webui`,
config/data của Caddy, config + logs của LiteLLM) — phải tạo lại hoặc copy sang.

### 3. Đưa code lên server mới

Repo đang được git quản lý nhưng **chưa có remote** (ta thêm ở §8 / cuối task này). Sau khi push:

```bash
git clone <url-github-cua-ban> acm-llm
cd acm-llm
```

`.env` và `*.secret.md` bị gitignore — **không** có trong bản clone. Chuyển riêng (xem §4).

### 4. Secrets — copy an toàn, rồi XOAY KHÓA

`.env` chứa key thật và không bao giờ commit. Copy qua kênh mã hóa (scp/rsync qua SSH),
không bao giờ qua git hay chat:

```bash
scp acm-llm/.env         newserver:~/acm-llm/.env
scp -r acm-llm/searxng   newserver:~/acm-llm/searxng      # nếu muốn giữ nguyên UUID/secret
# Copy cả docs/*.secret.md nếu bạn giữ chúng.
```

Hoặc tạo `.env` mới từ `.env.example` cho sạch, rồi điền:
`OPENAI_API_KEY`, `LLM_BASE_URL`/`LLM_MODEL`, `SIM_API_KEY`, `LINE_CHANNEL_SECRET`,
`LINE_CHANNEL_ACCESS_TOKEN`, `LINE_PUBLIC_BASE`, `TELEGRAM_BOT_TOKEN`, `NGROK_AUTHTOKEN`,
endpoint router/hermes, các giới hạn chống lạm dụng.

> **Xoay các khóa này khi migrate** (chúng đã dùng thật trên máy cũ): `SIM_API_KEY`,
> `LINE_CHANNEL_ACCESS_TOKEN`, `TELEGRAM_BOT_TOKEN`, `OPENAI_API_KEY`, `NGROK_AUTHTOKEN`.
> Cập nhật `.env` bằng giá trị mới trước khi bật service.

### 5. Bật stack — theo đúng thứ tự

**5.1 Ollama trên host (router / task nền)**

```bash
curl -fsSL https://ollama.com/install.sh | sh   # nếu chưa có; chạy systemd *:11434
ollama pull qwen2.5:3b-instruct
```

**5.2 vLLM model chính** (GPU, `docker run` — chỉnh flag theo dump ở §2; dạng tham khảo):

```bash
docker run -d --name qwen3.6-35b-a3b --restart unless-stopped \
  --gpus '"device=1"' --ipc=host \
  -p 8002:8000 \
  -v /mnt/HDD4/hf-cache:/root/.cache/huggingface \
  -e HF_TOKEN=$HF_TOKEN \
  vllm/vllm-openai:latest \
  --model Qwen/Qwen3.6-35B-A3B-FP8 --served-model-name qwen3.6-35b-a3b \
  --max-model-len 131072 --reasoning-parser qwen3 \
  --enable-auto-tool-choice --tool-call-parser qwen3_xml
# Chờ đến khi: curl -s localhost:8002/v1/models  trả về model.
```

**5.3 SearXNG** (compose):

```bash
docker compose up -d searxng
```

**5.4 Orchestrator + sim-server** (compose, build tại chỗ):

```bash
docker compose -f docker-compose.orchestrator.yml up -d --build
curl -s localhost:8100/health          # orchestrator
curl -s localhost:9000/health          # sim-server
```

**5.5 Open WebUI, Caddy, LiteLLM** (`docker run` — dựng lại từ dump ở §2).
Khôi phục volume `open-webui` nếu muốn giữ chat/user cũ, không thì nó chạy mới. Áp branding
bằng `webui-assets/apply-branding.sh` và cài pipe `webui-assets/acm_assistant_pipe.py` trỏ
về orchestrator.

**5.6 Monitoring** (tùy chọn nhưng nên có):

```bash
docker compose -f monitoring/docker-compose.monitoring.yml up -d
./monitoring/setup-dashboards.sh
```

### 6. Webhook công khai (LINE) + Telegram

- **Telegram** chạy ngay khi orchestrator lên (long-poll, không cần URL vào). Chỉ cần
  `TELEGRAM_BOT_TOKEN` đúng và `TELEGRAM_ALLOWED_CHAT_IDS` chuẩn.
- **LINE** cần webhook HTTPS công khai tới `:8100/line/webhook`. Hiện dùng **ngrok fixed
  domain** (đặt `NGROK_AUTHTOKEN` + `LINE_PUBLIC_BASE`); `line-tunnel.sh` cũ (cloudflared
  quick tunnel) vẫn dùng được như phương án dự phòng. Sau khi tunnel lên, **đăng ký lại URL
  webhook** trong LINE Developers console (hoặc script tự làm). Bấm "Verify" của LINE để kiểm tra.

### 7. Kiểm tra end-to-end (smoke test)

```bash
# 1. LLM truy cập được
curl -s localhost:8002/v1/models | jq '.data[].id'
# 2. Có model router
ollama list | grep qwen2.5
# 3. Flow orchestrator (chat)
curl -s localhost:8100/flow/start -H 'content-type: application/json' \
  -d '{"text":"hello","user_id":"smoke"}' | head
# 4. Flow mạch qua sim-server (gửi/dán một netlist nhỏ qua API)
# 5. Open WebUI → model "ACM Assistant" trả lời trên trình duyệt
# 6. Bot Telegram trả lời; LINE "Verify" thành công
# 7. Chạy bộ eval:
python -m pytest tests/llm_eval   # hoặc entrypoint mà project ghi
# 8. Dashboard Grafana có dữ liệu ở :3001
```

### 8. Đẩy code lên GitHub (làm ở cuối task này)

```bash
git remote add origin <url-github-cua-ban>
git add -A && git commit -m "docs: server migration & deployment plan"
git push -u origin main
```

`.env`, `*.bak*`, `__pycache__/`, `.venv/` đã bị gitignore, nên **không secret nào rời máy**.
Kiểm tra lại bằng `git status --ignored` trước khi push.

### 9. Rollback / an toàn

- Giữ server cũ chạy đến khi server mới qua hết §7.
- Chuyển DNS/webhook (LINE `LINE_PUBLIC_BASE`) mới là công tắc thật — làm sau cùng.
- Tải weights là bước chậm nhất; tải trước trên máy mới trước khi cutover.
