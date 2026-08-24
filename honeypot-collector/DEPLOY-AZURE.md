# Deploying the Honeypot Collector to Azure

A beginner-friendly runbook. If you have never shipped a container before, read
it top to bottom. If you just want the commands, jump to **Step 2**.

---

## The mental model: build locally, ship an image, run the image on Azure

You do **not** edit code on the server. That is the single most important idea
here. Instead:

1. You write and test the code **on your laptop / dev box** until it works.
2. You package it into a **container image** — a frozen, self-contained box that
   holds Python, the dependencies, and `collector.py`, and nothing else.
3. You **run that exact image on Azure**. The server never sees your source code
   or your editor; it just runs the box.

Why this way?

- **Reproducible.** The image that passed your local test is byte-for-byte the
  same image running in production. "Works on my machine" stops being a problem.
- **No live editing.** Nobody SSHes in to hand-patch a running service. To change
  behaviour you build a new image and redeploy. That makes rollbacks trivial (run
  the previous image) and keeps the server boring.
- **Isolation is enforceable.** The image carries no DB drivers and no secrets.
  Secrets are injected at run time as environment variables. If an attacker pops
  this container, there is nothing inside to steal or pivot into — which is the
  entire point of a honeypot.

So the flow is always: **edit → build → test locally → push image → run on Azure.**

---

## Step 1 — LOCAL: build, run, and test

From inside `honeypot-collector/`:

```bash
# 1. Create your real config from the template and fill in SMTP + a secret.
cp .env.example .env
#   Edit .env: set SMTP_USERNAME / SMTP_PASSWORD, and generate a strong secret:
#   openssl rand -hex 32   ->  paste into HONEYPOT_SHARED_SECRET

# 2. Build and start the collector (reads .env automatically).
docker compose up -d --build

# 3. Confirm it is alive (this is NOT a decoy — just liveness).
curl -s http://localhost:8010/healthz
#   expected: {"status":"ok"}
```

### Test the `/collect` webhook (Pattern B — embedded decoy)

This is what the analysis site will call. It must present the shared secret in
the `X-Honeypot-Secret` header. Use the same secret you put in `.env`:

```bash
# Correct secret -> 200 {"ok":true} AND an alert email is sent.
curl -s -X POST http://localhost:8010/collect \
  -H "Content-Type: application/json" \
  -H "X-Honeypot-Secret: <the-secret-from-your-.env>" \
  -d '{
        "token": "analysis.export",
        "source": "analysis-site",
        "ip": "203.0.113.7",
        "method": "GET",
        "path": "/api/v1/internal/export",
        "query": "id=999",
        "user_agent": "curl/8.0",
        "referer": null,
        "supplied_id": "999",
        "api_key_seen": "sk_fake_analysis_key_abc123",
        "authorization_seen": null,
        "body": null,
        "ts_utc": "2026-08-20T04:15:00Z"
      }'
#   expected: {"ok":true}   (and a "[Honeypot] Decoy tripped" email arrives)

# Wrong / missing secret -> 401 and NO email (randoms cannot spam the inbox).
curl -s -X POST http://localhost:8010/collect \
  -H "X-Honeypot-Secret: wrong" -d '{}'
#   expected: {"detail":"Unauthorized"}
```

### Test the hosted decoys (Pattern A — App + payment site)

These look like real business APIs. A request with the **safe id** is treated as
our own camouflage traffic (fake 200, **no** alert). Any other id — or no id —
trips an alert and returns a realistic fake 401.

```bash
# Safe id (default 136017) -> plausible fake 200, NO alert (camouflage).
curl -s "http://localhost:8010/api/v1/client/data?id=136017"
#   expected: {"id":"136017","status":"active","record":"ok"}

curl -s "http://localhost:8010/api/v1/usdt/check?id=136017"
#   expected: {"id":"136017","usdt_verified":true,"status":"ok"}

# Wrong id -> fake 401, AND a trip alert email is sent (source="hosted").
curl -s "http://localhost:8010/api/v1/client/data?id=999"
#   expected: {"detail":"Unauthorized"}

# No id at all -> also a trip.
curl -s -X POST http://localhost:8010/api/v1/usdt/check
#   expected: {"detail":"Unauthorized"}
```

**Confirm the alert email arrives.** Check the inbox for `HONEYPOT_ALERT_TO`
(default `kieran.xiang@kohleservices.com`). The email has a bilingual title line,
dual MT/HK timestamps, the attacker IP called out, and every captured field in a
table. If nothing arrives, read the container logs:

```bash
docker compose logs -f collector
#   Look for HONEYPOT_ALERT_SENT (good) or HONEYPOT_ALERT_FAILED (SMTP problem).
```

A burst of identical probes collapses to **one** email per `COOLDOWN_MINUTES`
(default 5) — that is the dedup working, not a bug.

When local testing passes, stop the stack and move to Azure:

```bash
docker compose down
```

---

## Step 2 — SHIP TO AZURE

You need the Azure CLI once: `az login`, then pick your subscription with
`az account set --subscription "<name-or-id>"`.

Set some names you will reuse (edit to taste — the DNS label must be globally
unique within its region):

```bash
RG=honeypot-rg                 # resource group
LOC=eastasia                   # region close to your users
ACR=kcmhoneypotacr             # ACR name: 5-50 chars, letters/numbers only, GLOBALLY unique
DNS=kcm-honeypot               # DNS label -> kcm-honeypot.eastasia.azurecontainer.io
IMG=honeypot-collector:1.0     # image name:tag

az group create --name $RG --location $LOC
```

### PRIMARY (recommended): Azure Container Registry + Container Instances

Nothing to manage — no VM, no OS patching. Azure builds the image **in the
cloud** straight from your `Dockerfile` (you do not need Docker locally for this,
and there is no local push step).

```bash
# 1. Create a private registry.
az acr create --resource-group $RG --name $ACR --sku Basic --admin-enabled true

# 2. Build the image in the cloud from THIS folder's Dockerfile.
#    Run this from inside honeypot-collector/ (the "." is the build context).
az acr build --registry $ACR --image $IMG .

# 3. Grab the registry login creds (ACI needs them to pull the private image).
ACR_SERVER=$(az acr show   --name $ACR --query loginServer -o tsv)
ACR_USER=$(az acr credential show --name $ACR --query username -o tsv)
ACR_PASS=$(az acr credential show --name $ACR --query "passwords[0].value" -o tsv)

# 4. Run it as a Container Instance.
#    IMPORTANT: SMTP_PASSWORD and HONEYPOT_SHARED_SECRET go in
#    --secure-environment-variables (never shown in `az container show`/logs).
#    The non-secret ones go in plain --environment-variables.
az container create \
  --resource-group $RG \
  --name honeypot-collector \
  --image $ACR_SERVER/$IMG \
  --registry-login-server $ACR_SERVER \
  --registry-username "$ACR_USER" \
  --registry-password "$ACR_PASS" \
  --os-type Linux \
  --cpu 1 --memory 1 \
  --ports 80 443 \
  --dns-name-label $DNS \
  --restart-policy Always \
  --environment-variables \
      SMTP_SERVER=smtp.office365.com \
      SMTP_PORT=587 \
      SMTP_USERNAME=alerts@kohleservices.com \
      HONEYPOT_ALERT_TO=kieran.xiang@kohleservices.com \
      HONEYPOT_SAFE_IDS=136017 \
      COOLDOWN_MINUTES=5 \
  --secure-environment-variables \
      SMTP_PASSWORD='<smtp-password>' \
      HONEYPOT_SHARED_SECRET='<the-long-random-secret>'
```

> Note: the container listens on **8010** internally. The `--ports 80 443` above
> exposes the instance's public ports; the clean way to serve real HTTPS on 443
> is to put a TLS proxy in front (Step 3). If you want ACI to expose 8010
> directly for a first smoke test, change `--ports 8010` and hit
> `http://$DNS.$LOC.azurecontainer.io:8010/healthz` — but do NOT leave plain HTTP
> as the production endpoint (Step 3 explains why).

Find the public DNS name any time:

```bash
az container show -g $RG -n honeypot-collector --query ipAddress.fqdn -o tsv
```

### ALTERNATIVE: a small Azure VM running Docker

Pick this **only if you already have a VM** and want everything in one place;
otherwise ACI is less to babysit (no OS to patch, no SSH to secure). Trade-off:
the VM is a real machine you now own the security of — the opposite of what a
honeypot's blast radius should be, so keep it minimal and separate from anything
internal.

```bash
# On the VM (Ubuntu, Docker + compose plugin installed):
git clone <repo> && cd <repo>/honeypot-collector
#   or: scp -r honeypot-collector user@vm:/opt/honeypot-collector

cp .env.example .env      # fill in SMTP + secret on the VM (never commit it)
docker compose up -d      # same compose file you tested locally
curl -s http://localhost:8010/healthz
```

Open only the ports you actually serve in the VM's Network Security Group, and
never give this VM a route into the internal network.

---

## Step 3 — HTTPS + a real domain

The fake key and decoy URL you plant in the App / payment site must sit behind
**HTTPS** — a `http://` URL, or a raw `...azurecontainer.io:8010` address, looks
fake and will make an attacker suspicious (and some clients refuse plain HTTP).
So terminate TLS in front of the collector. Simplest options, pick one:

- **Cloudflare in front (recommended, least to run).** Point a subdomain (e.g.
  `api-cdn.kohleservices.com`) at the container's public address in Cloudflare
  DNS and let Cloudflare serve HTTPS at the edge. Zero extra containers to
  operate; you also get free edge rate-limiting in front of the honeypot.
  Trade-off: Cloudflare sees the traffic (fine here — no real data flows), and
  you must trust the origin hop; restrict the origin so only Cloudflare can reach
  it.
- **Caddy sidecar (recommended if you want it self-contained).** Add a tiny Caddy
  container in front that auto-provisions a Let's Encrypt certificate and reverse
  proxies to `collector:8010`. One extra container, no manual certs. Great when
  you do not want a third party in the path. Sketch:

  ```
  # Caddyfile
  api-cdn.kohleservices.com {
      reverse_proxy collector:8010
  }
  ```

  Run Caddy + collector together (compose or an ACI container group) so Caddy
  holds 80/443 and the collector stays internal on 8010.
- **ACI + Application Gateway.** Azure-native TLS termination and WAF. Most
  moving parts and most cost — only worth it if you are standardising on App
  Gateway anyway. For a single honeypot it is overkill; prefer one of the above.

**Recommendation:** if the org already uses Cloudflare, front it with Cloudflare
(nothing to run). Otherwise use the Caddy sidecar (self-contained, auto-TLS).

Whatever you choose, the public URL the decoys point at becomes
`https://api-cdn.kohleservices.com/...`.

---

## Step 4 — WIRE THE SYSTEMS

Each real system points at the collector. The golden rule: **every system gets a
DISTINCT fake key**, so when a trip fires you instantly know WHICH system was
breached (the key/token travels into the alert email).

- **Analysis site — Pattern B (embedded decoy).** The analysis backend already
  detects a probe inside its own code, then reports it by POSTing to
  `https://api-cdn.kohleservices.com/collect` with the header
  `X-Honeypot-Secret: <shared-secret>` and a JSON body (`token`, `source`, `ip`,
  `method`, `path`, ... — see the curl in Step 1). Use a source/token unique to
  the analysis site, e.g. `"source":"analysis-site"`, `"token":"analysis.export"`.

- **App — Pattern A (hosted decoy).** The App cannot easily change its backend,
  so plant a distinct **fake API key** and a URL pointing at a decoy hosted here,
  e.g. a fake key `app_live_key_7f3c...` next to
  `https://api-cdn.kohleservices.com/api/v1/client/data`. Any attacker who
  extracts the key and calls the URL with a non-safe id trips the alert.

- **Payment site — Pattern A (hosted decoy).** Same idea, a **different** fake key
  `pay_live_key_9a1d...` next to
  `https://api-cdn.kohleservices.com/api/v1/usdt/check`.

Because the App and payment site use different decoy paths (and you should give
them different fake keys), a trip's `token` (`client.data` vs `usdt.check`) and
key tell you which one leaked. For Pattern B, the `source` field does the same
job.

---

## Step 5 — OPERATE

- **Rotate the shared secret.** Generate a new one (`openssl rand -hex 32`),
  update the embedded decoy(s) that call `/collect`, then redeploy the collector
  with the new value:

  ```bash
  az container create ... --secure-environment-variables HONEYPOT_SHARED_SECRET='<new>' ...
  #   (re-run the same create command; it replaces the instance)
  ```

  Update the decoy and the collector close together — a mismatch means real trips
  get 401'd and no email fires.

- **Add a safe id.** Extend the comma-separated list and redeploy:

  ```bash
  ... --environment-variables HONEYPOT_SAFE_IDS=136017,204882 ...
  ```

- **Where the logs are.**

  ```bash
  az container logs -g honeypot-rg -n honeypot-collector          # dump
  az container logs -g honeypot-rg -n honeypot-collector --follow # tail
  ```

  Grep-able tokens: `HONEYPOT_TRIP` (a probe hit), `HONEYPOT_ALERT_SENT` (email
  went out), `HONEYPOT_ALERT_FAILED` (SMTP problem — check creds),
  `HONEYPOT_TRIP_SUPPRESSED` (dedup collapsed a burst),
  `HONEYPOT_CAMOUFLAGE` (safe-id traffic, correctly no alert).

- **The isolation rule (non-negotiable).** Never give this container database
  credentials, internal-network routes, VPN access, or any internal secret. Its
  only job is receive → dedup → email, and its only outbound need is SMTP. If it
  is ever compromised, the correct blast radius is "an attacker can send emails
  from the alert mailbox" — nothing more. Keep it that way.

---

## Security notes

> - **Keep the shared secret out of any client bundle.** `HONEYPOT_SHARED_SECRET`
>   belongs only in the analysis site's *backend* (Pattern B). It must never ship
>   in a frontend/App bundle — anything in a client can be extracted.
> - **The fake keys are meant to leak — the real secret is not.** Pattern A's
>   fake API keys are bait; giving each system its own tells you who was breached.
>   The `/collect` shared secret is real auth and must stay server-side.
> - **In production, send the safe id in a header or body, not the URL query.**
>   URL query strings leak into proxy/CDN/access logs; a safe id sitting in
>   `?id=136017` could end up logged somewhere and burn the camouflage. The demo
>   uses `?id=` for easy testing; in production prefer a header/body-carried id.
> - **Isolation, again:** no DB creds, no internal network. If you are ever
>   tempted to "just let it read one table," don't — that is the exact pivot a
>   honeypot exists to deny.
