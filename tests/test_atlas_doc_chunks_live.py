"""머신 문서·워크스페이스 메모리 조각을 서버가 만드는 경로의 통합 시험 — 시험 표(ATLAS_TABLE_SUFFIX=_test)에 붙인 서버에 대고 돈다.

서버 환경: ATLAS_TABLE_SUFFIX=_test · ATLAS_DOCS_SYNC_SEC=2 · ATLAS_DOCS_STATE_SEC=1 · ATLAS_MACHINE_ADDRS=127.0.0.1=isD-MacBookPro
실행: python tests/test_atlas_doc_chunks_live.py http://127.0.0.1:19001   (운영 주소에 대고 돌리지 않는다)
핵심: 전환 표지(ws_cutover)가 db 가 되기 전에는 옛 파일 업로더의 조각을 한 줄도 건드리지 않고, 켜진 뒤에는 업로더와 같은 키·같은 조각으로 제자리 교체한다.
"""
import asyncio
import json
import os
import secrets
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import doc_chunker  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.client.sse import sse_client  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:19001"
TAG = secrets.token_hex(3)
TOKEN = os.environ.get("ATLAS_TEST_TOKEN")
PIPELINE_TOOLS = ("add_atlas", "import_docs")
RESULTS = []
WAIT = 240   # 시험 표에 가져와 둔 다른 문서도 전환 범위 안이면 함께 맞춰지므로 넉넉히 기다린다(초)


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"{'통과' if ok else '실패'} | {name} | {detail}"[:300])


async def tool(s, _tool, **args):
    if TOKEN and _tool in PIPELINE_TOOLS and "token" not in args:
        args["token"] = TOKEN
    r = await s.call_tool(_tool, args)
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


async def rows(s, like):
    return await sql(s, "SELECT id, origin_key, chunk_no, chunk_count, document, machine, project, "
                        "JSON_VALUE(metadata,'$.file_hash') fh, JSON_VALUE(metadata,'$.doc_id') doc_id, JSON_VALUE(metadata,'$.section_title') st "
                        "FROM chunks_test WHERE source=%s AND origin_key LIKE %s ORDER BY origin_key, chunk_no", [like[0], like[1]])


async def wait_for(fn, what):
    for _ in range(WAIT):
        v = await fn()
        if v:
            return v
        await asyncio.sleep(1)
    print(f"  (기다리다 끝남: {what})")
    return None


def legacy_chunk(no, count, text, machine, project, item_at=1700000000000):
    return {"chunk_no": no, "chunk_count": count, "document": text, "before": "", "after": "",
            "metadata": {"machine": machine, "project": project, "item_at": item_at, "file_hash": "legacyhash0000", "chunker_version": "1",
                         "doc_title": "legacy", "path": "/ws/old.md"}}


BODY1 = ("---\nname: zz-chunk-{t}\ndescription: 조각 시험 문서\ntype: reference\n---\n\n머리 앞부분.\n\n## 가 절\n가 절 본문입니다.\n\n## 나 절\n나 절 본문입니다.\n")
BODY2 = ("---\nname: zz-chunk-{t}\ndescription: 조각 시험 문서\ntype: reference\n---\n\n머리 앞부분.\n\n## 가 절\n가 절 본문 고침.\n")


async def main():
    host, other = "isD-MacBookPro", "JYGFRESH-JHCHEONG"
    async with sse_client(BASE + "/sse", timeout=10) as (rd, wr):
        async with ClientSession(rd, wr) as s:
            await s.initialize()
            name = f"zz-chunk-{TAG}"
            lk = f"{host}:{name}.md"
            doc_id = f"machine_doc:machine:{host}:{name}"

            # 1) 업로더가 만든 옛 조각을 시험 표에 심고, 같은 문서를 put_doc 으로 올린다(전환 표지 없음)
            for no, txt in enumerate(["옛 머리", "옛 가", "옛 나"]):
                r = await tool(s, "add_atlas", source="machine_doc", origin_key=f"{lk}#{no}", chunks=[legacy_chunk(0, 1, txt, host, "machine")])
                check(f"옛 조각 심기 #{no}", r.get("counts", {}).get("inserted") == 1, str(r.get("counts")))
            r = await tool(s, "put_doc", kind="machine_doc", scope=f"machine:{host}", name=name, body=BODY1.format(t=TAG), base_version=0,
                           reason="시험", header={"legacy_origin_key": lk, "file": f"{name}.md"})
            check("put_doc → 바로 씀", r.get("status") == "written" and r.get("version") == 1, str(r)[:100])
            before = await rows(s, ("machine_doc", f"{lk}%"))
            await asyncio.sleep(7)   # 맞춤 주기 두세 번
            after = await rows(s, ("machine_doc", f"{lk}%"))
            check("전환 표지 없음 → 옛 조각 그대로(행 id·본문 같음)", before == after and len(after) == 3, f"{len(before)}→{len(after)}")
            cv = await sql(s, "SELECT version, chunk_version FROM docs_test WHERE doc_id=%s", [doc_id])
            check("전환 표지 없음 → chunk_version 도 그대로(판과 다름)", cv and cv[0]["chunk_version"] != cv[0]["version"], str(cv))

            # 2) 전환 표지 db → 업로더와 같은 키·조각으로 제자리 교체
            r = await tool(s, "set_cutover", workspace="@machine_doc", machine=host, mode="db", approval_quote="시험 전환")
            check("set_cutover(@machine_doc, 호스트, db)", r.get("status") == "set", str(r)[:100])
            expect_groups = doc_chunker.doc_groups("machine_doc", lk, BODY1.format(t=TAG), {"doc_title": name})
            expect = {(k, rec["chunk_no"]): rec for k, recs in expect_groups for rec in recs}

            async def replaced():
                got = await rows(s, ("machine_doc", f"{lk}%"))
                return got if got and all(x["fh"] in (None, "") for x in got) and len(got) == len(expect) else None
            got = await wait_for(replaced, "서버가 조각을 만듦")
            check("전환 뒤 서버가 만든 조각 수 = 이식본 기대", bool(got) and len(got) == len(expect), f"{len(got or [])} vs {len(expect)}")
            if got:
                same = all(expect[(x["origin_key"], x["chunk_no"])]["document"] == x["document"] for x in got if (x["origin_key"], x["chunk_no"]) in expect)
                check("본문·키 = doc_chunker 출력(옛 키 유지)", same and {(x["origin_key"], x["chunk_no"]) for x in got} == set(expect))
                check("머신 칸 = 호스트, 프로젝트 = machine, doc_id 기록", all(x["machine"] == host and x["project"] == "machine" and x["doc_id"] == doc_id for x in got))
            cv = await sql(s, "SELECT version, chunk_version FROM docs_test WHERE doc_id=%s", [doc_id])
            check("chunk_version = version", bool(cv) and cv[0]["chunk_version"] == cv[0]["version"], str(cv))

            # 3) 다시 맞춰도 변하지 않음, 절이 줄면 남는 키를 지움
            ids1 = {(x["origin_key"], x["chunk_no"]): x["id"] for x in (got or [])}
            r = await tool(s, "put_doc", kind="machine_doc", scope=f"machine:{host}", name=name, body=BODY2.format(t=TAG), base_version=1, reason="절 줄임")
            check("2판 쓰기", r.get("version") == 2, str(r)[:80])

            async def shrunk():
                g = await rows(s, ("machine_doc", f"{lk}%"))
                return g if g and all(x["origin_key"] != f"{lk}#2" for x in g) else None
            g2 = await wait_for(shrunk, "남는 절 키 삭제")
            keys2 = {x["origin_key"] for x in (g2 or [])}
            check("절이 줄면 남는 키(#2)가 지워짐", bool(g2) and f"{lk}#2" not in keys2, str(sorted(keys2)))
            idents = await sql(s, "SELECT COUNT(*) n FROM chunk_idents_test WHERE source='machine_doc' AND origin_key=%s", [f"{lk}#2"])
            check("지운 키의 식별자도 없음", idents and int(idents[0]["n"]) == 0, str(idents))
            unchanged = [x for x in (g2 or []) if ids1.get((x["origin_key"], x["chunk_no"])) == x["id"] and x["origin_key"] == f"{lk}#0"]
            check("본문이 같은 절(#0, 머리 앞부분)은 행이 그대로(재임베딩 없음)", len(unchanged) == 1, f"{len(unchanged)}")

            # 4) 전역 문서: 전환 표지 'global' → 한 벌만 남기고 다른 머신 사본을 지움
            gname = f"zz-g-{TAG}"
            await tool(s, "add_atlas", source="machine_doc", origin_key=f"{host}:{gname}.md#0", chunks=[legacy_chunk(0, 1, "전역 옛 isD", host, "machine")])
            await tool(s, "add_atlas", source="machine_doc", origin_key=f"{other}:{gname}.md#0", chunks=[legacy_chunk(0, 1, "전역 옛 JYG", other, "machine")])
            p = await tool(s, "put_doc", kind="machine_doc", scope="global", name=gname, body="## 전역 절\n전역 본문.\n", base_version=0, reason="시험",
                           header={"legacy_origin_key": f"{host}:{gname}.md", "file": f"{gname}.md"})
            await tool(s, "decide_proposal", proposal_id=p["proposal_id"], decision="approve", approval_quote="시험 승인")
            await asyncio.sleep(6)
            g_before = await rows(s, ("machine_doc", f"%:{gname}.md#%"))
            check("전역 문서도 표지 없으면 옛 사본 둘 그대로", len(g_before) == 2 and all(x["fh"] == "legacyhash0000" for x in g_before), str(len(g_before)))
            await tool(s, "set_cutover", workspace="@machine_doc", machine="global", mode="db", approval_quote="전역 전환")

            async def only_one():
                g = await rows(s, ("machine_doc", f"%:{gname}.md#%"))
                return g if g and len(g) == 1 and g[0]["origin_key"].startswith(host) and g[0]["fh"] in (None, "") else None
            g_after = await wait_for(only_one, "전역 한 벌")
            check("전역 전환 → 다른 머신 사본 삭제·한 벌은 서버 조각(machine=global)", bool(g_after) and g_after[0]["machine"] == "global", str(g_after and g_after[0]["origin_key"]))

            # 5) 퇴역 → 조각 삭제
            rp = await tool(s, "put_doc", kind="machine_doc", scope=f"machine:{host}", name=name, body="x", base_version=2, reason="퇴역", retire=True)
            await tool(s, "decide_proposal", proposal_id=rp["proposal_id"], decision="approve", approval_quote="퇴역 승인")

            async def gone():
                g = await rows(s, ("machine_doc", f"{lk}%"))
                return True if not g else None
            check("퇴역 → 조각 모두 삭제", bool(await wait_for(gone, "퇴역 삭제")))

            # 6) 워크스페이스 메모리: 그 워크스페이스의 모든 머신 행이 db 여야 서버가 만든다
            ws, wname = f"zzws{TAG}", f"zz-mem-{TAG}"
            wkey = f"{ws}-claude-workspace/.claude/memory/{wname}.md"
            await tool(s, "add_atlas", source="workspace_memory", origin_key=wkey, chunks=[legacy_chunk(0, 1, "옛 메모리", "CS-15S-EQ3", f"{ws}-claude-workspace")])
            await tool(s, "put_doc", kind="workspace_memory", scope=f"ws:{ws}", name=wname, body="---\nname: m\n---\n새 메모리 본문.\n", base_version=0,
                       reason="시험", header={"legacy_origin_key": wkey}, caller_ws=ws)
            await tool(s, "set_cutover", workspace=ws, machine=host, mode="db", approval_quote="한 머신만")
            await tool(s, "set_cutover", workspace=ws, machine="CS-15S-EQ3", mode="file", approval_quote="다른 머신은 아직")
            await asyncio.sleep(6)
            w1 = await rows(s, ("workspace_memory", wkey))
            check("한 머신이라도 file 이면 서버가 안 만듦(옛 조각 그대로)", len(w1) == 1 and w1[0]["document"] == "옛 메모리" and w1[0]["fh"] == "legacyhash0000")
            await tool(s, "set_cutover", workspace=ws, machine="CS-15S-EQ3", mode="db", approval_quote="모두 db")

            async def mem_done():
                w = await rows(s, ("workspace_memory", wkey))
                return w if w and w[0]["fh"] in (None, "") and w[0]["document"].startswith("---") else None
            w2 = await wait_for(mem_done, "워크스페이스 메모리 전환")
            check("모든 머신 db → 서버가 만든 메모리 조각(키 유지·project = 키의 폴더 이름)",
                  bool(w2) and w2[0]["origin_key"] == wkey and w2[0]["project"] == f"{ws}-claude-workspace", str(w2 and w2[0]["project"]))

            # 7) 정리 — 시험 표의 시험 행
            await sql(s, "DELETE FROM chunk_idents_test WHERE origin_key LIKE %s OR origin_key LIKE %s OR origin_key LIKE %s", [f"%{TAG}%", f"%{TAG}%", f"%{TAG}%"])
            await sql(s, "DELETE FROM chunks_test WHERE origin_key LIKE %s", [f"%{TAG}%"])
            await sql(s, "DELETE FROM ws_cutover_test WHERE workspace=%s OR (workspace='@machine_doc' AND machine IN (%s,'global'))", [ws, host])
            await sql(s, "DELETE FROM docs_test WHERE name IN (%s,%s,%s)", [name, gname, wname])

    print(f"시험 {len(RESULTS)}개 중 실패 {RESULTS.count(False)}개")
    sys.exit(1 if False in RESULTS else 0)


asyncio.run(main())
