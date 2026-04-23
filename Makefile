.PHONY: test test-all lint typecheck fmt all build dev up down clean \
        run-selfhosted run-public stop-selfhosted stop-public \
        setup-selfhosted setup-public bundle-singlefile \
        hs-up hs-down hs-address hs-rotate

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
