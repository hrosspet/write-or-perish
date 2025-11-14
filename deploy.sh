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

# Run database migrations
log "Running database migrations..."
export FLASK_APP="$BACKEND_DIR/app.py"
if [ -d "migrations" ]; then
    flask db upgrade || warn "Database migration failed or no migrations to apply"
else
    warn "No migrations directory found, skipping migrations"
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
log ""
