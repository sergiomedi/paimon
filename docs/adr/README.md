# Architecture Decision Records

Every decision that is expensive to reverse is recorded here, in
[MADR](https://adr.github.io/madr/) format. An ADR captures the context at the time
of the decision, the alternatives that were genuinely considered, and the consequences
accepted — including the negative ones.

ADRs are immutable once accepted. A decision that no longer holds is not edited; a new
ADR supersedes it, and the old one is marked accordingly. The value of the record is the
reasoning trail, and rewriting history destroys it.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-use-madr-for-architecture-decisions.md) | Use MADR for architecture decisions | Accepted |
| [0002](0002-monorepo-layout-and-module-boundaries.md) | Monorepo layout and enforced module boundaries | Accepted |
| [0003](0003-ports-and-adapters-for-llm-and-vector-store.md) | Ports and adapters for LLM and vector store | Accepted |
| [0004](0004-authentication-with-entra-id.md) | Authentication with Microsoft Entra ID | Accepted |
| [0005](0005-python-toolchain.md) | Python toolchain: uv, ruff, mypy | Accepted |
| [0006](0006-continuous-integration-from-phase-1.md) | Continuous integration from Phase 1 | Accepted |
| [0007](0007-persistence-postgresql-sqlalchemy-alembic-redis.md) | Persistence: PostgreSQL, SQLAlchemy, Alembic, Redis | Accepted |
| [0008](0008-target-domain-engineering-operations.md) | Target domain: engineering operations | Accepted |
| [0009](0009-dependency-injection-with-fastapi-depends.md) | Dependency injection with FastAPI's Depends | Accepted |
| [0010](0010-separate-embedding-and-chat-ports.md) | Separate embedding and chat ports (refines 0003) | Accepted |
| [0011](0011-fix-embeddings-at-1024-dimensions.md) | Fix embeddings at 1024 dimensions | Accepted |
| [0012](0012-fuse-retrieval-by-rank.md) | Fuse retrieval results by rank, not by score | Accepted |
| [0013](0013-anchor-ground-truth-to-quotations.md) | Anchor evaluation ground truth to quotations, not chunk ids | Accepted |
| [0014](0014-azure-adapters-and-authentication.md) | Azure adapters and how they authenticate | Accepted |
| [0015](0015-agent-state-lives-in-the-domain.md) | Agent state lives in the domain, the graph in infrastructure | Accepted |
| [0016](0016-deterministic-workflows-before-autonomous-agents.md) | Deterministic workflows before autonomous agents | Accepted |
| [0017](0017-agent-persistence-on-the-existing-driver.md) | Persist agent runs on the drivers the platform already has | Accepted |
| [0018](0018-tool-calling-as-a-capability.md) | Tool calling is a capability, and the tool surface stays small | Accepted |

## Writing a new ADR

Copy [`template.md`](template.md), take the next free number, and open it as part of the
pull request that implements the decision. An ADR proposed after the code is merged is
documentation; an ADR proposed with the code is design.
