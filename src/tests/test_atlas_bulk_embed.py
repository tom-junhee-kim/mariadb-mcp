import asyncio
import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import atlas  # noqa: E402
import embeddings  # noqa: E402


class FakeEmb:
    """embed(text, model_name=None) 호출을 기록한다. fail_models 에 든 이름(None=기본 모델)으로 부르면 예외를 낸다."""

    def __init__(self, fail_models=()):
        self.calls = []
        self.fail_models = set(fail_models)

    async def embed(self, text, model_name=None):
        self.calls.append((text, model_name))
        if model_name in self.fail_models:
            raise RuntimeError("boom")
        return [0.0] * 1024


class BulkAddEmbedding(unittest.TestCase):
    def _run(self, emb, bulk):
        with patch.object(atlas, "BULK_EMBED_MODEL", bulk):
            return asyncio.run(atlas._embed_for_add(emb, "텍스트"))

    def test_no_bulk_uses_default_model(self):
        emb = FakeEmb()
        self._run(emb, None)
        self.assertEqual(emb.calls, [("텍스트", None)])

    def test_bulk_model_is_used_for_add(self):
        emb = FakeEmb()
        vec = self._run(emb, "bulk-x")
        self.assertEqual(emb.calls, [("텍스트", "bulk-x")])
        self.assertEqual(len(vec), 1024)

    def test_bulk_failure_falls_back_to_default_once(self):
        emb = FakeEmb(fail_models={"bulk-x"})
        with self.assertLogs(atlas.logger, level="WARNING") as cm:
            vec = self._run(emb, "bulk-x")
        self.assertEqual(emb.calls, [("텍스트", "bulk-x"), ("텍스트", None)])
        self.assertEqual(len(vec), 1024)
        self.assertTrue(any("bulk-x" in m for m in cm.output))

    def test_default_failure_after_fallback_propagates(self):
        emb = FakeEmb(fail_models={"bulk-x", None})
        with self.assertLogs(atlas.logger, level="WARNING"):
            with self.assertRaises(RuntimeError):
                self._run(emb, "bulk-x")
        self.assertEqual(len(emb.calls), 2)


class AddOneWiring(unittest.TestCase):
    """_add_one 이 add 전용 임베딩 함수를 쓰고, 검색(do_search)은 쓰지 않는다."""

    def test_add_one_embeds_with_bulk_model(self):
        from unittest.mock import AsyncMock, MagicMock
        cur, conn = AsyncMock(), AsyncMock()
        cur.fetchone = AsyncMock(return_value=None)   # 기존 행 없음 → 새로 넣는다
        conn.begin = AsyncMock()
        conn.commit = AsyncMock()
        conn.rollback = AsyncMock()
        emb = FakeEmb()
        ch = {"document": "본문", "chunk_no": 0, "metadata": {"machine": "m", "item_at": 1}}
        with patch.object(atlas, "BULK_EMBED_MODEL", "bulk-x"):
            res = asyncio.run(atlas._add_one(cur, conn, emb, "transcript", "k", ch, False))
        self.assertEqual(res["status"], "inserted")
        self.assertEqual(len(emb.calls), 1)
        self.assertEqual(emb.calls[0][1], "bulk-x")

    def test_search_path_does_not_use_bulk(self):
        import inspect
        self.assertNotIn("_embed_for_add", inspect.getsource(atlas.do_search))
        self.assertIn("emb.embed(QUERY_PREFIX + query)", inspect.getsource(atlas.do_search))


class BulkConfig(unittest.TestCase):
    def tearDown(self):
        importlib.reload(embeddings)   # 환경변수를 되돌린 상태로 모듈 값을 원상복구

    def test_env_adds_allowed_model_and_dimension(self):
        with patch.dict(os.environ, {"ATLAS_BULK_EMBED_MODEL": "bulk-x", "OPENAI_EMBEDDING_DIM": "1024"}):
            importlib.reload(embeddings)
            self.assertEqual(embeddings.BULK_EMBED_MODEL, "bulk-x")
            self.assertIn("bulk-x", embeddings.ALLOWED_OPENAI_MODELS)
            self.assertEqual(embeddings.OPENAI_MODEL_DIMENSIONS["bulk-x"], 1024)

    def test_env_unset_leaves_bulk_off(self):
        with patch.dict(os.environ):
            os.environ.pop("ATLAS_BULK_EMBED_MODEL", None)
            importlib.reload(embeddings)
            self.assertIsNone(embeddings.BULK_EMBED_MODEL)

    def test_blank_env_is_treated_as_unset(self):
        with patch.dict(os.environ, {"ATLAS_BULK_EMBED_MODEL": "   "}):
            importlib.reload(embeddings)
            self.assertIsNone(embeddings.BULK_EMBED_MODEL)


if __name__ == "__main__":
    unittest.main()
