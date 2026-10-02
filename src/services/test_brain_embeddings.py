from types import SimpleNamespace

import pytest

from src.services import brain_embeddings


class _DeletedPageBrain:
    async def get_page(self, conn, slug):
        return SimpleNamespace(compiled_truth="A field observation", timeline="")

    async def get_timeline(self, conn, slug, limit):
        return []

    async def upsert_chunks(self, conn, slug, chunks):
        raise ValueError(f"Page not found: {slug}")


@pytest.mark.asyncio
async def test_embed_page_discards_result_when_page_was_deleted(monkeypatch):
    async def fake_embeddings(chunks):
        return [[0.1, 0.2] for _ in chunks], "test-model"

    monkeypatch.setattr(brain_embeddings, "_get_embeddings", fake_embeddings)

    count = await brain_embeddings.embed_page(
        object(), _DeletedPageBrain(), "deleted-during-embedding"
    )

    assert count == 0


@pytest.mark.asyncio
async def test_embed_page_does_not_hide_unrelated_value_errors(monkeypatch):
    async def fake_embeddings(chunks):
        return [[0.1, 0.2] for _ in chunks], "test-model"

    class BrokenBrain(_DeletedPageBrain):
        async def upsert_chunks(self, conn, slug, chunks):
            raise ValueError("invalid embedding dimensions")

    monkeypatch.setattr(brain_embeddings, "_get_embeddings", fake_embeddings)

    with pytest.raises(ValueError, match="invalid embedding dimensions"):
        await brain_embeddings.embed_page(object(), BrokenBrain(), "still-present")
