.PHONY: help build up down restart logs ps \
	pipeline pipeline-4k clean clean-metrics clean-all \
	test test-engine test-benchmarks \
	metric-server metrics-shell \
	dashboard logs-engine logs-prometheus logs-grafana \
	docker-shell docker-bash img-info \
	prometheus-webhook

# ── Variables ──────────────────────────────────────────────────────────────────

COMPOSE   := docker-compose
COMPOSE_F := docker-compose.yml
ENGINE_CTX:= cascade_simulation_engine
IMAGE     := cascade-sim
PY        := python
PY_VENV   := .venv/bin/python

# ── Colour codes ────────────────────────────────────────────────────────────────

GREEN  := \033[0;32m
YELLOW := \033[0;33m
CYAN   := \033[0;36m
RESET  := \033[0m

# ── Help ────────────────────────────────────────────────────────────────────────

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | \
		sed 's/:.*## /@|$(GREEN)$(CYAN)/g' | \
		column -t -s'@'

# ── Docker ───────────────────────────────────────────────────────────────────

build: ## Build Docker image from Dockerfile
	$(COMPOSE) -f $(COMPOSE_F) build simulation-engine

img-info: ## Show built Docker image size and layers
	docker images $(IMAGE) --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}\t{{.CreatedSince}}"

docker-shell: ## Shell into running simulation-engine container
	docker exec -it $(ENGINE_CTX) /bin/bash

docker-bash: ## Start a new interactive bash session in simulation-engine
	docker run --rm -it --entrypoint /bin/bash $(IMAGE)

# ── Stack ─────────────────────────────────────────────────────────────────

up: ## Start full Docker stack (Prometheus + Grafana + Jaeger + simulation-engine)
	$(COMPOSE) -f $(COMPOSE_F) up -d --remove-orphans
	@echo "$(GREEN)Cascade stack is up.$(RESET)"
	@echo "  Grafana   — http://localhost:3000  (admin / cascade)"
	@echo "  Prometheus— http://localhost:9091"
	@echo "  Jaeger    — http://localhost:16686"
	@echo "  Metrics   — http://localhost:9090/metrics"
	@echo "  Engine    — http://localhost:9090/health"

down: ## Stop and remove all containers
	$(COMPOSE) -f $(COMPOSE_F) down

restart: down up ## Restart the full stack

ps: ## Show running containers
	$(COMPOSE) -f $(COMPOSE_F) ps

logs: ## Tail logs from all containers
	$(COMPOSE) -f $(COMPOSE_F) logs -f --tail=50

logs-engine: ## Tail simulation engine logs only
	$(COMPOSE) -f $(COMPOSE_F) logs -f simulation-engine

logs-prometheus: ## Tail Prometheus logs
	$(COMPOSE) -f $(COMPOSE_F) logs -f prometheus

logs-grafana: ## Tail Grafana logs
	$(COMPOSE) -f $(COMPOSE_F) logs -f grafana

# ── Prometheus webhook (test alerting) ─────────────────────────────────────────

prometheus-webhook: ## POST a test alert to the simulation-engine health endpoint
	@curl -s -X POST http://localhost:9093/api/v1/alerts \
	  -H 'Content-Type: application/json' \
	  -d '{}' || echo "No webhook receiver in test mode"

# ── Pipeline ─────────────────────────────────────────────────────────────────

pipeline: ## Run the simulation pipeline locally (no Docker, no render)
	$(PY_VENV)/$(PY) run_v2.py --config configs/recovery_test.json \
		--output-dir metrics

pipeline-4k: ## Run the full pipeline with 4K Manim render
	$(PY_VENV)/$(PY) run_v2.py --config configs/recovery_test.json \
		--output-dir metrics_4k --render-4k

# ── Engine (local, fast iteration) ────────────────────────────────────────────

engine-only: ## Run engine only (no render, no metrics server) — fastest iteration
	$(PY_VENV)/$(PY) run_v2.py --config configs/recovery_test.json \
		--engine-only --output-dir metrics

engine-metrics: ## Run engine with embedded metrics server
	$(PY_VENV)/$(PY) run_v2.py --config configs/recovery_test.json \
		--engine-only --output-dir metrics --enable-metrics

# ── Metrics server ─────────────────────────────────────────────────────────────

metric-server: ## Start the standalone Prometheus metrics server
	$(PY_VENV)/$(PY) -m services.metrics_server --port 9090

metrics-shell: ## Start metrics server in interactive Python
	$(PY_VENV)/$(PY) -c "from services.metrics_server import PrometheusMetricsServer; s=PrometheusMetricsServer(9090); s.start()"

# ── Metrics + Prometheus (local Python process, no Docker) ─────────────────────

prometheus-local: ## Start local prometheus scraping the local engine metrics server
	$(PY_VENV)/$(PY) -m prometheus_server --config.file=monitoring/prometheus.yml

# ── Testing ────────────────────────────────────────────────────────────────────

test: test-engine test-benchmarks ## Run all tests

test-engine: ## Smoke-test the recovery engine
	$(PY_VENV)/$(PY) -c "
from simulations.recovery_engine import load_config, RecoveryEngine
from services.metrics_server import PrometheusMetricsServer
cfg = load_config('configs/recovery_test.json')
eng = RecoveryEngine(cfg)
metrics_srv = PrometheusMetricsServer(port=9091)
metrics_srv.instrument_engine(eng)
eng.run()
print('PASS: engine ran', eng.tick, 'ticks, outcome:', eng.recovery_outcome)
"

test-benchmarks: ## Run topology benchmarks
	$(PY_VENV)/$(PY) experiments/topology_benchmarks.py --nodes 12 --seeds 42 1337

# ── Cleanup ─────────────────────────────────────────────────────────────────────

clean-metrics: ## Remove generated telemetry files
	rm -rf metrics/

clean: clean-metrics ## Remove local metrics output
	rm -rf metrics_4k/

clean-all: clean ## Remove all generated data + Docker volumes
	docker compose -f $(COMPOSE_F) down -v --remove-orphans
	rm -rf metrics/ metrics_4k/ experiments/*/results_*.csv experiments/*/summary_*.json