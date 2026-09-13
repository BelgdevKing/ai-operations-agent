-- Runs once, on first creation of the Postgres data volume.
-- Extensions the platform will rely on later; creating them now is harmless.

-- UUID primary keys (gen_random_uuid lives in pgcrypto)
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Vector similarity search for the RAG phase
CREATE EXTENSION IF NOT EXISTS "vector";

-- Trigram indexes for fuzzy text search over business data
CREATE EXTENSION IF NOT EXISTS "pg_trgm";
