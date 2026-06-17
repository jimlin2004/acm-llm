# Remote Access – Let Another System Use Our LLM

How an external system consumes the lab's LLM **privately**, either over **Tailscale**
(remote / cross-network) or over the **internal LAN** (same network). The model is served
through the LiteLLM proxy with per-client API keys, rate limits, and usage logging.

> **Golden rule:** only ever expose the **LiteLLM proxy**. Never expose vLLM directly —
> it has no authentication, so anyone who reaches it can use the GPUs for free.

---

## 1. The endpoint

| | Value |
|---|---|
| Service | LiteLLM proxy (OpenAI-compatible) |
| Host port | `8000` → container `4000` |
| Path prefix | `/v1` |
| Default model | `qwen3-vl-32b` (Qwen3-VL, vision) |
| Small model | `qwen2.5-1.5b` (single-GPU mode only) |
| Health check (no auth) | `GET /health/liveliness` |

The base URL depends on **how** the caller reaches the host (see §3).

```
caller ──► http://<host-address>:8000/v1 ──► LiteLLM ──► vLLM (internal app-net, no ports)
                         (API key + rate limit + per-user log)
```

---

## 2. Host addresses

This host (`user-WS990T`) is reachable on:

| Network | Address | Use when |
|---|---|---|
| Campus / internal LAN | `140.113.28.150` | Caller is on the same NCKU network |
| Tailscale (private mesh VPN) | `100.83.40.102` | Caller is remote / on a different network |
| Tailscale MagicDNS | `user-ws990t.taile0a1fc.ts.net` | Same as above, by name |

> Tailscale traffic is end-to-end encrypted (WireGuard), so plain HTTP over the
> `100.x` address is safe. On the LAN, HTTP is cleartext on the wire — acceptable inside
> a trusted network, but do not route it over the public internet without TLS.

---

## 3. Two access scenarios

### Scenario A — Caller is on our internal LAN

Nothing to install on the caller. They use the host's LAN address directly:

```
Base URL: http://140.113.28.150:8000/v1
```

Make sure the host firewall allows inbound TCP 8000 from the LAN:

```bash
sudo ufw allow from 140.113.0.0/16 to any port 8000 proto tcp   # restrict to campus range
# or, simplest (any source):  sudo ufw allow 8000/tcp
```

### Scenario B — Caller is remote, via Tailscale

1. **Both machines must be in the same tailnet.** On the caller's machine:
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   ```
   Then approve/invite that machine into the tailnet (admin console:
   https://login.tailscale.com/admin/machines). Owner of both nodes here is
   `duong.pt1771@`.

2. The caller uses the Tailscale address — **no firewall changes, no public exposure**:
   ```
   Base URL: http://100.83.40.102:8000/v1
   # or:     http://user-ws990t.taile0a1fc.ts.net:8000/v1
   ```

3. (Optional) Lock it down further with Tailscale ACLs so only specific tailnet nodes
   can reach port 8000 on this host.

> Tailscale also works when the caller is *also* on our internal LAN — it just routes
> over the mesh. So if you are unsure which network a partner is on, Tailscale is the
> safe default.

---

## 4. Issue an API key for the caller (do NOT share the master key)

With LiteLLM running, generate a scoped virtual key using the master key
(`sk-acm-llm-master-2026`). Run this **on the host**:

```bash
curl -s http://localhost:8000/key/generate \
  -H "Authorization: Bearer sk-acm-llm-master-2026" \
  -H "Content-Type: application/json" \
  -d '{
        "models": ["qwen3-vl-32b"],
        "max_budget": 10,
        "rpm_limit": 60,
        "key_alias": "partner-systemX"
      }'
```

The response contains `"key": "sk-..."` — hand **that** key to the partner. It is scoped
to the listed models, has a budget, and is rate-limited independently. Manage/revoke keys
in the LiteLLM UI at `http://localhost:8000/ui` (or via the `/key/delete` API).

---

## 5. Client usage (OpenAI-compatible)

Replace `<BASE_URL>` with the address from §3 and `<KEY>` with the virtual key from §4.

**Python (openai SDK):**
```python
from openai import OpenAI

client = OpenAI(base_url="<BASE_URL>", api_key="<KEY>")

resp = client.chat.completions.create(
    model="qwen3-vl-32b",
    messages=[{"role": "user", "content": "Hello"}],
)
print(resp.choices[0].message.content)
```

**Vision request (Qwen3-VL accepts images):**
```python
resp = client.chat.completions.create(
    model="qwen3-vl-32b",
    messages=[{
        "role": "user",
        "content": [
            {"type": "text", "text": "What is in this image?"},
            {"type": "image_url", "image_url": {"url": "https://example.com/cat.jpg"}},
        ],
    }],
)
```

**curl:**
```bash
curl <BASE_URL>/chat/completions \
  -H "Authorization: Bearer <KEY>" \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3-vl-32b","messages":[{"role":"user","content":"hi"}]}'
```

**List available models:**
```bash
curl <BASE_URL>/models -H "Authorization: Bearer <KEY>"
```

---

## 6. Verify connectivity

```bash
# From the caller — no auth needed, just checks the proxy is reachable
curl http://<host-address>:8000/health/liveliness
# expected: {"status":"healthy"} (or HTTP 200)
```

If this fails:
- **Connection refused / timeout** → LiteLLM not running, wrong port, or firewall/ACL blocking.
- **Reachable on host but not from caller** → firewall (ufw) or, for Tailscale, the caller
  is not in the tailnet.
- **401 / invalid key** on `/v1/...` → key not created or revoked; the `/health/liveliness`
  path itself needs no key.

---

## 7. Security checklist

- [x] Only LiteLLM port `8000` is exposed; vLLM has no published port.
- [ ] Each partner gets a **virtual key** with `max_budget` + `rpm_limit` + `models` scope.
- [ ] Master key (`sk-acm-llm-master-2026`) is never shared and is rotated if leaked.
- [ ] Prefer **Tailscale** (encrypted) for anything off-LAN.
- [ ] If ever exposed to the public internet, put TLS in front (Caddy/nginx) and serve `443`,
      not plain `8000`. See `docs/litellm-guide.md`.
- [ ] Firewall scoped to the campus range / tailnet rather than `0.0.0.0/0` where possible.
