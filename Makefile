# observability-golden-signals-stack - portoes locais, sem rede e sem Docker.
PY := python3

.PHONY: help validate test cost ci
help: ## lista os alvos
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-9s %s\n", $$1, $$2}'

validate: ## contrato estrutural: YAML/JSON, regras, dashboards e termos protegidos
	$(PY) scripts/validate.py

test: ## suíte unittest (regras, tags, stats, telemetria da app)
	$(PY) -m unittest discover -s tests

cost: ## custo de cardinalidade de labels, com checagem de limites
	$(PY) scripts/custo-cardinalidade.py --check

ci: validate test cost ## os tres portoes que o workflow publicado roda
	@echo "ci: validate, testes e cardinalidade passaram (exit 0)"
