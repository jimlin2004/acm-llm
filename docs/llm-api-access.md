# Using the ACM Lab LLM (OpenAI-compatible API)

For **external parties** who want to call the lab's vLLM model. The endpoint speaks the
**OpenAI API**, so any OpenAI-compatible SDK/tool works — just change `base_url` and
`api_key`.

Requests go through an authenticated **Caddy gateway** (`:8081`) that checks a shared Bearer
key and forwards to vLLM. The raw vLLM port (`:8002`) is firewalled off — always use the
gateway.

---

## Connection details

| | Value |
|---|---|
| Base URL (campus LAN) | `http://140.113.28.150:8081/v1` |
| Base URL (Tailscale) | `http://100.83.40.102:8081/v1` |
| API key | **shared privately by the admin** — not committed here. Stored host-side at `caddy/llm-gateway.key`. |
| Model | `qwen3.6-35b-a3b` (Qwen3.6-35B-A3B-FP8, vision) |

**Which Base URL?**
- On the **NYCU network (140.113.x)** → use the LAN URL, nothing to install.
- **Off-campus / another network** → use the Tailscale URL; do the "Tailscale" step below first.

---

## (Off-campus only) Join Tailscale

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Then ask the ACM Lab admin (`duong.pt1771@`) to approve your machine into the tailnet. Once
approved, the `http://100.83.40.102:8081` URL works.

---

## Quick test

```bash
curl http://140.113.28.150:8081/v1/models \
  -H "Authorization: Bearer <YOUR_API_KEY>"
```

A list containing `qwen3.6-35b-a3b` means you're connected. A missing/wrong key returns
`401 Unauthorized`.

---

## Python (OpenAI SDK)

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://140.113.28.150:8081/v1",
    api_key="<YOUR_API_KEY>",
)

resp = client.chat.completions.create(
    model="qwen3.6-35b-a3b",
    messages=[{"role": "user", "content": "Hello"}],
    max_tokens=1024,            # see the note below
)
print(resp.choices[0].message.content)
```

### Streaming

```python
stream = client.chat.completions.create(
    model="qwen3.6-35b-a3b",
    messages=[{"role": "user", "content": "Count from 1 to 5"}],
    max_tokens=512, stream=True,
)
for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="", flush=True)
```

### Vision (image input)

```python
resp = client.chat.completions.create(
    model="qwen3.6-35b-a3b",
    messages=[{
        "role": "user",
        "content": [
            {"type": "text", "text": "What is in this image?"},
            {"type": "image_url", "image_url": {"url": "https://example.com/cat.jpg"}},
        ],
    }],
    max_tokens=1024,
)
```

---

## curl (chat)

```bash
curl http://140.113.28.150:8081/v1/chat/completions \
  -H "Authorization: Bearer <YOUR_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3.6-35b-a3b","messages":[{"role":"user","content":"hi"}],"max_tokens":512}'
```

---

## Notes & gotchas

- ⚠️ **This is a reasoning model.** It spends tokens "thinking" before the visible answer. If
  `max_tokens` is too low (a few dozen), `content` can come back **empty** because the budget
  was consumed by the thinking phase. Use **≥ 512–1024**.
- **Vision supported** — send images via `image_url` per the OpenAI multimodal format.
- **Available endpoints**: `/v1/models`, `/v1/chat/completions`, `/v1/completions`,
  `/v1/embeddings` (whatever vLLM serves).
- **HTTP, not HTTPS** — only use it inside the campus LAN or over Tailscale (encrypted). Do
  not send the key over the public internet.
- **One shared key.** Don't distribute it widely. Ask the admin to rotate it if it leaks.

---

## Operator notes (ACM Lab side)

- Gateway config: the `:8081` block in `caddy/config/Caddyfile` (Caddy runs on the host
  network). It checks `Authorization: Bearer <key>` and `reverse_proxy 127.0.0.1:8002`.
- Shared key file: `caddy/llm-gateway.key` (chmod 600, **not** committed).
- Caddy has `admin off`, so apply config changes with `docker restart caddy-proxy` (briefly
  blips Open WebUI on `:3000`).
- Raw vLLM `:8002` is blocked from the LAN/Tailscale interfaces via iptables `DOCKER-USER`
  (only localhost + the docker bridge reach it). See [`remote-access.md`](remote-access.md).
- To rotate the key: edit the `Bearer` value in the `:8081` block (and `caddy/llm-gateway.key`),
  then `docker restart caddy-proxy`.
