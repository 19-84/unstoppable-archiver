.PHONY: test lint typecheck fmt all build dev clean bundle-singlefile

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

# -- Vendor --

bundle-singlefile:
	@echo "Downloading SingleFile bundle..."
	curl -sL https://raw.githubusercontent.com/nicois/single-file-cli/master/lib/single-file-bundle.js \
		-o src/archiver/vendor/single-file-bundle.js
	@echo "Done: src/archiver/vendor/single-file-bundle.js"
