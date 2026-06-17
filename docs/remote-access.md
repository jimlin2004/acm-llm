# Remote Access – Reaching the Lab Privately

How a person or an external system reaches the lab **privately**, either over **Tailscale**
(remote / cross-network) or over the **internal LAN** (same network).

> **History:** access used to go through a **LiteLLM proxy** with per-client virtual keys.
> LiteLLM was removed on 2026-06-15. There is **no authenticated LLM gateway in the stack
> right now** — see [§4](#4-programmatic-api-access) for the current options and the gap.

> **Golden rule:** never expose **vLLM** (`:8002`) directly. It has no authentication —
> anyone who reaches it can use the GPUs for free. Only ever expose a fronted, authenticated
> service (Open WebUI behind Caddy), and only over Tailscale or a firewalled LAN port.

---

## 1. The entry point

The user-facing entry point is **Open WebUI**, served behind the **Caddy** reverse proxy.

| | Value |
|---|---|
| Service | Open WebUI (chat UI + OpenAI-compatible API) |
| Front | Caddy `caddy-proxy` on port `3000` → Open WebUI (`127.0.0.1:3010`) |
| Models | `ACM Assistant` (orchestrator pipe) + any model registered in Open WebUI |
| Health | Open WebUI `GET /health` |

```
caller ──► http://<host-address>:3000 ──► Caddy ──► Open WebUI ──► orchestrator / vLLM
                                          (auth handled by Open WebUI)
```

vLLM and the orchestrator stay on the internal side; they are not published to callers.

---

## 2. Host addresses

This host (`user-WS990T`) is reachable on:

| Network | Address | Use when |
|---|---|---|
| Campus / internal LAN | `140.113.28.150` | Caller is on the same NCKU network |
| Tailscale (private mesh VPN) | `100.83.40.102` | Caller is remote / on a different network |
| Tailscale MagicDNS | `user-ws990t.taile0a1fc.ts.net` | Same as above, by name |

> Tailscale traffic is end-to-end encrypted (WireGuard), so plain HTTP over the
> `100.x` address is safe. On the LAN, HTTP is cleartext on the wire — acceptable inside a
> trusted network, but do not route it over the public internet without TLS.

---

## 3. Two access scenarios

### Scenario A — Caller is on the internal LAN

Nothing to install. Open the UI directly:

```
http://140.113.28.150:3000
```

Make sure the host firewall allows inbound TCP 3000 from the LAN:

```bash
sudo ufw allow from 140.113.0.0/16 to any port 3000 proto tcp   # restrict to campus range
```

### Scenario B — Caller is remote, via Tailscale

1. **Both machines must be in the same tailnet.** On the caller's machine:
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   ```
   Then approve/invite that machine into the tailnet (admin console:
   https://login.tailscale.com/admin/machines). Owner of both nodes is `duong.pt1771@`.

2. The caller uses the Tailscale address — **no firewall changes, no public exposure**:
   ```
   http://100.83.40.102:3000
   # or:  http://user-ws990t.taile0a1fc.ts.net:3000
   ```

3. (Optional) Lock it down further with Tailscale ACLs so only specific tailnet nodes can
   reach port 3000 on this host.

> Tailscale also works when the caller is *also* on the internal LAN — it just routes over
> the mesh. If unsure which network a partner is on, Tailscale is the safe default.

---

## 4. Programmatic API access

For a **human user**, the UI above is enough. For an **external system** that needs an
OpenAI-compatible API, the supported path is **Open WebUI's built-in API**:

1. In Open WebUI: **Settings → Account → API Keys → Create** a key for the caller.
2. The caller points an OpenAI client at Open WebUI:
   ```python
   from openai import OpenAI
   client = OpenAI(base_url="http://100.83.40.102:3000/api", api_key="<webui-api-key>")
   resp = client.chat.completions.create(
       model="ACM Assistant",                       # or another registered model
       messages=[{"role": "user", "content": "Hello"}],
   )
   print(resp.choices[0].message.content)
   ```
3. List models: `GET http://<host>:3000/api/models` with the same `Authorization: Bearer`.

> **Gap to be aware of:** Open WebUI keys are per-user, not the budget/rate-limited virtual
> keys LiteLLM used to provide. If a partner needs **scoped, rate-limited, metered** API keys
> (the old `max_budget` / `rpm_limit` model), reintroduce a gateway in front of vLLM. Do not
> hand out raw access to `:8002` — it is unauthenticated.

---

## 5. Verify connectivity

```bash
# From the caller — checks the proxy/UI is reachable
curl -I http://<host-address>:3000
# expected: HTTP/1.1 200 (or a redirect to the login page)
```

If this fails:
- **Connection refused / timeout** → Caddy/Open WebUI not running, wrong port, or
  firewall/ACL blocking.
- **Reachable on host but not from caller** → firewall (ufw) or, for Tailscale, the caller
  is not in the tailnet.
- **401 / login required** on `/api/...` → no API key, or the key was revoked.

---

## 6. Security checklist

- [x] vLLM (`:8002`) has no published external route; only Caddy/Open WebUI is exposed.
- [ ] Prefer **Tailscale** (encrypted) for anything off-LAN.
- [ ] Firewall scoped to the campus range / tailnet rather than `0.0.0.0/0` where possible.
- [ ] If ever exposed to the public internet, put TLS in front (Caddy on `443`), not plain HTTP.
- [ ] If scoped/metered API keys are needed again, add an authenticated gateway in front of
      vLLM — never expose the raw vLLM port.
