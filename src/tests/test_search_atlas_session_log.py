"""search_atlas(MCP 도구)가 세션 ID 없이 불렸을 때 shown 을 기록하지 않는지 — 서버·DB 없이 도는 단위 시험.

do_search 는 가짜로 바꿔 어떤 log_shown 이 넘어가는지만 본다. 대조군:
  - 세션 ID 가 있으면 log_shown 이 그대로(True)
  - 호출자가 log_shown=False 를 주면 세션 ID 가 있어도 False 그대로(건드리지 않음)
  - HTTP `/atlas/search` 경로는 이 감싸개를 거치지 않고 do_search 를 직접 부르므로 영향이 없음을 소스 줄로 확인
"""
import asyncio
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import atlas  # noqa: E402


class _FakeMcp:
    def __init__(self):
        self.tools = {}

    def tool(self, fn):
        self.tools[fn.__name__] = fn
        return fn

    def custom_route(self, *args, **kwargs):
        return lambda fn: fn


class _FakeServer:
    def __init__(self):
        self.mcp = _FakeMcp()


def _tool():
    server = _FakeServer()
    with mock.patch.object(atlas, "EXACT_MEMORY", False):
        atlas.register_atlas_tools(server, object())
    return server.mcp.tools["search_atlas"]


async def _call(search_atlas, **kw):
    seen = {}

    async def fake_do_search(server, emb, query, sources, per_source_n, return_n, top_n, before, exclude_session_id,
                             exclude_after_line, machine, project, session_id, session_name, log_shown, *rest):
        seen["log_shown"] = log_shown
        seen["session_id"] = session_id
        return {"results": [], "meta": {}}

    with mock.patch.object(atlas, "do_search", fake_do_search):
        await search_atlas("질의", **kw)
    return seen


class SearchAtlasSessionLog(unittest.TestCase):
    def setUp(self):
        self.search_atlas = _tool()

    def test_세션_ID_없음은_shown_을_기록하지_않는다(self):
        seen = asyncio.run(_call(self.search_atlas))
        self.assertIs(seen["log_shown"], False)

    def test_세션_ID_빈_문자열도_기록하지_않는다(self):
        seen = asyncio.run(_call(self.search_atlas, session_id=""))
        self.assertIs(seen["log_shown"], False)

    def test_세션_ID_있으면_기본값_그대로_기록한다(self):
        seen = asyncio.run(_call(self.search_atlas, session_id="11111111-2222-3333-4444-555555555555"))
        self.assertIs(seen["log_shown"], True)
        self.assertEqual(seen["session_id"], "11111111-2222-3333-4444-555555555555")

    def test_호출자가_log_shown_False_를_주면_그대로_False(self):
        seen = asyncio.run(_call(self.search_atlas, session_id="11111111-2222-3333-4444-555555555555", log_shown=False))
        self.assertIs(seen["log_shown"], False)

    def test_HTTP_경로는_감싸개를_거치지_않고_do_search_를_직접_부른다(self):
        # 훅·파이프라인이 쓰는 /atlas/search 가 이번 수정의 영향 밖임을 소스로 확인(대조: 감싸개에만 session_id 검사가 있음)
        http = (Path(__file__).resolve().parents[1] / "atlas_docs.py").read_text(encoding="utf-8")
        body = http[http.index('"/atlas/search"'):]
        body = body[:body.index("@mcp.tool")]
        self.assertIn("atlas.do_search(", body)
        self.assertNotIn("search_atlas", body)
        self.assertIsNone(re.search(r"if not session_id", body))
        core = (Path(__file__).resolve().parents[1] / "atlas.py").read_text(encoding="utf-8")
        wrapper = core[core.index("async def search_atlas"):]
        self.assertIn("if not session_id:", wrapper)


if __name__ == "__main__":
    unittest.main()
