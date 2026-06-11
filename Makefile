.PHONY: test test-all test-db lint typecheck fmt all build dev up down clean \
        run-selfhosted run-public stop-selfhosted stop-public \
        setup-selfhosted setup-public bundle-singlefile \
        hs-up hs-down hs-address hs-rotate

COMPOSE := docker compose

# Gates run through the dev overlay so ./src, ./tests and ./scripts are
# bind-mounted — they check the live working tree, not whatever source
# was baked into the image at the last build. (Dependency or pyproject
# changes still need a `make build` first.)
DEV_COMPOSE := $(COMPOSE) -f docker-compose.yml -f docker-compose.dev.yml
RUN := $(DEV_COMPOSE) run --rm --no-deps app

# Integration tests connect to the compose-network postgres and insist
# on a disposable *_test database (they drop tables on teardown).
TEST_DB_URL := postgresql://archiver:archiver@postgres:5432/archiver_test
RUN_TEST := $(DEV_COMPOSE) run --rm -e ARCHIVER_TEST_DB_URL=$(TEST_DB_URL) app

# -- Quality gates (run inside Docker) --

test: test-db
	$(RUN_TEST) uv run --no-sync pytest --tb=short -m "not slow"

test-all: test-db
	$(RUN_TEST) uv run --no-sync pytest --tb=short

# Ensure postgres is up and the test database exists (idempotent).
test-db:
	$(DEV_COMPOSE) up -d --wait postgres
	$(DEV_COMPOSE) exec -T postgres sh -c \
		"psql -U archiver -d archiver -tc \"SELECT 1 FROM pg_database WHERE datname='archiver_test'\" | grep -q 1 || createdb -U archiver archiver_test"

lint:
	$(RUN) uv run --no-sync ruff check .

typecheck:
	$(RUN) uv run --no-sync pyright

fmt:
	$(RUN) uv run --no-sync ruff format .
	$(RUN) uv run --no-sync ruff check --fix .

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

# --build so `git pull && make run-<mode>` actually deploys the pulled
# code instead of restarting the stale image.
run-selfhosted:
	$(COMPOSE) -f docker-compose.yml -f docker-compose.selfhosted.yml up -d --build

run-public:
	$(COMPOSE) -f docker-compose.yml -f docker-compose.public.yml up -d --build

stop-selfhosted:
	$(COMPOSE) -f docker-compose.yml -f docker-compose.selfhosted.yml down

stop-public:
	$(COMPOSE) -f docker-compose.yml -f docker-compose.public.yml down

# -- Hidden service (Tor) --

HS_COMPOSE := $(COMPOSE) --profile darknet \
              -f docker-compose.yml \
              -f docker-compose.dev.yml \
              -f docker-compose.hidden-service.yml

# Bring up the full dev stack plus the hidden-service overlay. The
# archiver UI becomes reachable through Tor Browser at the .onion
# printed by `make hs-address`.
hs-up:
	$(HS_COMPOSE) up -d

hs-down:
	$(HS_COMPOSE) down

hs-address:
	@$(HS_COMPOSE) exec -T tor sh -c 'until test -f /var/lib/tor/archiver_hs/hostname; do sleep 1; done; cat /var/lib/tor/archiver_hs/hostname'

# Regenerate the onion address. Destroys the persistent key volume.
hs-rotate:
	$(HS_COMPOSE) down
	docker volume rm archiver_tor_hidden_service
	$(HS_COMPOSE) up -d

# -- Vendor --

bundle-singlefile:
	@echo "Downloading SingleFile bundle from npm..."
	mkdir -p /tmp/singlefile-dl && cd /tmp/singlefile-dl && \
		npm pack single-file-cli 2>/dev/null && \
		tar -xzf single-file-cli-*.tgz && \
		cp package/lib/single-file-bundle.js $(CURDIR)/src/archiver/vendor/single-file-bundle.js
	@echo "Done: src/archiver/vendor/single-file-bundle.js"
