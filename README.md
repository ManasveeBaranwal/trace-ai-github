# Trace AI · SustivioLabs

A FastAPI workspace for boutique sustainability firms: client companies and reporting years, source owners, datasets and requests, evidence storage, review and calculations, BRSR preparation, and source-linked AI assistance.

This is a self-contained deployment copy of the working local product. No existing account databases, uploaded evidence, backups, credentials, or private research files are included.

## Free public demo

`render.yaml` selects **Free** compute, with no paid disk, database or hosted AI. Keep the Render workspace on Hobby and leave payment details unset. Render suspends free services or stops new builds at the applicable limits when no payment method is on file; see [the current free-plan rules](https://render.com/docs/free).

The public demo contains fictional records only. Visitors can explore clients, reporting years, source evidence and reports, and ask source-retrieval questions. Editing, uploads, approvals, account creation, collection links and reminders are disabled. No founder shortcut or real company data is exposed. Inference is disabled in this mode, including when `OLLAMA_HOST` is configured.

Free services sleep after 15 minutes of inactivity, can take about a minute to wake, and lose local files on restart or redeploy. This demo reseeds its fictional data when necessary. It is suitable for reviewers; keep editable real records in the local application. Free quotas can also suspend availability. The demo is not an always-on production service.

The container uses `cloud_server.py` to select `public_demo_server.load_public_demo()` only when `TRACE_PUBLIC_DEMO=1`. Ordinary startup retains account authentication. The public-demo data directory must be dedicated to the demo; it refuses existing non-demo records.

## Local boutique sample

Use Python 3.13 or newer. From this repository folder:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
./run-boutique.sh
```

Open http://localhost:8004/boutique; click **Open boutique workspace** if the entry screen appears. This loopback-only sample creates fictional records in `data/boutique-preview`. Press Control+C to stop; run the same command to resume with saved sample data.

Frontend: `static/boutique.html`, `static/boutique.css`, `static/boutique.js`. Backend: `app.py`, `boutique.py`, `boutique_ai.py`. `legacy_backend.py` bundles the extraction dependency; an explicit `SUSTIVIO_BACKEND` can override it.

## Authenticated application

Create the first founder interactively. Password input is hidden; there are no default production credentials.

```sh
python manage.py
./run.sh
```

Open http://127.0.0.1:8002/login. The founder creates a consulting firm and its manager through the platform account tools. An authenticated firm manager lands in the boutique workspace. Reviewer accounts must be provisioned before sharing a hosted login URL.

## Authenticated Docker hosting

The Dockerfile starts the frontend and backend on `0.0.0.0:$PORT` with one worker and login required.

```sh
docker build -t trace-ai .
docker volume create trace-data
docker run --rm -p 8000:8000 --name trace-ai -v trace-data:/var/lib/trace-ai trace-ai
```

In a second terminal, provision the founder on the same mounted database:

```sh
docker exec -it trace-ai python manage.py
```

Open http://localhost:8000/login. Keep passwords out of source and command arguments. `.env.example` documents configuration but is not automatically loaded. For HTTPS hosts set `TRACE_COOKIE_SECURE=1`, configure explicit comma-separated `TRACE_ALLOWED_HOSTS`, and configure `FORWARDED_ALLOW_IPS` for your trusted reverse proxy so origin/CSRF checks see the correct HTTPS scheme.

The checked-in Render blueprint is for the free fictional demo above. Authenticated production hosting needs separate durable storage and explicit approval of any provider costs. Render's `RENDER_EXTERNAL_HOSTNAME` is automatically added to trusted hosts; add custom domains explicitly through `TRACE_ALLOWED_HOSTS`. The blueprint trusts forwarded headers through Render's managed HTTPS proxy; do not reuse its wildcard `FORWARDED_ALLOW_IPS` on a directly exposed container. Provision the first founder with `python manage.py` against the same database before sharing an authenticated installation. Retain `TRACE_REQUIRE_LOGIN=1`, `TRACE_TWO_ACCESS=0`, and one instance.

GitHub Pages cannot run the Python backend. A private source repository can support a publicly reachable website protected by account login. No anonymous hosted founder or manager shortcut is enabled.

## Storage and AI

`TRACE_DATA` stores SQLite databases and evidence. Use a durable writable volume and back it up separately; ephemeral hosting storage loses data on replacement. This MVP uses one application worker.

Local AI needs Ollama running with `qwen3-vl:4b`. `OLLAMA_HOST` defaults to `http://127.0.0.1:11434`. A hosted container cannot reach your Mac's Ollama through its own localhost. Configure a reachable private hosted Ollama service for generation and extraction; do not expose an unauthenticated Ollama endpoint publicly. Without a model, boutique assistance displays a truthful source-retrieval fallback; bill AI extraction cannot complete.

AI reads scoped structured records and coefficient knowledge with source cards. It proposes actions and does not independently approve records or send messages. Reminder controls create drafts; email/WhatsApp delivery is not integrated. Evidence files are stored, but a general full-document vector index over every upload is not implemented. Reports require human review for completeness and regulatory suitability.

## Tests

```sh
python -m unittest discover -s tests -p 'test_*.py'
```

Tests use temporary fixture databases. Hosting references: [Render blueprints](https://render.com/docs/blueprint-spec), [environment variables](https://render.com/docs/environment-variables), [persistent disks](https://render.com/docs/disks).
