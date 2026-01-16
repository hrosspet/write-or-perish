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

# Run database migrations
log "Running database migrations..."
export FLASK_APP="$BACKEND_DIR/app.py"
if [ -d "migrations" ]; then
    # Auto-generate migrations if models changed
    log "Checking for model changes..."
    MIGRATION_OUTPUT=$(flask db migrate -m "auto-generated migration from deployment" 2>&1)
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

    # Apply migrations
    flask db upgrade || warn "Database migration failed or no migrations to apply"
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

# Restart Celery worker service
log "Restarting Celery worker service..."
sudo systemctl restart write-or-perish-celery || error "Failed to restart Celery service"

# Wait for Celery to start
sleep 3

# Check if Celery service is running
if sudo systemctl is-active --quiet write-or-perish-celery; then
    log "Celery worker service restarted successfully"
else
    error "Celery worker service failed to start"
fi

# Restart Gunicorn service
log "Restarting Gunicorn service..."
sudo systemctl restart write-or-perish || error "Failed to restart Gunicorn service"

# Wait for service to start
sleep 3

# Check if service is running
if sudo systemctl is-active --quiet write-or-perish; then
    log "Gunicorn service restarted successfully"
else
    error "Gunicorn service failed to start"
fi

# Reload Nginx
log "Reloading Nginx..."
sudo systemctl reload nginx || warn "Failed to reload Nginx (may need manual intervention)"

log "====== Deployment completed successfully ======"
log "Frontend: /home/hrosspet/write-or-perish/frontend/build"
log "Backend: Gunicorn running on 127.0.0.1:8000"
log "Celery: Worker running with 2 concurrent processes"
log "Redis: Running on 127.0.0.1:6379"
log ""
log "Service status:"
sudo systemctl is-active write-or-perish && log "  ✓ Gunicorn (write-or-perish)" || warn "  ✗ Gunicorn not running"
sudo systemctl is-active write-or-perish-celery && log "  ✓ Celery (write-or-perish-celery)" || warn "  ✗ Celery not running"
sudo systemctl is-active redis-server && log "  ✓ Redis (redis-server)" || warn "  ✗ Redis not running"
log ""
