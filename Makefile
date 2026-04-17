.PHONY: test test-all lint typecheck fmt all build dev up down clean \
        run-selfhosted run-public stop-selfhosted stop-public \
        setup-selfhosted setup-public bundle-singlefile

COMPOSE := docker compose
RUN := $(COMPOSE) run --rm app

# -- Quality gates (run inside Docker) --

test:
	$(RUN) uv run pytest --tb=short -m "not slow"

test-all:
	$(RUN) uv run pytest --tb=short

lint:
	$(RUN) uv run ruff check .

typecheck:
	$(RUN) uv run pyright

fmt:
	$(RUN) uv run ruff format .
	$(RUN) uv run ruff check --fix .

all: lint typecheck test

# -- Docker --

build:
	$(COMPOSE) build

dev:
	$(COMPOSE) -f docker-compose.yml -f docker-compose.dev.yml up

up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

clean:
	$(COMPOSE) down -v --remove-orphans

# -- Deployment modes --

setup-selfhosted:
	@test -f .env || cp .env.example.selfhosted .env
	@echo "Wrote .env (edit as needed), then run: make run-selfhosted"

setup-public:
	./scripts/setup-public.sh

run-selfhosted:
	$(COMPOSE) -f docker-compose.yml -f docker-compose.selfhosted.yml up -d

run-public:
	$(COMPOSE) -f docker-compose.yml -f docker-compose.public.yml up -d

stop-selfhosted:
	$(COMPOSE) -f docker-compose.yml -f docker-compose.selfhosted.yml down

stop-public:
	$(COMPOSE) -f docker-compose.yml -f docker-compose.public.yml down

# -- Vendor --

bundle-singlefile:
	@echo "Downloading SingleFile bundle from npm..."
	mkdir -p /tmp/singlefile-dl && cd /tmp/singlefile-dl && \
		npm pack single-file-cli 2>/dev/null && \
		tar -xzf single-file-cli-*.tgz && \
		cp package/lib/single-file-bundle.js $(CURDIR)/src/archiver/vendor/single-file-bundle.js
	@echo "Done: src/archiver/vendor/single-file-bundle.js"
