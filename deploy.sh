#!/bin/bash

# Write or Perish Deployment Script
# This script handles the deployment of backend updates

set -e  # Exit on any error

PROJECT_DIR="/home/hrosspet/write-or-perish"
CONDA_ENV="write-or-perish"
BACKEND_DIR="$PROJECT_DIR/backend"
LOG_FILE="$PROJECT_DIR/deployment.log"

# Color output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log() {
    echo -e "${GREEN}[$(date +'%Y-%m-%d %H:%M:%S')]${NC} $1" | tee -a "$LOG_FILE"
}

error() {
    echo -e "${RED}[$(date +'%Y-%m-%d %H:%M:%S')] ERROR:${NC} $1" | tee -a "$LOG_FILE"
    exit 1
}

warn() {
    echo -e "${YELLOW}[$(date +'%Y-%m-%d %H:%M:%S')] WARNING:${NC} $1" | tee -a "$LOG_FILE"
}

log "====== Starting deployment ======"

# Navigate to project directory
cd "$PROJECT_DIR" || error "Failed to navigate to project directory"

# Initialize conda
log "Initializing conda..."
source ~/miniconda3/etc/profile.d/conda.sh || error "Failed to initialize conda"

# Check if conda environment exists
if ! conda env list | grep -q "^$CONDA_ENV "; then
    warn "Conda environment '$CONDA_ENV' not found, creating one..."
    conda create -n "$CONDA_ENV" python=3.9 -y || error "Failed to create conda environment"
fi

# Activate conda environment
log "Activating conda environment: $CONDA_ENV..."
conda activate "$CONDA_ENV" || error "Failed to activate conda environment"

# Install/update Python dependencies
log "Installing Python dependencies..."
pip install -q --upgrade pip
pip install -q -r "$BACKEND_DIR/requirements.txt" || error "Failed to install dependencies"

# Ensure git remote uses SSH (not HTTPS) for authentication
log "Checking git remote configuration..."
CURRENT_REMOTE=$(git remote get-url origin)
if [[ "$CURRENT_REMOTE" == https://* ]]; then
    log "Converting git remote from HTTPS to SSH..."
    SSH_REMOTE=$(echo "$CURRENT_REMOTE" | sed -E 's|https://github.com/|git@github.com:|')
    git remote set-url origin "$SSH_REMOTE"
    log "Git remote updated to: $SSH_REMOTE"
fi

# Database migrations.
#
# Services are stopped ONLY when a migration is actually pending, and only
# around `flask db upgrade` (ALTER TABLE needs an ACCESS EXCLUSIVE lock that
# blocks on live connections). Generating the migration and committing it
# back only read the schema and must not cost downtime: before 2026-08-28
# every deploy stopped Gunicorn up front, so 13 of 14 no-op deploys served
# 502s for a minute each and the one real migration for ~4 minutes.
SERVICES_STOPPED=0
stop_services_for_upgrade() {
    log "Stopping application services for the schema upgrade..."
    sudo systemctl stop write-or-perish-celery-beat 2>/dev/null || true
    sudo systemctl stop write-or-perish-celery 2>/dev/null || true
    sudo systemctl stop write-or-perish 2>/dev/null || true
    sleep 2
    SERVICES_STOPPED=1
}

log "Running database migrations..."
export FLASK_APP="$BACKEND_DIR/app.py"
if [ -d "migrations" ]; then
    # Auto-generate migrations if models changed (services stay up)
    log "Checking for model changes..."
    MIGRATION_OUTPUT=$(flask db migrate -m "auto-generated migration from deployment" 2>&1) || true
    echo "$MIGRATION_OUTPUT"

    if echo "$MIGRATION_OUTPUT" | grep -q "Generating"; then
        log "New migration generated, committing to git..."
        git add migrations/versions/*.py
        if git commit -m "Auto-generated migration from deployment [skip ci]"; then
            log "Migration committed, pushing to repository..."
            git push origin main || warn "Failed to push migration file (will be available locally)"
        else
            log "No changes to commit"
        fi
    else
        log "No model changes detected"
    fi

    # Check for multiple migration heads
    log "Checking for migration conflicts..."
    HEADS_COUNT=$(flask db heads 2>/dev/null | grep -c "^[a-f0-9]" || echo "0")
    if [ "$HEADS_COUNT" -gt 1 ]; then
        error "Multiple migration heads detected! Please merge them manually before deploying."
    fi

    # Apply migrations — stop the services only if there is something to apply
    CURRENT_REV=$( (flask db current 2>/dev/null || true) | grep -oE '^[a-f0-9]+' | head -1 || true)
    HEAD_REV=$( (flask db heads 2>/dev/null || true) | grep -oE '^[a-f0-9]+' | head -1 || true)
    if [ -n "$HEAD_REV" ] && [ "$CURRENT_REV" != "$HEAD_REV" ]; then
        log "Pending migration (${CURRENT_REV:-none} -> $HEAD_REV)"
        stop_services_for_upgrade
        flask db upgrade || error "Database migration failed"
    else
        log "Database already at head (${HEAD_REV:-unknown}); no downtime needed"
    fi
else
    warn "No migrations directory found, skipping migrations"
fi

# Check and install Redis if needed
log "Checking Redis installation..."
if ! command -v redis-server &> /dev/null; then
    log "Redis not found, installing..."
    sudo apt-get update -qq
    sudo apt-get install -y redis-server || error "Failed to install Redis"

    # Enable and start Redis
    sudo systemctl enable redis-server
    sudo systemctl start redis-server
    log "Redis installed and started"
else
    log "Redis already installed"

    # Ensure Redis is running
    if ! sudo systemctl is-active --quiet redis-server; then
        log "Starting Redis service..."
        sudo systemctl start redis-server || warn "Failed to start Redis"
    fi
fi

# Verify Redis is working
if redis-cli ping &> /dev/null; then
    log "Redis is responding to ping"
else
    warn "Redis may not be working correctly"
fi

# Install/update Gunicorn service
log "Installing Gunicorn service..."
GUNICORN_SERVICE_SOURCE="$PROJECT_DIR/write-or-perish.service"
GUNICORN_SERVICE_TARGET="/etc/systemd/system/write-or-perish.service"

if [ -f "$GUNICORN_SERVICE_SOURCE" ]; then
    # Check if service file has changed
    if ! sudo diff -q "$GUNICORN_SERVICE_SOURCE" "$GUNICORN_SERVICE_TARGET" >/dev/null 2>&1; then
        log "Gunicorn service file has changed, updating..."

        # Copy new service file
        sudo cp "$GUNICORN_SERVICE_SOURCE" "$GUNICORN_SERVICE_TARGET" || error "Failed to copy Gunicorn service file"

        # Reload systemd daemon
        sudo systemctl daemon-reload || error "Failed to reload systemd daemon"

        log "Gunicorn service file updated"
    else
        log "Gunicorn service file unchanged"
    fi

    # Enable Gunicorn service
    sudo systemctl enable write-or-perish || warn "Failed to enable Gunicorn service"
else
    error "Gunicorn service file not found at $GUNICORN_SERVICE_SOURCE"
fi

# Install/update Celery worker service
log "Installing Celery worker service..."
CELERY_SERVICE_SOURCE="$PROJECT_DIR/write-or-perish-celery.service"
CELERY_SERVICE_TARGET="/etc/systemd/system/write-or-perish-celery.service"

if [ -f "$CELERY_SERVICE_SOURCE" ]; then
    # Check if service file has changed
    if ! sudo diff -q "$CELERY_SERVICE_SOURCE" "$CELERY_SERVICE_TARGET" >/dev/null 2>&1; then
        log "Celery service file has changed, updating..."

        # Copy new service file
        sudo cp "$CELERY_SERVICE_SOURCE" "$CELERY_SERVICE_TARGET" || error "Failed to copy Celery service file"

        # Reload systemd daemon
        sudo systemctl daemon-reload || error "Failed to reload systemd daemon"

        log "Celery service file updated"
    else
        log "Celery service file unchanged"
    fi

    # Enable Celery service
    sudo systemctl enable write-or-perish-celery || warn "Failed to enable Celery service"
else
    error "Celery service file not found at $CELERY_SERVICE_SOURCE"
fi

# Install/update Celery beat service
log "Installing Celery beat service..."
CELERY_BEAT_SERVICE_SOURCE="$PROJECT_DIR/write-or-perish-celery-beat.service"
CELERY_BEAT_SERVICE_TARGET="/etc/systemd/system/write-or-perish-celery-beat.service"

if [ -f "$CELERY_BEAT_SERVICE_SOURCE" ]; then
    # Check if service file has changed
    if ! sudo diff -q "$CELERY_BEAT_SERVICE_SOURCE" "$CELERY_BEAT_SERVICE_TARGET" >/dev/null 2>&1; then
        log "Celery beat service file has changed, updating..."

        # Copy new service file
        sudo cp "$CELERY_BEAT_SERVICE_SOURCE" "$CELERY_BEAT_SERVICE_TARGET" || error "Failed to copy Celery beat service file"

        # Reload systemd daemon
        sudo systemctl daemon-reload || error "Failed to reload systemd daemon"

        log "Celery beat service file updated"
    else
        log "Celery beat service file unchanged"
    fi

    # Enable Celery beat service
    sudo systemctl enable write-or-perish-celery-beat || warn "Failed to enable Celery beat service"
else
    warn "Celery beat service file not found at $CELERY_BEAT_SERVICE_SOURCE"
fi

# Install/update nightly backup service + timer (scripts/backup.sh)
log "Installing backup timer..."
BACKUP_SERVICE_SOURCE="$PROJECT_DIR/write-or-perish-backup.service"
BACKUP_SERVICE_TARGET="/etc/systemd/system/write-or-perish-backup.service"
BACKUP_TIMER_SOURCE="$PROJECT_DIR/write-or-perish-backup.timer"
BACKUP_TIMER_TARGET="/etc/systemd/system/write-or-perish-backup.timer"

if [ -f "$BACKUP_SERVICE_SOURCE" ] && [ -f "$BACKUP_TIMER_SOURCE" ]; then
    BACKUP_UNITS_CHANGED=false
    for pair in "$BACKUP_SERVICE_SOURCE:$BACKUP_SERVICE_TARGET" "$BACKUP_TIMER_SOURCE:$BACKUP_TIMER_TARGET"; do
        src="${pair%%:*}"
        dst="${pair##*:}"
        if ! sudo diff -q "$src" "$dst" >/dev/null 2>&1; then
            sudo cp "$src" "$dst" || error "Failed to copy $(basename "$src")"
            BACKUP_UNITS_CHANGED=true
        fi
    done
    if [ "$BACKUP_UNITS_CHANGED" = true ]; then
        sudo systemctl daemon-reload || error "Failed to reload systemd daemon"
        log "Backup unit files updated"
    else
        log "Backup unit files unchanged"
    fi

    # Arm the timer only when a destination is configured; otherwise every
    # night would just produce a failed unit.
    if grep -qE '^BACKUP_BUCKET=.+' "$PROJECT_DIR/.env.production" 2>/dev/null; then
        sudo systemctl enable --now write-or-perish-backup.timer || warn "Failed to enable backup timer"
        log "Backup timer enabled; next run: $(systemctl show write-or-perish-backup.timer -p NextElapseUSecRealtime --value 2>/dev/null || echo unknown)"
    else
        warn "BACKUP_BUCKET not set in .env.production — nightly backups are NOT enabled (docs/RUNBOOKS.md → Backups and restore)"
    fi
else
    warn "Backup unit files not found, skipping backup timer"
fi

# Update Nginx configuration if changed
NGINX_CONFIG_SOURCE="$PROJECT_DIR/configs/nginx.txt"
NGINX_CONFIG_TARGET="/etc/nginx/sites-available/write-or-perish"

if [ -f "$NGINX_CONFIG_SOURCE" ]; then
    log "Checking if Nginx configuration needs updating..."

    # Check if config file has changed
    if ! sudo diff -q "$NGINX_CONFIG_SOURCE" "$NGINX_CONFIG_TARGET" >/dev/null 2>&1; then
        log "Nginx configuration has changed, updating..."

        # Backup current config
        sudo cp "$NGINX_CONFIG_TARGET" "$NGINX_CONFIG_TARGET.backup.$(date +%Y%m%d_%H%M%S)" 2>/dev/null || true

        # Copy new config
        sudo cp "$NGINX_CONFIG_SOURCE" "$NGINX_CONFIG_TARGET" || error "Failed to copy Nginx config"

        # Test configuration
        log "Testing Nginx configuration..."
        if sudo nginx -t 2>&1 | tee -a "$LOG_FILE"; then
            log "Nginx configuration test passed"
        else
            error "Nginx configuration test failed! Rolling back..."
            sudo cp "$NGINX_CONFIG_TARGET.backup."* "$NGINX_CONFIG_TARGET" 2>/dev/null || true
            exit 1
        fi
    else
        log "Nginx configuration unchanged, skipping update"
    fi
else
    warn "Nginx config source not found at $NGINX_CONFIG_SOURCE"
fi

# Restart Gunicorn FIRST — it is the site. Everything above stopped the
# services for migrations, so until this runs nginx has nothing to proxy to
# and prod serves 502s. Any `error` below this point fails the deploy loudly
# (red CI) without also taking prod down; before 2026-07-24 a beat that
# wouldn't start left Gunicorn stopped and caused an outage.
# Zero-downtime path: when the services were never stopped, HUP the Gunicorn
# master (ExecReload) — it re-forks workers on the new code while the old
# ones drain, so nginx always has an upstream. A HUP does NOT re-read
# EnvironmentFile, so if .env.production changed since the master started,
# fall back to a full restart.
GUNICORN_PID=$(cat "$PROJECT_DIR/gunicorn.pid" 2>/dev/null || true)
NEED_FULL_RESTART=1
if [ "$SERVICES_STOPPED" -eq 0 ] && [ -n "$GUNICORN_PID" ] && sudo systemctl is-active --quiet write-or-perish; then
    MASTER_START=$(date -d "$(ps -o lstart= -p "$GUNICORN_PID" 2>/dev/null)" +%s 2>/dev/null || echo 0)
    ENV_MTIME=$(stat -c %Y "$PROJECT_DIR/.env.production" 2>/dev/null || echo 0)
    if [ "$MASTER_START" -gt 0 ] && [ "$ENV_MTIME" -le "$MASTER_START" ]; then
        NEED_FULL_RESTART=0
    else
        log ".env.production changed since Gunicorn started — full restart instead of reload"
    fi
fi
if [ "$NEED_FULL_RESTART" -eq 0 ]; then
    log "Reloading Gunicorn workers (graceful, no downtime)..."
    sudo systemctl reload write-or-perish || {
        warn "Graceful reload failed; restarting Gunicorn"
        sudo systemctl restart write-or-perish || error "Failed to restart Gunicorn service"
    }
else
    log "Restarting Gunicorn service..."
    sudo systemctl restart write-or-perish || error "Failed to restart Gunicorn service"
fi

# Wait for service to start
sleep 3

# Check if service is running
if sudo systemctl is-active --quiet write-or-perish; then
    log "Gunicorn service restarted successfully"
else
    error "Gunicorn service failed to start"
fi

# Restart Celery worker service
log "Restarting Celery worker service..."
sudo systemctl restart write-or-perish-celery || error "Failed to restart Celery service"

# Restart Celery beat service.
# The celerybeat-schedule shelve file only caches "last run at" timestamps;
# a restart that lands mid-write leaves it truncated and beat dies on
# startup with EOFError/KeyError('entries') (2026-07-24, 2026-08-27). It has
# no state worth keeping across a deploy, so remove it every time — the
# schedule is regenerated from backend/celery_app.py on start.
log "Restarting Celery beat service..."
sudo systemctl stop write-or-perish-celery-beat 2>/dev/null || true
rm -f "$PROJECT_DIR"/celerybeat-schedule "$PROJECT_DIR"/celerybeat-schedule.* "$PROJECT_DIR"/celery-beat.pid
sudo systemctl restart write-or-perish-celery-beat || error "Failed to restart Celery beat service"

# Wait for Celery services to start
sleep 3

# Celery failures are recorded and re-raised at the very END of the script
# rather than exiting here: the deploy must still fail loudly (red CI), but
# a dead worker or beat should not skip the nginx reload below, which is what
# activates a changed nginx config.
FAILED_SERVICES=""

# Check if Celery worker service is running
if sudo systemctl is-active --quiet write-or-perish-celery; then
    log "Celery worker service restarted successfully"
else
    warn "Celery worker service failed to start"
    FAILED_SERVICES="$FAILED_SERVICES write-or-perish-celery"
fi

# Check if Celery beat service is running
if sudo systemctl is-active --quiet write-or-perish-celery-beat; then
    log "Celery beat service restarted successfully"
else
    # The schedule file was already removed above, so this is not the
    # corrupt-shelve case. Do NOT print the log here: this output lands in
    # a public repo's Actions log, and server logs can carry paths, task
    # args and broker details. Point at it instead.
    warn "Celery beat service failed to start — see logs/celery-beat.log on the VM"
    FAILED_SERVICES="$FAILED_SERVICES write-or-perish-celery-beat"
fi

# Reload Nginx
log "Reloading Nginx..."
sudo systemctl reload nginx || warn "Failed to reload Nginx (may need manual intervention)"

if [ -n "$FAILED_SERVICES" ]; then
    warn "====== Deployment completed WITH FAILURES ======"
else
    log "====== Deployment completed successfully ======"
fi
log "Frontend: /home/hrosspet/write-or-perish/frontend/build"
log "Backend: Gunicorn running on 127.0.0.1:8000"
log "Celery: Worker running with 2 concurrent processes"
log "Redis: Running on 127.0.0.1:6379"
log ""
log "Service status:"
sudo systemctl is-active write-or-perish && log "  ✓ Gunicorn (write-or-perish)" || warn "  ✗ Gunicorn not running"
sudo systemctl is-active write-or-perish-celery && log "  ✓ Celery (write-or-perish-celery)" || warn "  ✗ Celery not running"
sudo systemctl is-active write-or-perish-celery-beat && log "  ✓ Celery Beat (write-or-perish-celery-beat)" || warn "  ✗ Celery Beat not running"
sudo systemctl is-active redis-server && log "  ✓ Redis (redis-server)" || warn "  ✗ Redis not running"
log ""

# Re-raise any Celery failure recorded earlier. The site is already back up
# and nginx has been reloaded, so this exits non-zero purely to turn the
# deploy run red — a failed worker or scheduler must never pass silently.
if [ -n "$FAILED_SERVICES" ]; then
    error "Deployment finished, but these services are NOT running:$FAILED_SERVICES"
fi
