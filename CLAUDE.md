# Claude Code Instructions for Write-or-Perish

## CI/CD Pipeline - IMPORTANT

**Pushing to `main` branch triggers automatic deployment to production.**

The project uses GitHub Actions for CI/CD with direct deployment to production:

### Workflow Overview

1. **CI (`ci.yml`)** - Runs on PRs only (not on push):
   - Uses `dorny/paths-filter` to skip unchanged areas (backend vs frontend)
   - Backend: Python linting (flake8), pytest tests
   - Frontend: npm lint, jest tests, production build
   - Security: bandit scan, dependency vulnerability check (safety)

2. **Deploy to Production (`deploy.yml`)** - Runs ONLY on pushes to `main`:
   - Runs backend + frontend tests first, then deploys
   - Builds frontend with production URLs (`https://loore.org`)
   - Deploys to production VM via SSH
   - Pulls latest backend code on server
   - Runs `deploy.sh` to restart systemd services

3. **Deploy to Staging (`deploy-staging.yml`)** - Runs on pushes to `staging` (+ manual dispatch):
   - Builds frontend in GitHub Actions (not on VM — the VM runs out of memory during `npm ci`)
   - Frontend is built with staging URLs (`https://staging.loore.org`) and uploaded as a tarball via SCP
   - On the VM: `git reset --hard origin/staging`, extract frontend to `frontend/build-staging/`
   - Staging runs as Docker Compose project `wop-staging` using three compose files: `docker-compose.yml` + `docker-compose.prod.yml` + `docker-compose.staging.yml` with `--env-file .env.staging`
   - Frontend uses `nginx:alpine` image with pre-built assets volume-mounted (no Docker build)
   - Staging DB is ephemeral: schema is dropped and recreated each deploy, then `flask init-db`
   - After deploy, checks out `main` so production deploys aren't blocked
   - **Known issue**: `docker-compose.override.yml` (dev config) may get auto-loaded by Docker Compose, overriding the staging config. The deploy script renames it temporarily during `docker compose up`.

### Development Workflow

- Develop locally on feature branches
- Push to GitHub to trigger CI checks
- **Merge to `staging` = Deploy to staging** (https://staging.loore.org) — Docker Compose on same VM
- **Merge to `main` = Deploy to production** (https://loore.org) — systemd services

### Before Pushing to Main

Ensure:
- All tests pass locally
- Code lints without errors
- Changes are production-ready

### Git Commit Rules

- **NEVER amend commits** unless explicitly instructed by the user. We work on `main` and cannot force push.
- Always create new commits for fixes and changes.

### Local Code Verification

**Backend**: Run from the repo root:
1. `flake8 backend --count --select=E9,F63,F7,F82 --show-source --statistics` — **CI-blocking**: catches Python syntax errors and undefined names. Must pass with 0 errors.
2. `flake8 backend --count --exit-zero --max-complexity=10 --max-line-length=127 --statistics` — non-blocking warnings (style, complexity). CI runs this with `--exit-zero` so it never fails the build, but review the output.
3. `cd backend && python -m pytest` — runs all backend tests.

**Frontend**: Run from the `frontend/` directory:
1. `npm run build 2>&1 | tail -30` — checks for compilation errors and ESLint warnings. The build will fail on errors and report warnings. Note: there is no separate `npm run lint` script configured; the CI skips it gracefully.

**IMPORTANT**: Running `npm run build` changes the working directory to `frontend/`. Always use absolute paths or `cd` back to the repo root before running git commands, otherwise `git add` will fail with `pathspec did not match any files`.

### Database Migrations

The project uses **Flask-Migrate + Alembic**. Migrations are auto-generated and applied during deployment.

- **Do NOT write manual migration scripts.** The deploy script (`deploy.sh`) runs `flask db migrate` which auto-detects model changes and generates Alembic migrations automatically.
- When adding/changing columns in `backend/models.py`, just change the model — the migration will be auto-generated on deploy.
- Auto-generated migrations are committed back to git by the deploy script with `[skip ci]` tag.
- Migration files live in `migrations/versions/`.
### Local Docker Development

The project can be run locally via Docker Compose. This mirrors the staging/production stack (PostgreSQL, Redis, Flask, Celery, React) without needing to install dependencies on the host.

**Start dev environment**: `make dev` (or `make dev-build` to force rebuild)
**Stop**: `make stop`
**Logs**: `make logs`
**Shell into backend**: `make shell`

The dev stack runs:
- Backend on `http://localhost:5010` (Flask dev server with hot reload)
- Frontend on `http://localhost:3001` (React dev server with HMR)
- PostgreSQL, Redis, and Celery are internal to the Docker network

Source code is volume-mounted, so edits are reflected immediately without rebuilding.

**Important Docker Compose notes**:
- Ports are NOT defined in the base `docker-compose.yml` — they live only in the override files (`docker-compose.override.yml` for dev, `docker-compose.staging.yml` for staging). This prevents Docker Compose from concatenating port arrays across files.
- Backend code is mounted at `/app/backend/` inside containers to match the project-root import layout (`from backend.X import Y`).
- The `migrations/` directory is at the project root, outside the backend Docker build context. For staging, it's volume-mounted into the container.

### Production Environment

- URL: https://loore.org
- Backend: Flask + Gunicorn (systemd services)
- Frontend: React (built and served via nginx)
- Database: PostgreSQL
- Task queue: Celery + Redis

### Staging Environment

- URL: https://staging.loore.org
- Runs on the **same VM** as production, isolated via Docker Compose project name `wop-staging`
- Ports: backend on `127.0.0.1:5011`, frontend on `127.0.0.1:8081` (avoids collision with production)
- Docker Compose files: `docker-compose.yml` + `docker-compose.prod.yml` + `docker-compose.staging.yml`
- Env file: `.env.staging` (not committed; `.env.staging.example` is committed)
- Frontend: pre-built in CI, served by `nginx:alpine` from `frontend/build-staging/` (gitignored)
- DB: `writeorperish_staging` — ephemeral, reset on each deploy
- Resource limits are lower than production (512M backend, 128M frontend/redis, 256M db)
