# YantrAI Remote Agent Integration Spec (v1)

How any web app — Python, Node, Go, anything — becomes a remote agent embedded in
the YantrAI store. Python apps should use [`yantrai-sdk`](../../yantrai_sdk/README.md),
which implements everything below. Other stacks implement this spec directly.

A remote agent must do three things: **accept SSO**, **be iframe-embeddable**, and
(if chargeable) **report usage**.

---

## 0. Registration & credentials

Register the app in the Developer Portal (or, for first-party, via
`backfill_publish_l2_agents.py`). You receive:

| Field | Use |
|-------|-----|
| `client_id` (`cid_…`) | identifies your app; sent as the token `kid` claim |
| `signing_key` (`sk_…`) | HMAC secret — verify inbound tokens & sign usage tokens |
| `remote_url` | the HTTPS URL the platform iframes |

Keep `signing_key` server-side only. The platform stores the same value and signs
your users' SSO tokens with it.

---

## 1. Token format

`token = base64url(payload_json) + "." + base64url(HMAC_SHA256(signing_key, base64url(payload_json)))`

- JSON is compact (`separators=(",",":")`), base64url is **unpadded**.
- The signed input is the base64url payload **string** (not the raw JSON).

**Claims**

| Claim | Meaning |
|-------|---------|
| `u` | username |
| `c` | company / workspace name |
| `exp` | expiry, unix seconds (platform SSO tokens live **300s**) |
| `kid` | your `client_id` (present on app-scoped tokens) |

Verify = recompute the HMAC, `compare_digest` against the signature, then reject if
`exp < now`.

---

## 2. SSO (sign-in)

On the first iframe load the platform appends the user's token to your URL:

```
https://your-app.example.com/?token=<token>
```

1. Read `token` from the query string.
2. Verify it with your `signing_key` (§1). Reject if invalid/expired.
3. Establish your own session (e.g. a cookie) so later in-iframe navigation stays
   authed. The session cookie **must** be `SameSite=None; Secure` (it's a
   cross-origin iframe) — otherwise the browser drops it.

> Don't trust the raw query string without verifying the signature.
> Alternatively (no shared secret) call `GET {PLATFORM}/api/agents/verify-sso?token=…`
> → `{ "ok": true, "username", "company_name" }`.

---

## 3. Iframe embedding

The platform renders you in an `<iframe>`. Your responses must:

- **not** send `X-Frame-Options: DENY|SAMEORIGIN`, and
- send `Content-Security-Policy: frame-ancestors 'self' https://workspace.yantrailabs.com`.

---

## 4. Usage / billing (chargeable agents)

To charge credits, POST to the platform after a billable action:

```
POST {PLATFORM}/api/agents/usage
Content-Type: application/json
{
  "token": "<app-scoped token you sign: claims u,c,kid,exp>",
  "tokens": 1000,
  "action": "trench_inspection",
  "model": "your-model",
  "prompt_tokens": 0,
  "output_tokens": 0
}
```

- Mint the `token` yourself: sign `{u,c,kid:client_id,exp:now+300}` with your
  `signing_key` (§1). The platform resolves `kid`→your app and `c`→the workspace,
  then debits `tokens` credits and attributes them to your agent.
- Response: `{ "ok": true, "agent": "<slug>", "charged": N, "balance": <new_balance> }`.
- **Sandbox:** add `"dry_run": true` to validate + compute the charge **without**
  debiting (use the portal's "Test charge" button during integration).

Charge once per real unit of value (one inspection, one completed call) — not per
internal HTTP request.

---

## 5. Endpoints reference

| Endpoint | Who calls it | Purpose |
|----------|--------------|---------|
| `GET /api/agents/sso-token?username&company_name&slug` | platform UI | mints the user token it appends to your `remote_url` |
| `GET /api/agents/verify-sso?token=` | your app (optional) | server-side token check, no shared secret |
| `POST /api/agents/usage` | your app | report usage / charge (supports `dry_run`) |

## 6. Errors

| Code | Meaning |
|------|---------|
| `401` | token invalid or expired |
| `400` | token not scoped to a registered app (missing/unknown `kid`) or bad `tokens` |
| `404` | workspace could not be resolved for billing |

---

## Node / Next.js reference (SSO verify + usage)

```js
import crypto from "node:crypto";

const b64u = (b) => Buffer.from(b).toString("base64url");
const KEY = process.env.YANTRAI_SIGNING_KEY;
const CLIENT_ID = process.env.YANTRAI_CLIENT_ID;
const PLATFORM = process.env.YANTRAI_PLATFORM_URL ?? "https://workspace.yantrailabs.com";

export function verify(token) {
  const [body, sig] = (token || "").split(".");
  if (!body || !sig) return null;
  const expected = b64u(crypto.createHmac("sha256", KEY).update(body).digest());
  if (!crypto.timingSafeEqual(Buffer.from(sig), Buffer.from(expected))) return null;
  const p = JSON.parse(Buffer.from(body, "base64url").toString());
  return p.exp < Math.floor(Date.now() / 1000) ? null : p; // {u,c,exp,kid}
}

export async function reportUsage(user, tokens, action, model) {
  const payload = { u: user.u, c: user.c, kid: CLIENT_ID,
                    exp: Math.floor(Date.now() / 1000) + 300 };
  const body = b64u(JSON.stringify(payload));
  const sig = b64u(crypto.createHmac("sha256", KEY).update(body).digest());
  await fetch(`${PLATFORM}/api/agents/usage`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token: `${body}.${sig}`, tokens, action, model }),
  });
}
```

For iframe embedding in Next.js, set the CSP header in `next.config.js` `headers()`
(`frame-ancestors 'self' https://workspace.yantrailabs.com`) and ensure session
cookies are `SameSite=None; Secure`.
