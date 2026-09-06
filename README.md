# OpenPing

Free, self-hosted quote open tracker for small business owners. Upload a PDF or HTML quote, share the tracked link, get pinged when it is opened, and approve one follow-up draft. No multi-day drip.

No signup. No license. One Docker Compose service and a SQLite file. About 15 minutes on a 1GB VPS.

## What it does

- Upload a quote (PDF or HTML, max 10 MB), get an unguessable tracked link `{PUBLIC_BASE_URL}/v/{token}`
- Copy link / copy suggested send email (templates, no LLM), mark sent manually, or email the link once if SMTP is set
- First non-bot open: mark opened, ping the owner (UI always; email if SMTP + `OWNER_NOTIFY_EMAIL`), queue **exactly one** follow-up draft
- Edit / Copy / Approve & send (SMTP) / Mark sent / Skip — human approve only, never auto-send
- `GET /health` → HTTP 200 `{"status":"ok","smtp_configured":false,"llm_configured":false}` even when SMTP/LLM unset

Without SMTP you still get in-app open badges and can copy the follow-up draft. Approve & send stays hidden/disabled until `SMTP_HOST` is set.

## Privacy

Self-hosted. You run the box; the owner is the data controller for prospect emails and quote files. No third-party analytics SaaS. Tracking is the act of loading your own `/v/{token}` (and optional `/t/{token}.gif` for HTML).

## 15-minute Ubuntu VPS install

Documented on **Ubuntu 22.04 / 24.04**. About 15 minutes.

**Debian 13:** do **not** run the Ubuntu `docker-ce` recipe below on Debian. Use the distro packages instead:

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose
sudo usermod -aG docker "$USER"
```

Log out and back in (or `newgrp docker`). On Debian, start the stack with `docker-compose` (hyphen) if `docker compose` is not available.

**Amazon Linux:** not documented yet. Use Ubuntu or Debian.

### 1. Install Docker Engine and the Compose plugin (Ubuntu only)

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo ${UBUNTU_CODENAME:-$VERSION_CODENAME}) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker "$USER"
```

Log out and back in (or run `newgrp docker`) so `docker` works without `sudo`.

### 2. Clone, configure, start

```bash
git clone https://github.com/aidendify/openping.git
cd openping
cp .env.example .env
```

Edit `.env` and set at least `BUSINESS_NAME`, `PUBLIC_BASE_URL`, `SECRET_KEY`, and `OWNER_PASSWORD`. Leave `SMTP_*`, LLM keys, and `MARKETING_URL` empty unless configured. Set `OWNER_PASSWORD` on any VPS reachable from the internet (empty means the admin UI is open).

```bash
docker compose up --build -d
```

(On Debian, `docker-compose up --build -d` if the Compose plugin is not installed.)

The app binds `0.0.0.0:8080` in the container. Compose maps host `8080:8080`. SQLite lives on the `openping-data` volume at `/data/openping.db`; uploads at `/data/uploads`.

### 3. Smoke test

Use this `.env` for a first pass (Verifier values). Production should use a real `SECRET_KEY` and `OWNER_PASSWORD`. Do not bake these test passwords as production defaults.

```
OWNER_PASSWORD=testpass
PUBLIC_BASE_URL=http://localhost:8080
BUSINESS_NAME=Harbor HVAC
MARKETING_URL=
SECRET_KEY=change-me
OWNER_NOTIFY_EMAIL=
```

Leave all `SMTP_*` unset and do not set `OPENAI_API_KEY` / `LLM_API_KEY`.

1. Healthcheck:

   ```bash
   curl -sf http://localhost:8080/health
   ```

   Expected: JSON containing `"status":"ok"`, `"smtp_configured":false`, `"llm_configured":false`, HTTP 200.

2. Open http://localhost:8080, log in with `testpass`, create a quote uploading `sample-quote.pdf`, copy the `/v/{token}` link.

3. Open the tracked link (second browser or curl with a normal User-Agent):

   ```bash
   curl -sS -A "Mozilla/5.0" -o /dev/null -w "%{http_code}\n" http://localhost:8080/v/{token}
   ```

   Expected: HTTP 200. Owner UI shows status `opened` or `followup_queued`, open count ≥ 1, and exactly one follow-up draft.

4. A second open must not create a second follow-up row. Approve & send is disabled without SMTP; **Mark sent** sets the follow-up to sent.

## Configuration

Copy `.env.example` to `.env` before `docker compose up`. Variables:

| Variable | Purpose |
| --- | --- |
| `PORT` | Documented as 8080. The container always binds gunicorn to `0.0.0.0:8080`. |
| `DATABASE_PATH` | SQLite file. Compose overrides this to `/data/openping.db`. |
| `UPLOAD_DIR` | Quote files. Compose overrides this to `/data/uploads`. |
| `SECRET_KEY` | Flask session key. Change it on a public VPS. |
| `OWNER_PASSWORD` | Admin login. Empty = open admin (local/dev). Set this on any internet-reachable VPS. |
| `BUSINESS_NAME` | Used in drafts and optional viewer chrome. |
| `PUBLIC_BASE_URL` | No trailing slash. Used in tracked links, e.g. `http://localhost:8080`. |
| `FROM_NAME`, `FROM_EMAIL` | Sign-off and SMTP From. |
| `OWNER_NOTIFY_EMAIL` | Open-alert destination when SMTP is set. Empty → in-app only. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_TLS` | Optional send. If `SMTP_HOST` is unset, send buttons are hidden/disabled. |
| `OPENAI_API_KEY` or `LLM_API_KEY` + `LLM_BASE_URL` + `LLM_MODEL` | Optional follow-up body only; template fallback if unset or call fails. |
| `MARKETING_URL` | If set, footer link **Powered by OpenPing** points here. If unset, there is no footer. |

Do not commit `.env`. SMTP passwords, `OWNER_PASSWORD`, and API keys are never written to application logs.

## Healthcheck

`GET /health` → HTTP 200:

```json
{"status":"ok","smtp_configured":false,"llm_configured":false}
```

`smtp_configured` is `true` only when `SMTP_HOST` is set. `llm_configured` is `true` when `OPENAI_API_KEY` or (`LLM_API_KEY` + `LLM_BASE_URL`) is set. Health succeeds even when both are unset. This route never requires login.

## Local development (optional)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_PATH=./openping.db
export UPLOAD_DIR=./uploads
python app.py
```

Then open http://localhost:8080. This path is for hacking on the code; the supported install is Docker Compose.

## What this is not

OpenPing is **not** a Nudge-style multi-day drip (no Day 0/3/7 sequence). It is **not** AfterJob (no CSAT / Google review ask). It is **not** FormFirst (no contact-form webhook).

One tracked quote open → one owner ping → one approve-to-send follow-up. No SMS, Stripe, e-sign, CRM sync, or second Compose service.
