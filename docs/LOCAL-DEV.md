# Local Docker Development

Moved out of `CLAUDE.md` on 2026-11-04 so it loads only when needed. `CLAUDE.md` keeps the rules that apply everywhere: iterate locally, staging for the final pass, never `flask db migrate` locally.

The project can be run locally via Docker Compose. This mirrors the staging/production stack (PostgreSQL, Redis, Flask, Celery, React) without needing to install dependencies on the host.

**Start dev environment**: `make dev` (or `make dev-build` to force rebuild)
**Stop**: `make stop`
**Logs**: `make logs`
**Shell into backend**: `make shell`

The dev stack runs:
- Backend on `http://localhost:5010` (Flask dev server with hot reload)
- Frontend on `http://localhost:3001` (React dev server with HMR)
- PostgreSQL, Redis, and Celery are internal to the Docker network
- **No Celery beat by default**: `celery-beat` sits behind the Compose profile `beat` in `docker-compose.override.yml`, so no periodic job (profile updates, recent-context summaries, embedding sweeps, digests, ...) runs locally. The worker still runs, so jobs you trigger (replies, transcription, TTS) work. To run beat, set `COMPOSE_PROFILES=beat` in `.env` (or `docker compose --profile beat up`); to fire one job, `.delay()` it from a flask shell.

Source code is volume-mounted, so edits are reflected immediately without rebuilding.

**Iterative development happens HERE, not on staging** (workflow decision 2026-07-02): staging deploys cost 6–15 min + a DB wipe, local is hot-reload. Use staging only for the final pre-review pass (real TLS/nginx topology, wiped-DB onboarding, deploy pipeline). Practical notes:
- Dark feature flags (`SHARE_V1`, etc.) go in the local uncommitted `.env` — the compose files pass them through.
- Schema drift after model changes: sync additively with `db.create_all()` in a container Python shell. Do NOT run `flask db migrate` locally — deploy auto-generates and commits migrations, and local ones collide.
- Login without SMTP: mint a magic link inside the backend container with `token = make_link_token(email)` (optional `next_url`, `max_age`) from `backend.utils.sign_in_links`, which stores the link's row for the account that holds the address, and open `http://localhost:5010/auth/magic-link/verify?token=…`. A token made with `generate_magic_link_token` alone, or by setting `user.magic_link_token_hash`, does not sign in. `ios/scripts/local_backend.sh magic-link` does this for the iOS test user. The `sign_in_link` table comes with `db.create_all()`.
- After `package.json` changes: FIRST `docker compose build frontend` (bakes the new deps into the image), THEN `docker compose up -d --force-recreate --renew-anon-volumes frontend`. Renewal without the rebuild resets `node_modules` to the OLD image's state and the dev server fails with "Module not found" (a bare `docker compose exec frontend npm install <pkg>` patches the running container, but the anon volume loses it on the next recreate)..
- Unset env vars reach containers as EMPTY STRINGS (not absent) — guard numeric parses with `or "default"`, not a `get()` default.
- Dev API calls are SAME-ORIGIN through the CRA proxy (`src/setupProxy.js`, target `PROXY_TARGET` — `http://backend:5010` in Docker). `frontend/.env.development` keeps `REACT_APP_API_URL`/`BACKEND_URL` EMPTY on purpose; absolute URLs there force CORS requests the backend doesn't serve.

**Important Docker Compose notes**:
- Ports are NOT defined in the base `docker-compose.yml` — they live only in the override files (`docker-compose.override.yml` for dev, `docker-compose.staging.yml` for staging). This prevents Docker Compose from concatenating port arrays across files.
- Backend code is mounted at `/app/backend/` inside containers to match the project-root import layout (`from backend.X import Y`).
- The `migrations/` directory is at the project root, outside the backend Docker build context. For staging, it's volume-mounted into the container.
