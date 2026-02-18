# Write or Perish - Docker Compose Makefile
# Simplifies common Docker operations

.PHONY: help dev prod test build clean logs shell db-shell migrate

# Default target
help:
	@echo "Write or Perish - Docker Commands"
	@echo ""
	@echo "Development:"
	@echo "  make dev          - Start development environment with hot reload"
	@echo "  make dev-build    - Rebuild and start development environment"
	@echo "  make logs         - Show logs from all containers"
	@echo "  make shell        - Open shell in backend container"
	@echo "  make db-shell     - Open PostgreSQL shell"
	@echo ""
	@echo "Testing:"
	@echo "  make test         - Run all tests in Docker"
	@echo "  make test-backend - Run only backend tests"
	@echo "  make test-frontend - Run only frontend tests"
	@echo ""
	@echo "Production:"
	@echo "  make prod         - Start production environment"
	@echo "  make prod-build   - Rebuild and start production"
	@echo ""
	@echo "Database:"
	@echo "  make migrate      - Run database migrations"
	@echo "  make db-reset     - Reset database (WARNING: destroys data)"
	@echo ""
	@echo "Cleanup:"
	@echo "  make stop         - Stop all containers"
	@echo "  make clean        - Stop and remove containers, networks"
	@echo "  make clean-all    - Remove everything including volumes (WARNING: destroys data)"

# ============================================================================
# DEVELOPMENT
# ============================================================================

dev:
	docker compose up

dev-build:
	docker compose up --build

dev-detached:
	docker compose up -d

# ============================================================================
# PRODUCTION
# ============================================================================

prod:
	docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d

prod-build:
	docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build

# ============================================================================
# TESTING
# ============================================================================

test:
	docker compose -f docker-compose.test.yml up --build --abort-on-container-exit

test-backend:
	docker compose -f docker-compose.test.yml up --build backend db redis --abort-on-container-exit

test-frontend:
	docker compose -f docker-compose.test.yml up --build frontend --abort-on-container-exit

# ============================================================================
# UTILITIES
# ============================================================================

logs:
	docker compose logs -f

logs-backend:
	docker compose logs -f backend celery

logs-frontend:
	docker compose logs -f frontend

shell:
	docker compose exec backend bash

db-shell:
	docker compose exec db psql -U $${POSTGRES_USER:-writeorperish} -d $${POSTGRES_DB:-writeorperish}

redis-cli:
	docker compose exec redis redis-cli

# ============================================================================
# DATABASE
# ============================================================================

migrate:
	docker compose exec backend flask db upgrade

migrate-create:
	docker compose exec backend flask db migrate -m "$(MSG)"

db-reset:
	@echo "WARNING: This will destroy all data in the database!"
	@read -p "Are you sure? [y/N] " confirm && [ "$$confirm" = "y" ]
	docker compose down -v
	docker compose up -d db
	sleep 5
	docker compose exec backend flask db upgrade

# ============================================================================
# CLEANUP
# ============================================================================

stop:
	docker compose stop

clean:
	docker compose down

clean-all:
	@echo "WARNING: This will remove all containers, networks, AND volumes (data will be lost)!"
	@read -p "Are you sure? [y/N] " confirm && [ "$$confirm" = "y" ]
	docker compose down -v --rmi local

# ============================================================================
# BUILD
# ============================================================================

build:
	docker compose build

build-no-cache:
	docker compose build --no-cache
