"""switch embeddings to nomic-embed-text (768-dim)

Revision ID: f5a6b7c8d9e0
Revises: e5f6a7b8c9d0
Create Date: 2026-04-18

Switch brain_content_chunks embedding column from text-embedding-3-large (1536)
to nomic-embed-text (768) served by self-hosted Ollama. Drops the HNSW index,
swaps the vector column dim, flips the model default, and rebuilds the index.

Safe because prod has never successfully embedded a chunk: brain_embeddings.py
was hardcoded to api.openai.com but prod's OPENAI_API_KEY is a Vercel gateway
key for Nemotron chat, so every embed_all_stale run 401'd. The table is empty.

If anyone ever runs a real OpenAI key against this system before the migration
lands, manually `TRUNCATE brain_content_chunks` first — the DROP COLUMN below
will discard the data anyway, but the TRUNCATE keeps the intent explicit.
"""

from alembic import op


revision = "f5a6b7c8d9e0"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_brain_chunks_embedding")
    op.execute("ALTER TABLE brain_content_chunks DROP COLUMN IF EXISTS embedding")
    op.execute("ALTER TABLE brain_content_chunks ADD COLUMN embedding vector(768)")
    op.execute(
        "ALTER TABLE brain_content_chunks "
        "ALTER COLUMN model SET DEFAULT 'nomic-embed-text'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_brain_chunks_embedding "
        "ON brain_content_chunks USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_brain_chunks_embedding")
    op.execute("ALTER TABLE brain_content_chunks DROP COLUMN IF EXISTS embedding")
    op.execute("ALTER TABLE brain_content_chunks ADD COLUMN embedding vector(1536)")
    op.execute(
        "ALTER TABLE brain_content_chunks "
        "ALTER COLUMN model SET DEFAULT 'text-embedding-3-large'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_brain_chunks_embedding "
        "ON brain_content_chunks USING hnsw (embedding vector_cosine_ops)"
    )
