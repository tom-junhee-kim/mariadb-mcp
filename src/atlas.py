# atlas.py — 메모리 아틀라스 도구 3개: search_atlas · get_atlas · add_atlas
#
# DB `memory_atlas` 의 표 chunks / chunk_idents / events 를 다룬다. 스키마와 규칙의 정본은
# 워크스페이스 플랜(메모리 아틀라스)이다. 기존 도구(search_vector_store 등)는 건드리지 않는다.
#
# - add_atlas    : 조각을 넣는다. 키·버전·해시가 같으면 임베딩 전에 건너뛰고, 다르면 갈아 끼우고,
#                  식별자(해시·경로·진행판 번호·인계 파일)를 서버에서 뽑아 chunk_idents 에 함께 넣는다.
# - get_atlas    : (source, origin_key) 로 조각 전체를 연다. 연 기록(opened)을 events 에 남긴다.
# - search_atlas : 종류별 상위 n + 시각·세션 제외 + 식별자 정확 일치. 보여 준 조각(shown)을 events 에 남긴다.
import asyncio
import hashlib
import json
import math
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import asyncmy

from config import logger
from atlas_mem import MemoryIndex

ATLAS_DB = os.getenv("ATLAS_DB", "memory_atlas")
_SUFFIX = os.getenv("ATLAS_TABLE_SUFFIX", "")          # 시험용: "_test" → chunks_test 등
ATLAS_INSTANCE = os.getenv("ATLAS_INSTANCE", "codescent")
# 벡터 인덱스는 행이 많은 종류에만 쓴다. 나머지는 인덱스 후 사후 필터가 후보를 잃으므로 정확 계산(거리 + 0 으로 인덱스 회피)한다.
INDEX_SOURCES = set(filter(None, os.getenv("ATLAS_INDEX_SOURCES", "transcript").split(",")))
EF_SEARCH = int(os.getenv("ATLAS_EF_SEARCH", "0"))     # 인덱스 탐색 폭 하한. 0 이면 INDEX_DEPTH 만큼
INDEX_DEPTH = int(os.getenv("ATLAS_INDEX_DEPTH", "300"))
# 메모리 정확 계산(atlas_mem.py). 켜면 벡터를 프로세스 메모리에 올려 코사인을 정확히 계산하고, 준비되기 전의 검색은 위 인덱스 경로로 대신한다.
IDENT_MAX_TOKENS = int(os.getenv("ATLAS_IDENT_MAX_TOKENS", "10"))   # 질의에서 정확 일치를 찾는 식별자 수
IDENT_PER_TOKEN = int(os.getenv("ATLAS_IDENT_PER_TOKEN", "30"))      # 토큰마다 최신순으로 받는 조각 수
EXACT_MEMORY = os.getenv("ATLAS_EXACT_MEMORY", "") == "1"
# 순위식 C4(관련성 + 최근성): score = (1 − 거리) + γ·exp(−δ·경과일). 시간 항은 대화 턴에만 걸고 밀린 수집(burst) 턴은 0 이다.
# 값은 사람 기준 판정 시험(reports/atlas-ranking-eval-20261023.md, 워크스페이스)에서 골랐다
RANK_C4_GAMMA = float(os.getenv("ATLAS_RANK_C4_GAMMA", "0.3"))
RANK_C4_DELTA = float(os.getenv("ATLAS_RANK_C4_DELTA", "0.01"))   # 하루당
_DAY_MS = 86_400_000
MEM: Optional[MemoryIndex] = None   # 인덱스에서 받는 후보 수(필터는 받은 뒤 파이썬에서 건다)
T_CHUNKS = f"chunks{_SUFFIX}"
T_IDENTS = f"chunk_idents{_SUFFIX}"
T_EVENTS = f"events{_SUFFIX}"
Q_CHUNKS = f"`{ATLAS_DB}`.`{T_CHUNKS}`"
Q_IDENTS = f"`{ATLAS_DB}`.`{T_IDENTS}`"
Q_EVENTS = f"`{ATLAS_DB}`.`{T_EVENTS}`"
SOURCES = ("transcript", "auto_memory", "workspace_memory", "machine_doc")
EMBED_MODEL_TAG = os.getenv("ATLAS_EMBED_MODEL_TAG", "bge-m3-korean/1024")   # 저장 행의 metadata.embed_model
_QP = os.getenv("ATLAS_QUERY_PREFIX", "").strip()   # 질의 임베딩 앞에 붙이는 접두어(arctic-embed 는 "query:"). env-file 은 값 끝 공백을 보존하지 않으므로 공백 한 칸은 코드가 붙인다
QUERY_PREFIX = _QP + " " if _QP else ""   # 임베딩 서버가 이 접두어("query: ")로 질의를 알아본다
MAX_CHUNKS_PER_CALL = 16
MAX_DOC_CHARS = 8000

# ---------------------------------------------------------------------------
# 식별자 추출 — 규칙은 ident_rules.json 한 곳. 저장(add_atlas)과 질의(search_atlas)가 같은 코드를 쓴다.
# ---------------------------------------------------------------------------
_RULES_PATH = Path(__file__).resolve().parent / "ident_rules.json"
_RULES_RAW = _RULES_PATH.read_bytes()
IDENT_RULES_SHA = hashlib.sha256(_RULES_RAW).hexdigest()[:16]
_RULES_DOC = json.loads(_RULES_RAW.decode("utf-8"))
IDENT_VERSION = str(_RULES_DOC["version"])
_LOWER = bool(_RULES_DOC.get("lowercase", True))
_MAX_TOKEN = int(_RULES_DOC.get("max_token_len", 160))
_MAX_PER_TEXT = int(_RULES_DOC.get("max_per_text", 60))
_RULES: List[Tuple[str, "re.Pattern[str]"]] = []
for _r in _RULES_DOC["rules"]:
    _flags = re.IGNORECASE if "i" in _r.get("flags", "") else 0
    _RULES.append((_r["type"], re.compile(_r["regex"], _flags)))
logger.info(f"atlas ident rules loaded: version={IDENT_VERSION} sha256={IDENT_RULES_SHA}")


def extract_idents(text: str) -> List[Tuple[str, str]]:
    """text 에서 (token, type) 목록을 뽑는다. 규칙 순서대로, 토큰 중복은 처음 것만, 상한 초과분은 버린다."""
    seen: Dict[str, str] = {}
    for rtype, rx in _RULES:
        for m in rx.finditer(text or ""):
            tok = m.group(0)
            if _LOWER:
                tok = tok.lower()
            if not tok or len(tok) > _MAX_TOKEN or tok in seen:
                continue
            seen[tok] = rtype
            if len(seen) >= _MAX_PER_TEXT:
                return list(seen.items())
    return list(seen.items())


# ---------------------------------------------------------------------------
# 공통 도우미
# ---------------------------------------------------------------------------
def _check_source(source: str) -> None:
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}: {source!r}")


def _check_key(origin_key: str) -> None:
    if not origin_key or not isinstance(origin_key, str) or len(origin_key) > 400:
        raise ValueError("origin_key must be a non-empty string of at most 400 characters")


def _hash16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _vec_text(vec: List[float]) -> str:
    return json.dumps([float(x) for x in vec])


def _event_uid(machine: str, session_id: str, event: str, source: str, origin_key: str, chunk_no: Optional[int]) -> str:
    raw = "|".join([machine, session_id, event, source, origin_key, "" if chunk_no is None else str(chunk_no), str(time.time_ns() // 1_000_000)])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


async def _log_events(cur, rows: List[Dict[str, Any]]) -> None:
    """events 에 기록한다. 실패해도 호출한 도구는 실패시키지 않는다."""
    if not rows:
        return
    sql = (f"INSERT IGNORE INTO {Q_EVENTS} (event, event_at, ref_source, ref_origin_key, ref_chunk_no, machine, project, "
           "session_id, session_name, prompt_sha1, distance, rank_no, event_uid) "
           "VALUES (%s, UTC_TIMESTAMP(3), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)")
    try:
        for r in rows:
            await cur.execute(sql, (r["event"], r["source"], r["origin_key"], r.get("chunk_no"), r.get("machine") or "unknown",
                                    r.get("project"), r.get("session_id") or "", r.get("session_name"), r.get("prompt_sha1"),
                                    r.get("distance"), r.get("rank_no"),
                                    _event_uid(r.get("machine") or "unknown", r.get("session_id") or "", r["event"], r["source"], r["origin_key"], r.get("chunk_no"))))
        await cur.execute("COMMIT")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"atlas: events 기록 실패(무시): {e}")


# ---------------------------------------------------------------------------
# add_atlas
# ---------------------------------------------------------------------------
def _embed_text(source: str, document: str, meta: Dict[str, Any]) -> str:
    """파일류는 「문서 제목 › 절 제목」 머리글을 붙여 임베딩한다(저장되는 document 에는 붙이지 않는다)."""
    if source == "transcript":
        return document
    title = meta.get("doc_title")
    section = meta.get("section_title")
    if title and section:
        return f"{title} › {section}\n{document}"
    if title:
        return f"{title}\n{document}"
    return document


async def do_add(server, emb, source: str, origin_key: str, chunks: List[Dict[str, Any]],
                    only_if_newer: bool = False, instance_name: Optional[str] = None) -> Dict[str, Any]:
    _check_source(source)
    _check_key(origin_key)
    instance_name = instance_name or ATLAS_INSTANCE
    if not isinstance(chunks, list) or not chunks:
        raise ValueError("chunks must be a non-empty list")
    if len(chunks) > MAX_CHUNKS_PER_CALL:
        raise ValueError(f"at most {MAX_CHUNKS_PER_CALL} chunks per call")
    pool = await server._get_pool(instance_name)
    results: List[Dict[str, Any]] = []
    max_chunk_count: Optional[int] = None
    async with pool.acquire() as conn:
        async with conn.cursor(cursor=asyncmy.cursors.DictCursor) as cur:
            for ch in sorted(chunks, key=lambda c: int(c.get("chunk_no", 0))):
                t0 = time.time()
                res: Dict[str, Any] = {"chunk_no": int(ch.get("chunk_no", 0))}
                try:
                    res.update(await _add_one(cur, conn, emb, source, origin_key, ch, only_if_newer))
                    cc = ch.get("chunk_count")
                    if isinstance(cc, int) and cc > 0:
                        max_chunk_count = cc
                except Exception as e:  # noqa: BLE001
                    try:
                        await conn.rollback()
                    except Exception:  # noqa: BLE001
                        pass
                    logger.error(f"atlas add_atlas 조각 실패 {source}:{origin_key}#{res['chunk_no']}: {e}", exc_info=True)
                    res.update({"status": "error", "error": str(e)[:300]})
                res["ms"] = int((time.time() - t0) * 1000)
                results.append(res)
            # 파일이 줄어 조각 수가 줄었으면 넘치는 옛 조각을 지운다. 건너뛴 조각이 오래된 판일 수 있으므로 모든 조각이 반영됐을 때만
            if max_chunk_count is not None and all(r["status"] in ("inserted", "replaced", "skipped_exists") for r in results):
                try:
                    await conn.begin()
                    await cur.execute(f"DELETE FROM {Q_IDENTS} WHERE source=%s AND origin_key=%s AND chunk_no>=%s", (source, origin_key, max_chunk_count))
                    await cur.execute(f"DELETE FROM {Q_CHUNKS} WHERE source=%s AND origin_key=%s AND chunk_no>=%s", (source, origin_key, max_chunk_count))
                    await conn.commit()
                except Exception as e:  # noqa: BLE001
                    await conn.rollback()
                    logger.warning(f"atlas: 넘치는 조각 정리 실패(무시): {e}")
    counts: Dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"source": source, "origin_key": origin_key, "counts": counts, "results": results,
            "ident_version": IDENT_VERSION, "ident_rules_sha": IDENT_RULES_SHA}


async def _add_one(cur, conn, emb, source: str, origin_key: str, ch: Dict[str, Any], only_if_newer: bool) -> Dict[str, Any]:
    document = ch.get("document")
    if not isinstance(document, str) or not document.strip():
        raise ValueError("document must be a non-empty string")
    if len(document) > MAX_DOC_CHARS:
        raise ValueError(f"document too long: {len(document)} > {MAX_DOC_CHARS}")
    chunk_no = int(ch.get("chunk_no", 0))
    meta = dict(ch.get("metadata") or {})
    for need in ("machine", "item_at"):
        if meta.get(need) in (None, ""):
            raise ValueError(f"metadata.{need} is required")
    meta["item_at"] = int(meta["item_at"])
    chash = _hash16(document)
    meta.update({"source": source, "origin_key": origin_key, "chunk_no": chunk_no,
                 "chunk_count": int(ch.get("chunk_count") or meta.get("chunk_count") or 1),
                 "content_hash": chash, "ident_version": IDENT_VERSION})
    meta.setdefault("embed_model", EMBED_MODEL_TAG)
    meta.setdefault("chunker_version", "1")

    # 1) 이미 있는가 — 트랜잭션 안에서 읽어 MaxScale 이 마스터로 보내게 한다(방금 넣은 행을 못 보는 일 방지)
    await conn.begin()
    try:
        await cur.execute(
            f"SELECT item_at, content_hash, JSON_VALUE(metadata,'$.chunker_version') AS cv, JSON_VALUE(metadata,'$.ident_version') AS iv, "
            f"JSON_VALUE(metadata,'$.file_hash') AS fh FROM {Q_CHUNKS} WHERE source=%s AND origin_key=%s AND chunk_no=%s",
            (source, origin_key, chunk_no))
        row = await cur.fetchone()
    finally:
        await conn.rollback()
    replacing = False
    if row:
        if only_if_newer and int(row["item_at"]) >= meta["item_at"]:
            return {"status": "skipped_older"}
        same = (row["content_hash"] == chash and str(row["cv"]) == str(meta["chunker_version"])
                and str(row["iv"]) == IDENT_VERSION and (row["fh"] or "") == str(meta.get("file_hash") or ""))
        if same:
            return {"status": "skipped_exists"}
        replacing = True

    # 2) 임베딩(트랜잭션 밖에서)
    vec = await emb.embed(_embed_text(source, document, meta))
    if len(vec) != 1024:
        raise RuntimeError(f"unexpected embedding dimension: {len(vec)}")

    # 3) 식별자 — 같은 조각 본문과 앞뒤 문맥(조각 경계에 걸린 긴 경로 대비)에서 뽑는다
    ident_src = f"{(ch.get('before') or '')[-_MAX_TOKEN:]}{document}{(ch.get('after') or '')[:_MAX_TOKEN]}"
    idents = extract_idents(ident_src)

    # 4) 저장
    await conn.begin()
    try:
        if replacing:
            await cur.execute(f"DELETE FROM {Q_IDENTS} WHERE source=%s AND origin_key=%s AND chunk_no=%s", (source, origin_key, chunk_no))
            await cur.execute(f"DELETE FROM {Q_CHUNKS} WHERE source=%s AND origin_key=%s AND chunk_no=%s", (source, origin_key, chunk_no))
        await cur.execute(f"INSERT INTO {Q_CHUNKS} (document, embedding, metadata) VALUES (%s, VEC_FromText(%s), %s)",
                          (document, _vec_text(vec), json.dumps(meta, ensure_ascii=False)))
        for tok, rtype in idents:
            await cur.execute(f"INSERT IGNORE INTO {Q_IDENTS} (token, ident_type, source, origin_key, chunk_no) VALUES (%s,%s,%s,%s,%s)",
                              (tok, rtype, source, origin_key, chunk_no))
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    return {"status": "replaced" if replacing else "inserted", "idents": len(idents)}


# ---------------------------------------------------------------------------
# get_atlas
# ---------------------------------------------------------------------------
async def do_get(server, source: str, origin_key: str, chunk_no: Optional[int] = None, record_open: bool = True,
                    session_id: str = "", machine: str = "unknown", project: Optional[str] = None,
                    session_name: Optional[str] = None, instance_name: Optional[str] = None) -> Dict[str, Any]:
    _check_source(source)
    _check_key(origin_key)
    pool = await server._get_pool(instance_name or ATLAS_INSTANCE)
    async with pool.acquire() as conn:
        async with conn.cursor(cursor=asyncmy.cursors.DictCursor) as cur:
            sql = f"SELECT chunk_no, document, metadata FROM {Q_CHUNKS} WHERE source=%s AND origin_key=%s"
            params: List[Any] = [source, origin_key]
            if chunk_no is not None:
                sql += " AND chunk_no=%s"
                params.append(int(chunk_no))
            sql += " ORDER BY chunk_no"
            await cur.execute(sql, tuple(params))
            rows = await cur.fetchall()
            chunks = []
            for r in rows:
                m = r["metadata"]
                if isinstance(m, str):
                    try:
                        m = json.loads(m)
                    except Exception:  # noqa: BLE001
                        pass
                chunks.append({"chunk_no": r["chunk_no"], "document": r["document"], "metadata": m})
            if record_open and chunks:
                await _log_events(cur, [{"event": "opened", "source": source, "origin_key": origin_key,
                                         "chunk_no": None if chunk_no is None else int(chunk_no),
                                         "machine": machine, "project": project, "session_id": session_id, "session_name": session_name}])
    return {"source": source, "origin_key": origin_key, "found": len(chunks), "chunks": chunks}


# ---------------------------------------------------------------------------
# search_atlas
# ---------------------------------------------------------------------------
_CAND_COLS = ("source, origin_key, chunk_no, machine, project, session_name, title, item_at, turn_at_source, rule_score, "
              "content_hash, JSON_VALUE(metadata,'$.doc_title') AS doc_title, JSON_VALUE(metadata,'$.section_title') AS section_title, "
              "LEFT(document, 200) AS snippet, session_id, CAST(JSON_VALUE(metadata,'$.line_start') AS UNSIGNED) AS line_start")


def _filters(before: Optional[int], exclude_session_id: Optional[str], exclude_after_line: Optional[int]) -> Tuple[str, List[Any]]:
    sql, params = "", []
    if before is not None:
        sql += " AND item_at < %s"
        params.append(int(before))
    if exclude_session_id:
        if exclude_after_line is not None:
            sql += " AND (session_id IS NULL OR session_id <> %s OR CAST(JSON_VALUE(metadata,'$.line_start') AS UNSIGNED) < %s)"
            params += [exclude_session_id, int(exclude_after_line)]
        else:
            sql += " AND (session_id IS NULL OR session_id <> %s)"
            params.append(exclude_session_id)
    return sql, params


def _norm(r: Dict[str, Any]) -> Dict[str, Any]:
    return {"source": r["source"], "origin_key": r["origin_key"], "chunk_no": int(r["chunk_no"]), "machine": r["machine"],
            "project": r["project"], "session_name": r["session_name"], "title": r["title"],
            "doc_title": r.get("doc_title"), "section_title": r.get("section_title"),
            "item_at": None if r["item_at"] is None else int(r["item_at"]), "turn_at_source": r["turn_at_source"],
            "rule_score": None if r["rule_score"] is None else int(r["rule_score"]), "content_hash": r["content_hash"],
            "distance": float(r["distance"]), "snippet": r["snippet"]}


async def do_search(server, emb, query: str, sources: Optional[List[str]] = None, per_source_n: int = 15,
                       return_n: int = 10, top_n: int = 3, before: Optional[int] = None,
                       exclude_session_id: Optional[str] = None, exclude_after_line: Optional[int] = None,
                       machine: Optional[str] = None, project: Optional[str] = None, session_id: Optional[str] = None,
                       session_name: Optional[str] = None, log_shown: bool = True,
                       instance_name: Optional[str] = None, rank: str = "distance",
                       max_distance: Optional[float] = None) -> Dict[str, Any]:
    if not query or not isinstance(query, str):
        raise ValueError("query must be a non-empty string")
    if rank not in ("distance", "c4"):
        raise ValueError("rank must be 'distance' or 'c4'")
    srcs = list(sources) if sources else list(SOURCES)
    for s in srcs:
        _check_source(s)
    per_source_n = max(1, min(int(per_source_n), 100))
    return_n = max(1, min(int(return_n), 50))
    top_n = max(0, min(int(top_n), return_n))
    t0 = time.time()
    qvec = await emb.embed(QUERY_PREFIX + query)
    embed_ms = int((time.time() - t0) * 1000)
    qtext = _vec_text(qvec)
    fsql, fparams = _filters(before, exclude_session_id, exclude_after_line)
    pool = await server._get_pool(instance_name or ATLAS_INSTANCE)
    cands: Dict[Tuple[str, str, int], Dict[str, Any]] = {}
    per_src_count: Dict[str, int] = {}
    idents = extract_idents(query)
    ident_w: Dict[Tuple[str, str, int], float] = {}
    # 1) 종류별 벡터 후보 — 종류마다 연결을 따로 써서 동시에 묻는다. 필터는 후보를 뽑은 뒤 걸리므로 넉넉히 가져온다.
    def keep_row(r) -> bool:
        """인덱스 경로의 시각·세션 필터. SQL WHERE 는 인덱스가 뽑은 상위 후보에만 걸려 후보를 잃으므로 여기서 건다."""
        if before is not None and (r["item_at"] is None or int(r["item_at"]) >= int(before)):
            return False
        if exclude_session_id and r["session_id"] == exclude_session_id:
            if exclude_after_line is None:
                return False
            return r["line_start"] is not None and int(r["line_start"]) < int(exclude_after_line)
        return True

    async def vector_candidates(s: str):
        want = per_source_n * (4 if s == "machine_doc" else 3)
        use_index = s in INDEX_SOURCES
        dist = "VEC_DISTANCE_COSINE(embedding, VEC_FromText(%s))"
        async with pool.acquire() as c2:
            async with c2.cursor(cursor=asyncmy.cursors.DictCursor) as cu:
                if not use_index:   # 행이 적은 종류: 정확 계산 + SQL 필터
                    sql = (f"SELECT {_CAND_COLS}, {dist} AS distance FROM {Q_CHUNKS} "
                           f"WHERE source=%s{fsql} ORDER BY {dist} + 0 LIMIT %s")
                    await cu.execute(sql, tuple([qtext, s] + fparams + [qtext, want]))
                    return s, await cu.fetchall()
                depth = max(INDEX_DEPTH, want)
                ef = max(EF_SEARCH, depth)   # 인덱스는 ef_search 개까지만 돌려준다
                sql = f"SELECT {_CAND_COLS}, {dist} AS distance FROM {Q_CHUNKS} WHERE source=%s ORDER BY {dist} LIMIT %s"
                await cu.execute(f"SET SESSION mhnsw_ef_search={int(ef)}")
                try:
                    await cu.execute(sql, (qtext, s, qtext, depth))
                    rows = [r for r in await cu.fetchall() if keep_row(r)]
                    return s, rows[:want]
                finally:
                    await cu.execute("SET SESSION mhnsw_ef_search=DEFAULT")

    mem_rows = None
    if MEM is not None:
        MEM.ensure_started()
        if MEM.ready:
            wants = {s: per_source_n * (4 if s == "machine_doc" else 3) for s in srcs}
            mem_rows = MEM.search(qvec, wants, before, exclude_session_id, exclude_after_line)
    vec_results = list(mem_rows.items()) if mem_rows is not None else await asyncio.gather(*[vector_candidates(s) for s in srcs])
    for s_name, rows in vec_results:
        n = 0
        for r in rows:
            c = dict(r) if mem_rows is not None else _norm(r)
            c["cand_from"] = "vector"
            cands[(c["source"], c["origin_key"], c["chunk_no"])] = c
            n += 1
        per_src_count[s_name] = n
    async with pool.acquire() as conn:
        async with conn.cursor(cursor=asyncmy.cursors.DictCursor) as cur:
            # 2) 식별자 정확 일치 후보
            for tok, _t in idents[:IDENT_MAX_TOKENS]:
                sql = (f"SELECT c.source AS source, c.origin_key AS origin_key, c.chunk_no AS chunk_no, c.machine AS machine, "
                       f"c.project AS project, c.session_name AS session_name, c.title AS title, c.item_at AS item_at, "
                       f"c.turn_at_source AS turn_at_source, c.rule_score AS rule_score, c.content_hash AS content_hash, "
                       f"JSON_VALUE(c.metadata,'$.doc_title') AS doc_title, JSON_VALUE(c.metadata,'$.section_title') AS section_title, "
                       f"LEFT(c.document, 200) AS snippet, VEC_DISTANCE_COSINE(c.embedding, VEC_FromText(%s)) AS distance "
                       f"FROM {Q_IDENTS} i JOIN {Q_CHUNKS} c ON c.source=i.source AND c.origin_key=i.origin_key AND c.chunk_no=i.chunk_no "
                       f"WHERE i.token=%s AND c.source IN ({','.join(['%s'] * len(srcs))})"
                       + fsql.replace(" AND item_at", " AND c.item_at").replace("session_id", "c.session_id").replace("JSON_VALUE(metadata", "JSON_VALUE(c.metadata")
                       + f" ORDER BY c.item_at DESC LIMIT {IDENT_PER_TOKEN}")
                await cur.execute(sql, tuple([qtext, tok] + srcs + fparams))
                ident_rows = await cur.fetchall()
                w = 1.0 / max(1, len(ident_rows))     # 맞춘 조각이 적은(희귀한) 토큰일수록 가중이 크다
                for r in ident_rows:
                    key = (r["source"], r["origin_key"], int(r["chunk_no"]))
                    ident_w[key] = ident_w.get(key, 0.0) + w
                    if key in cands:
                        cands[key]["cand_from"] = "both"
                    else:
                        c = _norm(r)
                        c["cand_from"] = "ident"
                        cands[key] = c
            # 3) 머신 문서는 머신별 사본이 같은 내용이면 하나로 접는다(같은 머신 것 우선)
            folded: Dict[Tuple[str, str], Dict[str, Any]] = {}
            keep: List[Dict[str, Any]] = []
            for c in cands.values():
                if c["source"] == "machine_doc" and c["content_hash"]:
                    k = (c["source"], c["content_hash"])
                    cur_best = folded.get(k)
                    better = (cur_best is None or (c["machine"] == machine and cur_best["machine"] != machine)
                              or (c["distance"] < cur_best["distance"] and (cur_best["machine"] != machine or c["machine"] == machine)))
                    if better:
                        if cur_best is not None and cur_best["cand_from"] != c["cand_from"]:
                            c["cand_from"] = "both"
                        folded[k] = c
                else:
                    keep.append(c)
            keep.extend(folded.values())
            # 4) 정렬: 식별자 일치 조각 먼저, 그다음 순위식(distance: 1 − 거리, c4: 위 RANK_C4_*)
            ref_ms = int(before) if before is not None else int(time.time() * 1000)
            for c in keep:
                sc = 1.0 - c["distance"]
                if rank == "c4" and c["source"] == "transcript" and c.get("turn_at_source") != "burst":
                    age_d = max(0.0, (ref_ms - int(c["item_at"])) / _DAY_MS) if c.get("item_at") is not None else 0.0
                    sc += RANK_C4_GAMMA * math.exp(-RANK_C4_DELTA * age_d)
                c["score"] = round(sc, 6)
            keep.sort(key=lambda c: (0 if c["cand_from"] in ("ident", "both") else 1,
                                     -ident_w.get((c["source"], c["origin_key"], c["chunk_no"]), 0.0), -c["score"], c["distance"]))
            out = keep[:return_n]
            for i, c in enumerate(out):
                c["rank"] = i + 1
                # 보여 줄 것은 상위 top_n 을 자른 뒤 거리 상한으로 거른다(식별자 일치는 상한을 건너뛴다) — 시험과 같은 순서
                c["shown"] = i < top_n and (max_distance is None or c["cand_from"] in ("ident", "both")
                                            or c["distance"] <= float(max_distance))
            # 4-2) 메모리 경로의 후보는 본문 앞부분(snippet)을 상위 후보만 DB 에서 읽어 채운다
            need = [c for c in out if c.get("snippet") is None and c.get("_id")]
            if need:
                ph = ",".join(["%s"] * len(need))
                await cur.execute(f"SELECT id, LEFT(document, 200) AS snippet FROM {Q_CHUNKS} WHERE id IN ({ph})", tuple(c["_id"] for c in need))
                snip = {r["id"]: r["snippet"] for r in await cur.fetchall()}
                for c in need:
                    c["snippet"] = snip.get(c["_id"], "")
            for c in out:
                c.pop("_id", None)
            # 5) 보여 준 조각을 events 에 기록
            if log_shown and top_n:
                qsha = hashlib.sha1(query.encode("utf-8")).hexdigest()
                await _log_events(cur, [{"event": "shown", "source": c["source"], "origin_key": c["origin_key"], "chunk_no": c["chunk_no"],
                                         "machine": machine, "project": project, "session_id": session_id, "session_name": session_name,
                                         "prompt_sha1": qsha, "distance": c["distance"], "rank_no": c["rank"]} for c in out if c["shown"]])
    return {"results": out, "meta": {"total_ms": int((time.time() - t0) * 1000), "embed_ms": embed_ms,
                                      "per_source_fetched": per_src_count, "idents": [t for t, _ in idents],
                                      "exact_memory": mem_rows is not None, "mem_rows": (MEM.n if MEM is not None else 0),
                                      "ident_rules_sha": IDENT_RULES_SHA}}


# ---------------------------------------------------------------------------
# MCP 등록 — server.register_tools() 에서 한 줄로 부른다
# ---------------------------------------------------------------------------
def register_atlas_tools(server, embedding_service) -> None:
    global MEM
    mcp = server.mcp
    if EXACT_MEMORY:
        MEM = MemoryIndex(server, Q_CHUNKS, ATLAS_INSTANCE)
        MEM.ensure_started()   # 이벤트 루프가 돌고 있으면 바로 적재 시작, 아니면 첫 검색 때 시작
        logger.info(f"atlas: 메모리 정확 계산 켬 ({Q_CHUNKS})")

    @mcp.tool
    async def search_atlas(query: str, sources: Optional[List[str]] = None, per_source_n: int = 15, return_n: int = 10,
                                top_n: int = 3, before: Optional[int] = None, exclude_session_id: Optional[str] = None,
                                exclude_after_line: Optional[int] = None, machine: Optional[str] = None,
                                project: Optional[str] = None, session_id: Optional[str] = None,
                                session_name: Optional[str] = None, log_shown: bool = True,
                                instance_name: Optional[str] = None, rank: str = "distance",
                                max_distance: Optional[float] = None) -> Dict[str, Any]:
        """Search the memory atlas (transcripts, auto-memory, workspace memory, machine docs) by meaning plus exact identifiers (commit hash, path, board no). rank='c4' adds recency for transcripts; max_distance limits which of the top_n are marked shown."""
        return await do_search(server, embedding_service, query, sources, per_source_n, return_n, top_n, before,
                                  exclude_session_id, exclude_after_line, machine, project, session_id, session_name,
                                  log_shown, instance_name, rank, max_distance)

    @mcp.tool
    async def get_atlas(source: str, origin_key: str, chunk_no: Optional[int] = None, record_open: bool = True,
                             session_id: str = "", machine: str = "unknown", project: Optional[str] = None,
                             session_name: Optional[str] = None, instance_name: Optional[str] = None) -> Dict[str, Any]:
        """Open the full chunks of one atlas item by source and origin_key (as listed by search_atlas)."""
        return await do_get(server, source, origin_key, chunk_no, record_open, session_id, machine, project, session_name, instance_name)

    @mcp.tool
    async def add_atlas(source: str, origin_key: str, chunks: List[Dict[str, Any]], only_if_newer: bool = False,
                             instance_name: Optional[str] = None) -> Dict[str, Any]:
        """Add or replace atlas chunks (pipeline only). Skips unchanged chunks before embedding and records identifiers."""
        return await do_add(server, embedding_service, source, origin_key, chunks, only_if_newer, instance_name)

    logger.info("Registered atlas tools: search_atlas, get_atlas, add_atlas")
