"""search_atlas 의 skip_log_keys 통합 시험 — 시험 표(ATLAS_TABLE_SUFFIX=_test)에 붙인 서버에 대고 돈다.

서버 환경: ATLAS_TABLE_SUFFIX=_test
실행: python tests/test_atlas_search_skip_log_live.py http://127.0.0.1:19001   (운영 주소에 대고 돌리지 않는다)
핵심: 호출 쪽이 이미 보여 줘서 싣지 않은 조각은 events 에 shown 으로 남지 않는다. 검색 결과·shown 플래그·기본 동작은 그대로다.
  - 양성 기본: skip_log_keys 없이 부르면 shown 인 조각 전부가 기록된다(회귀)
  - 양성 skip: 목록에 든 조각만 빠지고 나머지는 기록된다
  - 대조군: 목록에 없는 키는 아무것도 빼지 않는다 · log_shown=false 면 한 줄도 없다
시험이 심은 조각·이벤트는 끝에 지운다(시험 표만).
"""
import asyncio
import json
import secrets
import sys
import uuid

from mcp import ClientSession
from mcp.client.sse import sse_client

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:19001"
TAG = secrets.token_hex(4)
HOST = "isD-MacBookPro"
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"{'통과' if ok else '실패'} | {name} | {detail}"[:300])


async def tool(s, _tool, **args):
    r = await s.call_tool(_tool, args)
    if not r.content:
        return [] if _tool == "execute_sql" else {}
    t = r.content[0].text
    if r.isError:
        return {"_error": t}
    try:
        return json.loads(t)
    except ValueError:
        return {"_text": t}


async def sql(s, q, params=None):
    r = await tool(s, "execute_sql", sql_query=q, database_name="memory_atlas", parameters=params or [])
    return r if isinstance(r, list) else r.get("result", r)


def chunk(text):
    return {"chunk_no": 0, "chunk_count": 1, "document": text, "before": "", "after": "",
            "metadata": {"machine": HOST, "project": "machine", "item_at": 1700000000000, "file_hash": "skiplogtest0000",
                         "chunker_version": "1", "doc_title": "skiplog", "path": "/ws/skiplog.md"}}


def key(c):
    return f"{c['source']}:{c['origin_key']}#{c['chunk_no']}"


async def search(s, sid, **extra):
    q = f"zz{TAG} 건너뛰기 기록 시험 문서 본문"
    d = await tool(s, "search_atlas", query=q, sources=["machine_doc"], top_n=3, per_source_n=15, return_n=10,
                   machine=HOST, session_id=sid, **extra)
    return d.get("results", d.get("result", {}).get("results", [])) if isinstance(d, dict) else []


async def logged(s, sid):
    rows = await sql(s, "SELECT ref_source, ref_origin_key, ref_chunk_no FROM events_test WHERE session_id=%s AND event='shown'", [sid])
    return {f"{r['ref_source']}:{r['ref_origin_key']}#{r['ref_chunk_no']}" for r in rows}


async def main():
    origin_keys = [f"{HOST}:zz{TAG}-{i}.md#0" for i in range(4)]
    sids = [str(uuid.uuid4()) for _ in range(4)]
    async with sse_client(BASE + "/sse", timeout=10) as (rd, wr):
        async with ClientSession(rd, wr) as s:
            await s.initialize()
            try:
                for i, ok_ in enumerate(origin_keys):
                    r = await tool(s, "add_atlas", source="machine_doc", origin_key=ok_,
                                   chunks=[chunk(f"zz{TAG} 건너뛰기 기록 시험 문서 본문 {i}번 조각")])
                    check(f"조각 심기 #{i}", r.get("counts", {}).get("inserted") == 1, str(r.get("counts") or r)[:80])

                # 1) 기본 — skip_log_keys 없음
                # 심은 조각이 검색 인덱스에 들어올 때까지 기다린다 — 기록이 남은 시도는 지워 다음 시도와 섞지 않는다
                for _ in range(60):
                    res = await search(s, sids[0], log_shown=False)
                    if sum(1 for c in res if c["shown"]) >= 3:
                        break
                    await asyncio.sleep(1)
                res = await search(s, sids[0])
                shown = {key(c) for c in res if c["shown"]}
                check("검색이 심은 조각을 shown 으로 돌려줌(3건)", len(shown) == 3 and all(f"zz{TAG}" in k for k in shown), str(sorted(shown))[:120])
                check("기본: shown 인 조각 전부가 기록됨(회귀)", await logged(s, sids[0]) == shown)

                # 2) skip — 한 건 뺌
                first = sorted(shown)[0]
                res2 = await search(s, sids[1], skip_log_keys=[first])
                shown2 = {key(c) for c in res2 if c["shown"]}
                check("skip: 검색 결과·shown 플래그는 그대로", shown2 == shown, str(sorted(shown2))[:120])
                got = await logged(s, sids[1])
                check("skip: 뺀 조각은 기록 안 됨", first not in got)
                check("skip: 나머지 shown 은 기록됨", got == shown - {first}, f"{len(got)}건")

                # 3) 대조군 — 목록에 없는 키
                res3 = await search(s, sids[2], skip_log_keys=["machine_doc:없는키#9"])
                check("대조군: 목록에 없는 키는 아무것도 빼지 않음", await logged(s, sids[2]) == {key(c) for c in res3 if c["shown"]} == shown)
                # 4) 대조군 — log_shown false
                await search(s, sids[3], log_shown=False, skip_log_keys=[first])
                check("대조군: log_shown=false 면 기록 0줄", await logged(s, sids[3]) == set())
            finally:
                # 앞선 시험이 남긴 조각까지 같은 꼴(zz<태그>-n.md)이면 함께 지운다 — 시험 표만 대상, 커밋해야 남지 않는다
                pat = f"{HOST}:zz%-%.md#0"
                await sql(s, "DELETE FROM events_test WHERE ref_source='machine_doc' AND ref_origin_key LIKE %s", [pat])
                await sql(s, "DELETE FROM chunks_test WHERE source='machine_doc' AND origin_key LIKE %s", [pat])
                await sql(s, "COMMIT")
                left_e = await sql(s, "SELECT COUNT(*) n FROM events_test WHERE ref_origin_key LIKE %s", [pat])
                left_c = await sql(s, "SELECT COUNT(*) n FROM chunks_test WHERE origin_key LIKE %s", [pat])
                check("정리: 시험 표에 남은 이벤트·조각 0", left_e[0]["n"] == 0 and left_c[0]["n"] == 0, f"{left_e} {left_c}")
    print(f"결과: {'통과' if all(RESULTS) else '실패 ' + str(RESULTS.count(False)) + '건'}")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
