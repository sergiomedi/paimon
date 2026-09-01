-- Runs once, when the data volume is first created.
-- pgvector backs the local retrieval adapter (ADR-0003); enabling it here means
-- a fresh checkout gets a usable database from `docker compose up` alone.
CREATE EXTENSION IF NOT EXISTS vector;
