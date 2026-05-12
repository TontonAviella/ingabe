"""brain_content_chunks: 1536 -> 768 dim (Ollama nomic-embed-text)

Brain embeddings switched from OpenAI text-embedding-3-large (1536-dim) to
local Ollama nomic-embed-text (768-dim). The pgvector column dimension is
part of the type, so we have to ALTER TYPE and rebuild the HNSW index.

Existing chunks were embedded with the old model, so their vectors are
incompatible with the new index. We DROP the rows on the way down (small
dataset, embeddings are deterministic from page content, the maintenance
scheduler will re-embed on its next tick).

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-05-02
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "c9d0e1f2a3b4"
down_revision: Union[str, None] = "b8c9d0e1f2a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop the HNSW index first (it references the column type and would
    # block the ALTER TYPE otherwise).
    op.execute("DROP INDEX IF EXISTS idx_brain_chunks_embedding")

    # Truncate existing chunks. They were embedded with text-embedding-3-large
    # at 1536-dim, useless for the new 768-dim index. The scheduler embeds
    # stale pages on its next tick, so rows reappear within minutes.
    op.execute("TRUNCATE brain_content_chunks RESTART IDENTITY")

    # Resize the embedding column. Cast through NULL because pgvector cannot
    # auto-truncate or pad between dimensions.
    op.execute(
        "ALTER TABLE brain_content_chunks "
        "ALTER COLUMN embedding TYPE vector(768) USING NULL::vector(768)"
    )

    # Recreate the HNSW index. m=16, ef_construction=64 are the pgvector
    # defaults that match what we use in qdrant_client.py for consistency.
    op.execute(
        "CREATE INDEX idx_brain_chunks_embedding "
        "ON brain_content_chunks USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_brain_chunks_embedding")
    op.execute("TRUNCATE brain_content_chunks RESTART IDENTITY")
    op.execute(
        "ALTER TABLE brain_content_chunks "
        "ALTER COLUMN embedding TYPE vector(1536) USING NULL::vector(1536)"
    )
    op.execute(
        "CREATE INDEX idx_brain_chunks_embedding "
        "ON brain_content_chunks USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )
