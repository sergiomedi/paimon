# Paimon backend

FastAPI service implementing the platform's domain, application, retrieval, agent and
infrastructure layers.

See the [architecture overview](../docs/architecture/overview.md) for the layering rules
and the [decision records](../docs/adr/README.md) for the reasoning behind them.

## Layout

| Package | Responsibility | May import |
|---|---|---|
| `domain` | Entities, value objects, ports | stdlib, `pydantic` |
| `application` | Use cases, orchestration | `domain` |
| `rag` | Ingestion, chunking, retrieval | `domain` |
| `agents` | LangGraph workflows | `domain`, `application` |
| `infrastructure` | Adapters for external systems | `domain` |
| `interfaces` | FastAPI routers, schemas, wiring | `application`, `domain` |

`interfaces/api/dependencies.py` is the composition root and the only module allowed to
import from `infrastructure`. The rule is enforced by `import-linter` in CI.

## Commands

```bash
uv sync                  # create the environment from uv.lock
uv run ruff check .      # lint
uv run ruff format .     # format
uv run mypy              # type check
uv run pytest            # tests
```
