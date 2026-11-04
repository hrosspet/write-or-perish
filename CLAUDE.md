# Claude Code Instructions for Write-or-Perish

## Product Intent — Read First for Feature Work

When implementing a feature that is underspecified, ambiguous, or specified in a possibly outdated doc, read `docs/LOORE-ESSENCE.md` first. It characterizes what Loore is and aspires to be, the vibes it should give, and the decision heuristics for resolving ambiguity (plus when to placeholder and ask instead of deciding).

Decision log: docs/DECISION-LOG.md · intent doc: docs/LOORE-ESSENCE.md (read by the `decision-log` skill)

## CI/CD Pipeline - IMPORTANT

**Pushing to `main` branch triggers automatic deployment to production.**

The project uses GitHub Actions for CI/CD with direct deployment to production:

### Workflow Overview

1. **CI (`ci.yml`)** - Runs on PRs only (not on push).

2. **Deploy to Production (`deploy.yml`)** - Runs ONLY on pushes to `main`, except pushes that only change `ios/**` (`paths-ignore`; the iOS app ships via Xcode/TestFlight):
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
- **Merge to `staging` = Deploy to staging** (https://staging.loore.org) — Docker Compose on the same VM as production
- **Merge to `main` = Deploy to production** (https://loore.org) — systemd services

### Git Commit Rules

- **NEVER amend commits** unless explicitly instructed by the user. We work on `main` and cannot force push.

### Design Docs Maintenance

After implementing a feature, update the status in these design docs:
- `docs/FOUR-FEATURE-ECOSYSTEM.md` — Feature 1 current state, or relevant feature section
- `docs/TECHNICAL-ROADMAP.md` — "Current State: Already Built", phase checklists, and "Next Steps"
- `docs/ACCELERATED-DEVELOPMENT-ROADMAP.md` — "Already Done", phase items, and "Next Actions"

Mark completed items with ✅. For unplanned features, add new numbered entries (e.g. A.10, A.11) following the existing pattern.
- Always create new commits for fixes and changes.

### Celery Tasks — IMPORTANT

When adding a new Celery task file in `backend/tasks/`, you **MUST** register it in `backend/celery_app.py` by adding an import line:
```python
from backend.tasks import your_new_module  # noqa: F401
```
Without this, the task will be dispatched to the queue but never picked up by the worker. This has caused bugs before.

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

Iterate locally with `make dev` (backend `http://localhost:5010`, frontend `http://localhost:3001`), not on staging: staging is for the final pre-review pass only (workflow decision 2026-07-02). Never run `flask db migrate` locally; sync schema with `db.create_all()` (deploy generates and commits migrations, and local ones collide). Before local Docker work (make targets, celery beat, dark flags, schema sync, login without SMTP, `package.json` changes, env vars, the CRA proxy, compose layout), read `docs/LOCAL-DEV.md`.
