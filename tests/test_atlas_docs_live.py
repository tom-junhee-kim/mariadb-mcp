"""atlas_docs 통합 시험 — 시험 표(ATLAS_TABLE_SUFFIX=_test)에 붙인 서버에 대고 돈다.

서버 환경: ATLAS_TABLE_SUFFIX=_test · ATLAS_DOCS_SYNC_SEC=2 · ATLAS_DOCS_STATE_SEC=1 · ATLAS_MACHINE_ADDRS=127.0.0.1=isD-MacBookPro
실행: python tests/test_atlas_docs_live.py http://127.0.0.1:19001   (운영 주소에 대고 돌리지 않는다)
"""
import asyncio
import json
import secrets
import sys
import time
import urllib.request

from mcp import ClientSession
from mcp.client.sse import sse_client

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:19001"
TAG = secrets.token_hex(3)
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"{'통과' if ok else '실패'} | {name} | {detail}"[:300])


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read())


async def tool(s, _tool, **args):
    r = await s.call_tool(_tool, args)
    t = r.content[0].text
    if r.isError:
        return {"_error": t}
    try:
        return json.loads(t)
    except ValueError:
        return {"_text": t}


async def main():
    async with sse_client(BASE + "/sse", timeout=10) as (rd, wr):
        async with ClientSession(rd, wr) as s:
            await s.initialize()
            v0 = get("/atlas/version")["rules_version"]

            # 1) 규칙은 늘 후보로
            name = f"t-rule-{TAG}"
            r = await tool(s, "put_doc", kind="rule", scope="global", name=name, body="**시험 규칙**\n\n본문 하나.",
                           base_version=0, reason="시험", header={"event": "PreToolUse", "action": "context"})
            check("rule put_doc → 후보", r.get("status") == "proposed", r.get("proposal"))
            pid, csha = r.get("proposal_id"), r.get("content_sha")

            # 2) 시험 해시 없이 승인 → 거부, 틀린 해시 → 거부, 맞는 해시 → 승인·판 번호 +1
            r = await tool(s, "decide_proposal", proposal_id=pid, decision="approve", approval_quote="승인.")
            check("rule 승인에 test_sha 없음 → 거부", "_error" in r and "test_sha" in r["_error"], r.get("_error", "")[:80])
            r = await tool(s, "decide_proposal", proposal_id=pid, decision="approve", approval_quote="승인.", test_sha="0000000000000000")
            check("틀린 test_sha → 거부", "_error" in r)
            r = await tool(s, "decide_proposal", proposal_id=pid, decision="approve", test_sha=csha)
            check("사람 승인 원문·상시 승인 없이 → 거부", "_error" in r and "approval_quote" in r["_error"])
            r = await tool(s, "decide_proposal", proposal_id=pid, decision="approve", approval_quote="승인.", test_sha=csha)
            check("맞는 test_sha + 승인 원문 → 승인", r.get("status") == "approved" and r.get("version") == 1, str(r)[:120])
            time.sleep(1.5)
            check("판 번호 +1(/atlas/version)", get("/atlas/version")["rules_version"] == v0 + 1)

            # 3) 같은 판에서 만든 후보 둘 — 둘째 승인은 충돌
            did = f"rule:global:{name}"
            a = await tool(s, "put_doc", kind="rule", scope="global", name=name, body="판 2-가", base_version=1, reason="가", header={})
            b = await tool(s, "put_doc", kind="rule", scope="global", name=name, body="판 2-나", base_version=1, reason="나", header={})
            ra = await tool(s, "decide_proposal", proposal_id=a["proposal_id"], decision="approve", approval_quote="P 가 승인", test_sha=a["content_sha"])
            rb = await tool(s, "decide_proposal", proposal_id=b["proposal_id"], decision="approve", approval_quote="P 나 승인", test_sha=b["content_sha"])
            check("같은 판 후보 둘: 첫째 승인", ra.get("status") == "approved")
            check("같은 판 후보 둘: 둘째는 충돌로 거부(앞 변경을 덮지 않음)", rb.get("status") == "conflict", str(rb)[:100])
            d = await tool(s, "get_doc", doc_id=did, with_history=True)
            check("지금 판은 첫째 후보 본문·이력 2판", d.get("body") == "판 2-가" and len(d.get("history", [])) == 2)

            # 4) put_doc 의 base_version 충돌
            r = await tool(s, "put_doc", kind="machine_doc", scope="machine:isD-MacBookPro", name=f"m-{TAG}", body="x",
                           base_version=5, reason="충돌 시험")
            check("새 문서에 base_version 5 → 충돌", r.get("status") == "conflict" and r.get("current_version") == 0)

            # 5) 바로 쓰는 범위와 후보로 가는 범위
            r = await tool(s, "put_doc", kind="machine_doc", scope="machine:isD-MacBookPro", name=f"m-{TAG}", body="자기 머신 문서",
                           base_version=0, reason="직접")
            check("자기 머신 범위 머신 문서 → 바로 씀", r.get("status") == "written" and r.get("version") == 1)
            r = await tool(s, "put_doc", kind="machine_doc", scope="machine:CS-15S-EQ3", name=f"m-{TAG}", body="남의 머신",
                           base_version=0, reason="남")
            check("다른 머신 범위 → 후보", r.get("status") == "proposed", r.get("why", ""))
            r = await tool(s, "put_doc", kind="machine_doc", scope="global", name=f"g-{TAG}", body="전역", base_version=0, reason="전역")
            check("전역 머신 문서 → 후보", r.get("status") == "proposed", r.get("why", ""))
            r = await tool(s, "put_doc", kind="workspace_memory", scope="ws:codescent", name=f"w-{TAG}", body="메모리",
                           base_version=0, reason="ws", caller_ws="codescent")
            check("자기 워크스페이스 메모리 → 바로 씀", r.get("status") == "written")
            r = await tool(s, "put_doc", kind="workspace_memory", scope="ws:komid", name=f"w-{TAG}", body="남의 ws",
                           base_version=0, reason="ws", caller_ws="codescent")
            check("다른 워크스페이스 메모리 → 후보", r.get("status") == "proposed")

            # 6) /atlas/rules — nonce 되돌림, since 같으면 unchanged, ws 규칙은 전환 표지가 db 일 때만
            n = secrets.token_hex(8)
            rr = get(f"/atlas/rules?ws=codescent&machine=isD-MacBookPro&nonce={n}")
            ids = {x["doc_id"] for x in rr.get("rules", [])}
            check("/atlas/rules echo = nonce", rr.get("echo") == n and did in ids, f"rules={len(ids)}")
            rr2 = get(f"/atlas/rules?ws=codescent&machine=isD-MacBookPro&since={rr['rules_version']}&nonce=x2")
            check("since 가 같으면 unchanged", rr2.get("unchanged") is True and rr2.get("echo") == "x2")
            wsname = f"t-ws-{TAG}"
            p = await tool(s, "put_doc", kind="rule", scope="ws:codescent", name=wsname, body="ws 규칙", base_version=0, reason="ws", header={})
            await tool(s, "decide_proposal", proposal_id=p["proposal_id"], decision="approve", approval_quote="ws 승인", test_sha=p["content_sha"])
            await tool(s, "set_cutover", workspace="codescent", machine="isD-MacBookPro", mode="file", approval_quote="시험 시작 상태")
            rr3 = get(f"/atlas/rules?ws=codescent&machine=isD-MacBookPro&nonce=x3")
            check("전환 표지 file → ws 규칙 빠짐", rr3["ws_mode"] == "file" and f"rule:ws:codescent:{wsname}" not in {x["doc_id"] for x in rr3["rules"]})
            await tool(s, "set_cutover", workspace="codescent", machine="isD-MacBookPro", mode="db", approval_quote="전환 승인")
            rr4 = get(f"/atlas/rules?ws=codescent&machine=isD-MacBookPro&nonce=x4")
            check("전환 표지 db → ws 규칙 들어옴", rr4["ws_mode"] == "db" and f"rule:ws:codescent:{wsname}" in {x["doc_id"] for x in rr4["rules"]})
            bad = urllib.request.Request(BASE + "/atlas/rules?ws=codescent")
            try:
                urllib.request.urlopen(bad, timeout=5); check("nonce 없는 요청 → 400", False)
            except urllib.error.HTTPError as e:
                check("nonce 없는 요청 → 400", e.code == 400)

            # 7) 조각 맞추기(rule·guide) — 기본 검색에는 섞이지 않고 sources 로만
            for _ in range(20):
                g = await tool(s, "get_atlas", source="rule", origin_key=did, record_open=False)
                if g.get("found"):
                    break
                time.sleep(1)
            check("rule 조각이 chunks 에 생김(chunk_version 맞춤)", g.get("found", 0) >= 1, f"found={g.get('found')}")
            await tool(s, "add_atlas", source="transcript", origin_key=f"sess-{TAG}:1",
                       chunks=[{"chunk_no": 0, "chunk_count": 1, "document": "판 2-가 와 비슷한 대화",
                                "metadata": {"machine": "isD-MacBookPro", "item_at": int(time.time() * 1000)}}])
            time.sleep(35)   # 메모리 색인 갱신 주기(30초)
            q = "판 2-가"
            r_def = await tool(s, "search_atlas", query=q, log_shown=False)
            srcs = {x["source"] for x in r_def.get("results", [])}
            check("sources 를 안 주면 rule·guide 가 나오지 않음(기본 검색 회귀)", not (srcs & {"rule", "guide"}) and "transcript" in srcs, str(srcs))
            r_rule = await tool(s, "search_atlas", query=q, sources=["rule"], log_shown=False)
            check("sources=['rule'] 로는 규칙 조각이 나옴", any(x["origin_key"] == did for x in r_rule.get("results", [])))
            sr = json.loads(urllib.request.urlopen(urllib.request.Request(BASE + "/atlas/search", data=json.dumps({"query": q, "log_shown": False}).encode(),
                                                                         headers={"Content-Type": "application/json"}), timeout=10).read())
            check("POST /atlas/search 가 search_atlas 와 같은 결과", [x["origin_key"] for x in sr.get("results", [])] == [x["origin_key"] for x in r_def.get("results", [])])

            # 8) 조정값 — 범위 밖 거부, 안이면 판 번호 +1
            tv0 = get("/atlas/version")["tuning_version"]
            r = await tool(s, "set_tuning", k="D:tool", v=0.9, reason="범위 밖")
            check("set_tuning 범위 밖 → 거부", "_error" in r)
            r = await tool(s, "set_tuning", k="D:tool", v=0.45, reason="시험")
            time.sleep(1.5)
            vv = get("/atlas/version")
            check("set_tuning 범위 안 → 값·판 번호 반영", vv["tuning"].get("D:tool") == 0.45 and vv["tuning_version"] == tv0 + 1)
            r = await tool(s, "set_tuning", k="rules_version", v=999, reason="손대기")
            check("rules_version 은 손으로 못 바꿈", "_error" in r)

            # 9) 가져오기 — 같으면 건너뜀, 바뀌면 새 판
            rows = [{"kind": "rule", "scope": "global", "name": f"imp-{TAG}", "header": {"x": 1}, "body": "가져온 규칙"}]
            r1 = await tool(s, "import_docs", rows=rows, by="test")
            r2 = await tool(s, "import_docs", rows=rows, by="test")
            check("import_docs: 처음 written, 다시 unchanged", r1["counts"]["written"] == 1 and r2["counts"]["unchanged"] == 1, f"{r1['counts']} {r2['counts']}")

            # 10) 상시 승인 — 범위 안 후보만
            p1 = await tool(s, "put_doc", kind="machine_doc", scope="global", name=f"sa-{TAG}", body="상시1", base_version=0, reason="상시")
            p2 = await tool(s, "put_doc", kind="machine_doc", scope="global", name=f"sb-{TAG}", body="상시2", base_version=0, reason="상시")
            st = await tool(s, "add_standing_approval", scope_desc="시험 묶음", match_rule={"proposal_ids": [p1["proposal_id"]]},
                            quote="전부 권고대로", quote_session="00000000-0000-0000-0000-000000000000", quote_line=1, approver="정종현")
            ok1 = await tool(s, "decide_proposal", proposal_id=p1["proposal_id"], decision="approve", standing_id=st["standing_id"])
            ok2 = await tool(s, "decide_proposal", proposal_id=p2["proposal_id"], decision="approve", standing_id=st["standing_id"])
            check("상시 승인: 범위 안 후보는 승인", ok1.get("status") == "approved")
            check("상시 승인: 범위 밖 후보는 거부", "_error" in ok2 and "does not cover" in ok2["_error"])

            # 11) 목록·통계
            ld = await tool(s, "list_docs", kind="rule", with_stats=True)
            check("list_docs(kind=rule, with_stats)", ld.get("count", 0) >= 3 and all("stats" in x for x in ld["docs"]))
            rs = await tool(s, "rule_stats", days=7)
            check("rule_stats 응답", "docs" in rs)
            mv = await tool(s, "move_doc", doc_id=did, to_scope="ws:codescent", reason="이동 시험")
            check("move_doc → 후보", mv.get("status") == "proposed")

            # 12) /atlas/docs — 열람 기록 없이 일괄 읽기, etag 는 판이 바뀔 때만 바뀜
            mdid = f"machine_doc:machine:isD-MacBookPro:m-{TAG}"
            cnt_sql = "SELECT COUNT(*) AS n FROM memory_atlas.events_test WHERE event='opened' AND ref_origin_key=%s"
            before = (await tool(s, "execute_sql", sql_query=cnt_sql, database_name="memory_atlas", parameters=[mdid]))
            d1 = get(f"/atlas/docs?kinds=machine_doc&scopes=global,machine:isD-MacBookPro&nonce=d1")
            d2 = get(f"/atlas/docs?kinds=machine_doc&scopes=global,machine:isD-MacBookPro&nonce=d2")
            after = (await tool(s, "execute_sql", sql_query=cnt_sql, database_name="memory_atlas", parameters=[mdid]))
            ids = {x["doc_id"] for x in d1.get("docs", [])}
            check("/atlas/docs: 요청 범위 문서가 본문과 함께 옴·다른 머신 범위는 안 옴",
                  mdid in ids and not any(x.startswith("machine_doc:machine:CS-15S-EQ3") for x in ids)
                  and all(x.get("body") is not None for x in d1["docs"]), f"count={d1.get('count')}")
            check("/atlas/docs: echo = nonce", d1.get("echo") == "d1" and d2.get("echo") == "d2")
            check("/atlas/docs: 판이 같으면 etag 같음", d1.get("etag") == d2.get("etag"))
            num = lambda r: r[0]["n"] if isinstance(r, list) and r and isinstance(r[0].get("n"), int) else None
            check("/atlas/docs: opened 를 남기지 않음", num(before) is not None and num(before) == num(after), f"{before} → {after}")
            await tool(s, "get_doc", doc_id=mdid)
            after2 = (await tool(s, "execute_sql", sql_query=cnt_sql, database_name="memory_atlas", parameters=[mdid]))
            check("대조군: get_doc 은 opened 를 남김", num(after) is not None and num(after2) == num(after) + 1, f"{after} → {after2}")
            await tool(s, "put_doc", kind="machine_doc", scope="machine:isD-MacBookPro", name=f"m-{TAG}", body="자기 머신 문서 2판",
                       base_version=1, reason="판 올림")
            d3 = get(f"/atlas/docs?kinds=machine_doc&scopes=global,machine:isD-MacBookPro&nonce=d3")
            check("/atlas/docs: 판이 바뀌면 etag 바뀜", d3.get("etag") != d1.get("etag"))
            try:
                urllib.request.urlopen(BASE + "/atlas/docs?kinds=bogus&scopes=global&nonce=z", timeout=5); check("틀린 kind → 400", False)
            except urllib.error.HTTPError as e:
                check("틀린 kind → 400", e.code == 400)

    print(f"시험 {len(RESULTS)}개 중 실패 {RESULTS.count(False)}개")
    sys.exit(1 if False in RESULTS else 0)


asyncio.run(main())
