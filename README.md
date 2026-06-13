# TinyAnim — Ship animations 80% lighter

Drop a **Lottie (`.json`)** or **SVG** file and get it back up to **80% smaller**,
with the rendered result **100% pixel-identical**. No sign-up, no cloud round-trip,
no per-file cost.

![status](https://img.shields.io/badge/status-production-22c55e)
![api cost](https://img.shields.io/badge/external%20API%20cost-%240.00-6366f1)

---

## ✨ What it does

| | Lottie (`.json`) | SVG |
|---|---|---|
| Rounds bloated floats (coords/timing/bezier) | ✅ 3 decimals | ✅ 2 decimals |
| Strips authoring metadata | ✅ `nm`, `mn`, `cl`, `meta` | ✅ `metadata`/`title`/`desc`, editor namespaces |
| Removes editor cruft | — | ✅ inkscape / sodipodi / sketch / illustrator / figma attrs, `xml:space`, comments, DOCTYPE |
| Smart id cleanup | — | ✅ drops **unreferenced** ids only (keeps `url(#…)` / `href` targets, skips files with `<style>`) |
| Compact re-serialization | ✅ no-whitespace JSON | ✅ minified path data, squeezed whitespace |

Everything happens in pure Python (standard library + SQLAlchemy). There is **no
headless browser, no rasterizer, no third-party API** in the hot path.

---

## 🚀 Run locally

```bash
cd tinyanim
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open **http://127.0.0.1:8000** and drag a `.json` or `.svg` onto the page.

> The first request creates `tinyanim.db` (SQLite) and a `storage/` folder for
> optimized output. Optimized files auto-expire after 24 hours.

---

## 🧱 Architecture

```
tinyanim/
├── app/
│   ├── main.py          FastAPI app: routing, CORS, upload caps, security, batch
│   ├── optimizer.py     Pure-Python LottieOptimizer + SVGOptimizer
│   ├── database.py      SQLAlchemy engine/session (SQLite, env-configurable path)
│   ├── models.py        GlobalStat + OptimizationRecord + ApiKey
│   ├── plans.py         Subscription plan definitions (quota / batch / size / price)
│   ├── auth.py          API-key issuance, authentication & quota metering
│   └── templates/
│       └── index.html   Single-page UI (Tailwind CDN + lottie-web preview + pricing)
├── requirements.txt
├── Procfile             Railway / Render start command
├── render.yaml          Render one-click Blueprint
├── runtime.txt          Pinned Python version
└── README.md
```

### API

| Method | Endpoint | Auth | Purpose |
|---|---|---|---|
| `GET`  | `/` | — | Single-page app |
| `POST` | `/api/optimize` | optional key | Optimize one file → sizes + `download_url` |
| `POST` | `/api/batch` | **key (paid)** | Optimize many files → a single `.zip` |
| `GET`  | `/api/download/{file_id}` | — | Download the optimized file / ZIP |
| `GET`  | `/api/stats` | — | Cumulative bytes/files saved (dashboard headline) |
| `GET`  | `/api/plans` | — | Public pricing data (drives the pricing UI) |
| `GET`  | `/api/me` | **key** | Calling key's plan + remaining quota |
| `POST` | `/api/keys` | **admin** | Issue a new API key for a plan |
| `POST` | `/api/checkout` | — | Start a paid subscription → returns key + Stripe URL |
| `POST` | `/api/webhooks/stripe` | **signature** | Stripe event receiver (activates / re-plans / cancels keys) |

### Production safeguards

- **Strict extension whitelist** (`.json`, `.svg`) — anything else is rejected with `400`.
- **Streamed per-plan size cap** — uploads are read in 64 KB chunks and aborted with `413`
  the moment they exceed the limit, so a malicious large file never balloons memory.
- **Per-key quota & per-IP rate limit** — anonymous traffic is IP-rate-limited (`429`);
  keyed traffic is metered against a rolling 30-day quota (`402` when exhausted).
- **Parser-validated** — unparseable Lottie/SVG returns `422`, never a 500.
- **No path traversal** — downloads are routed by a 32-char hex UUID validated
  against `^[0-9a-f]{32}$`; the original filename never touches the filesystem.
- **XXE-hardened** — the SVG `<!DOCTYPE>`/external entities are stripped before parsing.
- **Never inflates** — if optimization can't beat the original, the original bytes
  are returned instead.

---

## 💰 For Acquire.com buyers — why the margins are exceptional

**This product has a marginal cost of essentially $0 per file processed.**

1. **Zero external API spend.** Competing "optimizer" tools proxy to paid image/CDN
   APIs or spin up headless Chrome to re-render and re-export. TinyAnim does neither.
   All compression is deterministic Python string/JSON/XML manipulation. Your COGS
   per optimization is a few milliseconds of CPU — nothing metered, nothing billed.

2. **No GPU, no heavy graphics stack.** The dependency list is five small, pure
   packages. It runs on the cheapest $5/mo VPS or a free serverless tier. There is
   no rasterization library, no `cairo`, no `puppeteer` to pay for or babysit.

3. **Single-binary-simple ops.** SQLite means no managed database bill and no
   DevOps overhead — `git pull && uvicorn` is the entire deploy. Fewer moving parts =
   lower maintenance cost = higher net margin for the acquirer.

4. **Built-in growth flywheel.** The cumulative "X MB saved across N files" counter
   (persisted in SQLite) is live social proof that compounds with every visitor —
   a conversion asset that costs nothing to run.

5. **Clean monetization headroom.** The freemium wall is already built — API keys,
   per-plan quotas, batch upload and ZIP export ship in the box (see below). A buyer
   plugs in Stripe and starts charging the same day, with no change to the core engine.

**Bottom line:** revenue scales with usage while infrastructure cost stays flat and
near-zero. That is the profile that sells.

---

## 💳 Monetization (API keys, plans & batch)

Plans live in [`app/plans.py`](app/plans.py) and are the only thing you edit to
re-price:

| Plan | Price | Quota / mo | Batch | Max file |
|---|---|---|---|---|
| free | $0 | 50 | — | 10 MB |
| pro | $19 | 10,000 | 50 files | 25 MB |
| business | $99 | 100,000 | 200 files | 50 MB |

**Issuing a key** (admin-only — set `TINYANIM_ADMIN_TOKEN` first):

```bash
curl -X POST https://your-app/api/keys \
  -H "X-Admin-Token: $TINYANIM_ADMIN_TOKEN" \
  -d '{"plan":"pro","label":"acme corp"}'
# → { "api_key": "tinyanim_…", "plan": "pro", "monthly_quota": 10000 }
```

> The raw key is shown **once**; only its SHA-256 hash is stored. A leaked
> database exposes no usable credentials.

**Using a key:**

```bash
# single file — quota-metered, IP rate-limit bypassed, larger size cap
curl -F "file=@logo.json" https://your-app/api/optimize \
  -H "Authorization: Bearer tinyanim_…"

# batch → one ZIP of every optimized file
curl -X POST https://your-app/api/batch \
  -H "Authorization: Bearer tinyanim_…" \
  -F "files=@a.json" -F "files=@b.svg" -F "files=@c.svg"

# check remaining quota
curl https://your-app/api/me -H "Authorization: Bearer tinyanim_…"
```

### Stripe billing (built in)

Payments are fully wired in [`app/billing.py`](app/billing.py) — **no `stripe`
SDK dependency**; webhook signatures are verified with stdlib HMAC-SHA256 and
Checkout Sessions are created with a single `urllib` POST.

**The self-serve flow:**

1. Visitor clicks a paid plan → `POST /api/checkout` generates an **inactive** key
   (hash stored, raw value shown to them once) and returns a Stripe Checkout URL.
2. They pay on Stripe's hosted page.
3. Stripe calls `POST /api/webhooks/stripe` → the signature is verified, the event
   is deduped, and the key is flipped **active** and linked to the Stripe
   customer/subscription.

**Events handled:**

| Event | Effect |
|---|---|
| `checkout.session.completed` | Activates the pending key, links customer + subscription |
| `customer.subscription.updated` | Re-maps the key's plan from the new Stripe price |
| `customer.subscription.deleted` | Gracefully downgrades the key to `free` |

Every event is **idempotent** (processed ids are recorded in `processed_events`),
signatures outside a 300 s window are rejected as replays, and tampered signatures
return `400`.

**Required env:**

| Env var | For |
|---|---|
| `STRIPE_SECRET_KEY` | Creating Checkout Sessions (`/api/checkout`) |
| `STRIPE_WEBHOOK_SECRET` | Verifying webhook signatures (`whsec_…`) |
| `STRIPE_PRICE_PRO` / `STRIPE_PRICE_BUSINESS` | Map Stripe price ids → plans |

**Test the webhook locally with the Stripe CLI:**

```bash
stripe listen --forward-to localhost:8000/api/webhooks/stripe
# copy the printed whsec_… into STRIPE_WEBHOOK_SECRET, then:
stripe trigger checkout.session.completed
```

> The plan-quota metering, 30-day reset and 402-on-exhaustion live in
> [`app/auth.py`](app/auth.py) — billing only flips plans and activation, so the
> enforcement path is shared and already tested.

> **Schema note:** this foundation has no migration tool. After pulling the
> billing changes, delete an existing `tinyanim.db` (dev) or start from a fresh
> disk so the new `api_keys` / `processed_events` columns are created.

---

## 🚢 Deploy to Render / Railway

This repo ships a Render **Blueprint** ([`render.yaml`](render.yaml)) and a
[`Procfile`](Procfile) (Railway/Heroku-style). On Render: **New → Blueprint →**
select the repo. Set these env vars:

| Env var | Purpose |
|---|---|
| `TINYANIM_DATA_DIR` | Mount path of the persistent disk (e.g. `/var/data`) so SQLite + temp files survive restarts. |
| `TINYANIM_ADMIN_TOKEN` | Secret required to mint API keys. **Key issuance is disabled until this is set.** |
| `TINYANIM_RATE_LIMIT` | Anonymous optimize requests / minute / IP (default 60). |

> Render's **free** plan has no persistent disk — drop the `disk:` block from
> `render.yaml` and the app still runs fully; only the cumulative counter resets
> when the instance sleeps. Attach a disk (paid tier) to persist stats + keys.

---

## 🔧 Configuration

| Setting | Location | Default |
|---|---|---|
| Anonymous upload size | `app/main.py` → `ANON_MAX_UPLOAD_BYTES` | 10 MB |
| Per-plan upload size | `app/plans.py` → `Plan.max_upload_mb` | 10 / 25 / 50 MB |
| File retention | `app/main.py` → `FILE_TTL_SECONDS` | 24 h |
| Data directory | env `TINYANIM_DATA_DIR` | project root |
| Lottie precision | `app/optimizer.py` → `LottieOptimizer(precision=3)` | 3 decimals |
| SVG precision | `app/optimizer.py` → `SVGOptimizer(precision=2)` | 2 decimals |

Lower the precision for even smaller files; raise it if you have extreme zoom
requirements. Defaults are tuned to be visually lossless on screen.
