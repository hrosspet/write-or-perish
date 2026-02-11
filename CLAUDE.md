# Claude Code Instructions for Write-or-Perish

## CI/CD Pipeline - IMPORTANT

**Pushing to `main` branch triggers automatic deployment to production.**

The project uses GitHub Actions for CI/CD with direct deployment to production:

### Workflow Overview

1. **CI (`ci.yml`)** - Runs on ALL branches and PRs:
   - Backend: Python linting (flake8), pytest tests
   - Frontend: npm lint, jest tests, production build
   - Security: bandit scan, dependency vulnerability check (safety)

2. **Deploy (`deploy.yml`)** - Runs ONLY on pushes to `main`:
   - Builds frontend with production URLs
   - Deploys to production VM via SSH
   - Pulls latest backend code on server
   - Runs `deploy.sh` to restart services

### Development Workflow

- Develop locally on feature branches
- Push to GitHub to trigger CI checks
- **Merge to `main` = Deploy to production** (https://loore.org)
- There is NO staging environment

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
- Dockerization (Makefile, docker-compose) exists but is **not yet in use**.

### Production Environment

- URL: https://loore.org
- Backend: Flask + Gunicorn
- Frontend: React (built and served via nginx)
- Database: PostgreSQL
- Task queue: Celery + Redis
