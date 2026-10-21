"""아틀라스 도구 통합 시험 — 시험 표(`*_test`)에서만 돈다.

필요한 환경변수: ATLAS_TABLE_SUFFIX=_test, EMBEDDING_PROVIDER, OPENAI_API_KEY, OPENAI_BASE_URL,
IT_DB_HOST, IT_DB_PORT, IT_DB_USER, IT_DB_PASSWORD. 실행: python src/tests/integration_atlas.py
"""
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

assert os.getenv("ATLAS_TABLE_SUFFIX") == "_test", "시험 표(_test)에서만 실행한다"

import asyncmy  # noqa: E402

import atlas  # noqa: E402
from embeddings import EmbeddingService  # noqa: E402


class FakeServer:
    def __init__(self):
        self.pool = None

    async def _get_pool(self, instance_name=None):
        if self.pool is None:
            self.pool = await asyncmy.create_pool(
                host=os.environ["IT_DB_HOST"], port=int(os.environ["IT_DB_PORT"]), user=os.environ["IT_DB_USER"],
                password=os.environ["IT_DB_PASSWORD"], autocommit=True, minsize=1, maxsize=4, charset="utf8mb4")
        return self.pool


FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def chunk(no, count, doc, **meta):
    m = {"machine": "isD", "project": "it-proj", "item_at": 1_700_000_000_000 + no, "session_id": None}
    m.update(meta)
    return {"chunk_no": no, "chunk_count": count, "document": doc, "metadata": m}


async def q(server, sql, params=()):
    pool = await server._get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(cursor=asyncmy.cursors.DictCursor) as cur:
            await cur.execute(f"USE `{atlas.ATLAS_DB}`")
            await cur.execute(sql, params)
            return await cur.fetchall()


async def main():
    server, emb = FakeServer(), EmbeddingService()
    tag = uuid.uuid4().hex[:8]
    ws = f"it{tag}"
    key = f"{ws}/docs/atlas.md"
    docs = ["MariaDB 벡터 인덱스는 ORDER BY 가 거리 호출 그대로일 때만 쓰입니다. 커밋 cd2b506 참고.",
            "임베딩 서버는 단일 워커로 직렬 처리하며 512토큰을 넘으면 뒤를 버립니다.",
            "진행판 M-16 은 메모리 아틀라스 계획으로 닫았습니다. handoffs/atlas-test.md 확인."]
    try:
        # 1) 넣기
        r = await atlas.do_add(server, emb, "workspace_memory", key, [chunk(i, 3, d, doc_title="아틀라스 시험") for i, d in enumerate(docs)])
        check("add: 3 inserted", r["counts"] == {"inserted": 3}, str(r["counts"]))
        idr = await q(server, f"SELECT token FROM `{atlas.T_IDENTS}` WHERE source=%s AND origin_key=%s", ("workspace_memory", key))
        toks = {x["token"] for x in idr}
        check("idents: hash/board/handoff", {"cd2b506", "m-16", "handoffs/atlas-test.md"} <= toks, str(toks))

        # 2) 같은 호출 → 임베딩 없이 건너뜀
        t0 = time.time()
        r = await atlas.do_add(server, emb, "workspace_memory", key, [chunk(i, 3, d, doc_title="아틀라스 시험") for i, d in enumerate(docs)])
        dt = time.time() - t0
        check("add again: skipped_exists x3", r["counts"] == {"skipped_exists": 3}, str(r["counts"]))
        check("add again: 임베딩 없이 빠름(<0.6s)", dt < 0.6, f"{dt:.2f}s")

        # 3) 본문이 바뀌면 갈아 끼움
        r = await atlas.do_add(server, emb, "workspace_memory", key, [chunk(1, 3, docs[1] + " 수정됨", doc_title="아틀라스 시험")])
        check("add changed: replaced", r["counts"] == {"replaced": 1}, str(r["counts"]))
        n = await q(server, f"SELECT COUNT(*) AS n FROM `{atlas.T_CHUNKS}` WHERE source=%s AND origin_key=%s AND chunk_no=1", ("workspace_memory", key))
        check("replace: 행 1개 유지", n[0]["n"] == 1)

        # 4) only_if_newer
        r = await atlas.do_add(server, emb, "workspace_memory", key, [chunk(1, 3, docs[1] + " 오래된", item_at=1)], only_if_newer=True)
        check("only_if_newer: skipped_older", r["counts"] == {"skipped_older": 1}, str(r["counts"]))

        # 5) 조각 수가 줄면 넘치는 조각 정리
        r = await atlas.do_add(server, emb, "workspace_memory", key, [chunk(0, 2, docs[0], doc_title="아틀라스 시험"), chunk(1, 2, docs[1] + " 수정됨", doc_title="아틀라스 시험")])
        n = await q(server, f"SELECT COUNT(*) AS n FROM `{atlas.T_CHUNKS}` WHERE source=%s AND origin_key=%s", ("workspace_memory", key))
        check("chunk_count 감소: 넘치는 조각 삭제", n[0]["n"] == 2, str(n))
        n = await q(server, f"SELECT COUNT(*) AS n FROM `{atlas.T_IDENTS}` WHERE source=%s AND origin_key=%s AND chunk_no>=2", ("workspace_memory", key))
        check("chunk_count 감소: 식별자도 삭제", n[0]["n"] == 0, str(n))

        # 6) 필수 칸 누락·잘못된 source
        r = await atlas.do_add(server, emb, "workspace_memory", key + "x", [{"chunk_no": 0, "document": "x", "metadata": {"machine": "isD"}}])
        check("필수 metadata 누락: error", r["counts"] == {"error": 1}, str(r["counts"]))
        try:
            await atlas.do_add(server, emb, "bogus", key, [chunk(0, 1, "x")])
            check("잘못된 source 거부", False)
        except ValueError:
            check("잘못된 source 거부", True)

        # 7) get_atlas
        g = await atlas.do_get(server, "workspace_memory", key, session_id=f"s-{tag}", machine="isD")
        check("get: 조각 순서", [c["chunk_no"] for c in g["chunks"]] == [0, 1], str(g["found"]))
        ev = await q(server, f"SELECT COUNT(*) AS n FROM `{atlas.T_EVENTS}` WHERE event='opened' AND ref_origin_key=%s", (key,))
        check("get: opened 기록", ev[0]["n"] == 1, str(ev))

        # 8) 트랜스크립트 두 개(한 세션) + 머신 문서 사본 두 개(같은 내용, 다른 머신)
        sid = str(uuid.uuid4())
        await atlas.do_add(server, emb, "transcript", f"{sid}:10", [chunk(0, 1, "사용자: 임베딩 서버 직렬 처리 문제를 알려 주세요\n\n답변: 단일 워커입니다", session_id=sid, line_start=10, item_at=1_700_000_100_000)])
        await atlas.do_add(server, emb, "transcript", f"{sid}:50", [chunk(0, 1, "사용자: 서버 임베딩 직렬 처리 이야기를 다시\n\n답변: 단일 워커입니다", session_id=sid, line_start=50, item_at=1_700_000_200_000)])
        md = "## 임베딩 서버\nH55M 에서 도는 임베딩 서버는 GT1030 이고 단일 워커 직렬 처리다."
        await atlas.do_add(server, emb, "machine_doc", f"isD:{ws}.md#1", [chunk(0, 1, md, machine="isD")])
        await atlas.do_add(server, emb, "machine_doc", f"CS:{ws}.md#1", [chunk(0, 1, md, machine="CS")])

        s = await atlas.do_search(server, emb, "임베딩 서버 직렬 처리", return_n=20, top_n=3, machine="isD", session_id=f"s-{tag}", session_name="it")
        res = s["results"]
        mdocs = [c for c in res if c["source"] == "machine_doc" and ws in c["origin_key"]]
        check("search: 머신 문서 사본 접힘(1개, 같은 머신 우선)", len(mdocs) == 1 and mdocs[0]["machine"] == "isD", str([(c["origin_key"]) for c in mdocs]))
        check("search: 결과 있음·rank 1부터", res and res[0]["rank"] == 1)
        check("search: 점수 내림차순(식별자 후보 제외)", all(res[i]["distance"] <= res[i + 1]["distance"] for i in range(len(res) - 1) if res[i]["cand_from"] == res[i + 1]["cand_from"]))

        s2 = await atlas.do_search(server, emb, "임베딩 서버 직렬 처리", sources=["transcript"], return_n=20, exclude_session_id=sid)
        check("search: 세션 제외", not any(c["origin_key"].startswith(sid) for c in s2["results"]))
        s3 = await atlas.do_search(server, emb, "임베딩 서버 직렬 처리", sources=["transcript"], return_n=20, exclude_session_id=sid, exclude_after_line=30)
        keys3 = [c["origin_key"] for c in s3["results"] if c["origin_key"].startswith(sid)]
        check("search: 압축 경계 앞 조각만 허용", keys3 == [f"{sid}:10"], str(keys3))
        s4 = await atlas.do_search(server, emb, "임베딩 서버 직렬 처리", sources=["transcript"], return_n=20, before=1_700_000_150_000)
        keys4 = [c["origin_key"] for c in s4["results"] if c["origin_key"].startswith(sid)]
        check("search: before 시각 필터", keys4 == [f"{sid}:10"], str(keys4))

        # 9) 식별자 정확 일치 후보(필터가 붙은 SQL 포함)
        ikey = f"{ws}/docs/ident.md"
        await atlas.do_add(server, emb, "workspace_memory", ikey, [chunk(0, 1, "인계 문서 handoffs/atlas-test.md 는 아틀라스 시험용 인계 파일이다.", item_at=1_700_000_050_000)])
        qtext = "그 인계 파일 handoffs/atlas-test.md 내용이 뭐였지"
        s5 = await atlas.do_search(server, emb, qtext, sources=["workspace_memory"], return_n=5)
        top = s5["results"][0] if s5["results"] else None
        check("search: 식별자 후보 최상위·cand_from", top is not None and top["origin_key"] == ikey and top["cand_from"] in ("ident", "both"), str(top and (top["origin_key"], top["cand_from"])))
        check("search: meta.idents 에 토큰", "handoffs/atlas-test.md" in s5["meta"]["idents"])
        s5b = await atlas.do_search(server, emb, qtext, sources=["workspace_memory"], return_n=5, before=1_700_000_100_000, exclude_session_id=sid, exclude_after_line=30)
        check("search: 필터+식별자 SQL 동작", any(c["origin_key"] == ikey for c in s5b["results"]), str([c["origin_key"] for c in s5b["results"]]))
        s5c = await atlas.do_search(server, emb, qtext, sources=["workspace_memory"], return_n=5, before=1_700_000_010_000)
        check("search: before 가 식별자 후보에도 적용(대조군)", not any(c["origin_key"] == ikey for c in s5c["results"]))

        # 10) shown 기록
        ev = await q(server, f"SELECT COUNT(*) AS n FROM `{atlas.T_EVENTS}` WHERE event='shown' AND session_id=%s", (f"s-{tag}",))
        check("search: shown 기록(상위 3)", ev[0]["n"] == 3, str(ev))
        s6 = await atlas.do_search(server, emb, "임베딩 서버 직렬 처리", log_shown=False, session_id=f"s-{tag}-2")
        ev = await q(server, f"SELECT COUNT(*) AS n FROM `{atlas.T_EVENTS}` WHERE session_id=%s", (f"s-{tag}-2",))
        check("search: log_shown=False 면 기록 없음(대조군)", ev[0]["n"] == 0 and s6["results"])
    finally:
        # 내가 만든 시험 행만 지운다
        like = f"%{ws}%"
        for t in (atlas.T_IDENTS, atlas.T_CHUNKS):
            await q(server, f"DELETE FROM `{t}` WHERE origin_key LIKE %s OR origin_key LIKE %s", (like, f"{ws}%"))
        sids = await q(server, f"SELECT DISTINCT session_id FROM `{atlas.T_CHUNKS}` WHERE project='it-proj'")
        for x in sids:
            await q(server, f"DELETE FROM `{atlas.T_IDENTS}` WHERE origin_key LIKE %s", (f"{x['session_id']}:%",))
        await q(server, f"DELETE FROM `{atlas.T_CHUNKS}` WHERE project='it-proj'")
        await q(server, f"DELETE FROM `{atlas.T_EVENTS}` WHERE session_id LIKE %s OR ref_origin_key LIKE %s", (f"s-{tag}%", like))
        left = await q(server, f"SELECT COUNT(*) AS n FROM `{atlas.T_CHUNKS}` WHERE project='it-proj'")
        check("정리: 시험 행 0", left[0]["n"] == 0)
    print("RESULT", "OK" if not FAILS else f"FAIL {FAILS}")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
