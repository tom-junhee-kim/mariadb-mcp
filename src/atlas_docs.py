# atlas_docs.py — 메모리 아틀라스 10단계: 규칙·CLAUDE.md 절·머신 문서·워크스페이스 메모리의 정본(docs)과 그 둘레
#
# 표(sql/atlas_docs.sql): docs · docs_history · doc_proposals · rule_events · tuning · ws_cutover · standing_approvals
# 세션 도구 : list_docs · get_doc · put_doc · move_doc · diff_rules · rule_stats · add_standing_approval · set_cutover · decide_proposal
# 파이프라인 도구(세션에서는 managed deny 로 숨김): import_docs · add_rule_events · set_rule_reactions · set_tuning · open_proposal
# HTTP 경로: GET /atlas/version · /atlas/rules · /atlas/docs · /atlas/owned(업로더용) · POST /atlas/search
#
# 규칙(rule)·CLAUDE.md 절(guide)은 언제나 후보(doc_proposals)를 거쳐 사람 승인 뒤에만 바뀐다. 사람 턴 대조는 클라이언트(gate)가 하고,
# 서버는 판 충돌(base_version)·시험 해시(test_sha)·상시 승인 범위를 기계로 확인한다.
import asyncio
import hashlib
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import asyncmy

from config import logger
import atlas
import doc_chunker
from atlas import ATLAS_DB, ATLAS_INSTANCE, _SUFFIX

KINDS = ("rule", "guide", "machine_doc", "workspace_memory")
APPROVAL_KINDS = ("rule", "guide")          # 늘 후보로만 바뀌는 종류
CHUNK_KINDS = ("rule", "guide")             # 언제나 이 모듈이 검색 조각을 만드는 종류
FILE_KINDS = ("machine_doc", "workspace_memory")   # 파일 업로더가 조각을 만들다가, 전환 표지(ws_cutover)가 db 가 되면 이 모듈이 만든다
MACHINE_DOC_CUT = "@machine_doc"            # ws_cutover 에서 머신 문서 전환을 가리키는 가짜 워크스페이스 이름(machine 칸: 호스트 또는 'global')
MACHINES = ("isD-MacBookPro", "JYGFRESH-JHCHEONG", "ISD-TUF-GAMING", "CS-15S-EQ3")   # 옛 머신 문서 조각 키의 접두사
CHUNK_MAX = 800
CHUNKER_VERSION = "d1"
SYNC_SEC = float(os.getenv("ATLAS_DOCS_SYNC_SEC", "300"))
STATE_SEC = float(os.getenv("ATLAS_DOCS_STATE_SEC", "30"))
PIPELINE_TOKEN = os.getenv("ATLAS_PIPELINE_TOKEN", "")   # 비어 있으면 파이프라인 도구의 토큰을 확인하지 않는다(M-69 결정 전)


def _q(name: str) -> str:
    return f"`{ATLAS_DB}`.`{name}{_SUFFIX}`"


Q_DOCS, Q_HIST, Q_PROP = _q("docs"), _q("docs_history"), _q("doc_proposals")
Q_RULEEV, Q_TUNING, Q_CUT, Q_STAND = _q("rule_events"), _q("tuning"), _q("ws_cutover"), _q("standing_approvals")
DDL_PATH = Path(__file__).resolve().parent.parent / "sql" / "atlas_docs.sql"


def ddl(suffix: str) -> str:
    return DDL_PATH.read_text(encoding="utf-8").replace("{S}", suffix)


def _machine_addrs() -> Dict[str, str]:
    """ATLAS_MACHINE_ADDRS="192.168.2.2=isD-MacBookPro,192.168.2.3=CS-15S-EQ3" — 접속 주소로 쓰는 머신을 정한다(머신 구분용)."""
    out = {}
    for part in filter(None, os.getenv("ATLAS_MACHINE_ADDRS", "").split(",")):
        ip, _, name = part.partition("=")
        if ip and name:
            out[ip.strip()] = name.strip()
    return out


MACHINE_ADDRS = _machine_addrs()


def caller_machine() -> str:
    try:
        from fastmcp.server.dependencies import get_http_request
        ip = get_http_request().client.host
    except Exception:  # noqa: BLE001
        return "unknown"
    return MACHINE_ADDRS.get(ip, f"addr:{ip}")


def doc_id_of(kind: str, scope: str, name: str) -> str:
    return f"{kind}:{scope}:{name}"


def _check_scope(scope: str) -> None:
    if scope == "global":
        return
    head, sep, rest = scope.partition(":")
    if not (sep and head in ("ws", "machine") and rest and len(scope) <= 160):
        raise ValueError(f"scope must be 'global', 'ws:<workspace>' or 'machine:<host>': {scope!r}")


def _check_kind(kind: str) -> None:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}: {kind!r}")


def _check_name(name: str) -> None:
    if not name or not isinstance(name, str) or len(name) > 200:
        raise ValueError("name must be a non-empty string of at most 200 characters")


def content_sha(header: Dict[str, Any], body: str) -> str:
    """후보 내용의 해시 — 시험을 돌린 판과 승인하는 판이 같은지 가른다."""
    raw = json.dumps(header or {}, ensure_ascii=False, sort_keys=True) + "\n" + (body or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _json(v: Any) -> Any:
    if isinstance(v, (bytes, bytearray)):
        v = v.decode("utf-8")
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def _check_token(token: Optional[str]) -> None:
    if PIPELINE_TOKEN and token != PIPELINE_TOKEN:
        raise PermissionError("Operation forbidden: pipeline tool requires the pipeline token.")


# ---------------------------------------------------------------------------
# 연결 — 읽기도 트랜잭션 안에서 해 MaxScale 이 master 로 보내게 한다(방금 쓴 판을 slave 에서 못 보는 일 방지)
# ---------------------------------------------------------------------------
@asynccontextmanager
async def _tx(server, instance_name: Optional[str] = None):
    pool = await server._get_pool(instance_name or ATLAS_INSTANCE)
    async with pool.acquire() as conn:
        async with conn.cursor(cursor=asyncmy.cursors.DictCursor) as cur:
            await conn.begin()
            try:
                yield cur
            except BaseException:
                await conn.rollback()
                raise
            else:
                await conn.commit()


async def _bump(cur, key: str) -> int:
    await cur.execute(f"UPDATE {Q_TUNING} SET v = v + 1, updated_at = UTC_TIMESTAMP(3) WHERE k = %s", (key,))
    await cur.execute(f"SELECT v FROM {Q_TUNING} WHERE k = %s", (key,))
    row = await cur.fetchone()
    return int(row["v"]) if row else 0


async def _write_doc(cur, kind: str, scope: str, name: str, header: Dict[str, Any], body: str, status: str,
                     by: str, reason: Optional[str], proposal_id: Optional[int]) -> int:
    """docs 의 새 판을 쓰고 이력을 남긴다. 반환: 새 판 번호."""
    did = doc_id_of(kind, scope, name)
    await cur.execute(f"SELECT version FROM {Q_DOCS} WHERE doc_id = %s FOR UPDATE", (did,))
    row = await cur.fetchone()
    ver = (int(row["version"]) if row else 0) + 1
    hjson = json.dumps(header or {}, ensure_ascii=False)
    sha = hashlib.sha256((body or "").encode("utf-8")).hexdigest()[:16]
    if row:
        await cur.execute(f"UPDATE {Q_DOCS} SET header=%s, body=%s, body_sha=%s, status=%s, version=%s, updated_at=UTC_TIMESTAMP(3), "
                          f"updated_by=%s WHERE doc_id=%s", (hjson, body, sha, status, ver, by, did))
    else:
        await cur.execute(f"INSERT INTO {Q_DOCS} (doc_id, kind, scope, name, status, header, body, body_sha, version, chunk_version, "
                          f"updated_at, updated_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,0,UTC_TIMESTAMP(3),%s)",
                          (did, kind, scope, name, status, hjson, body, sha, ver, by))
    await cur.execute(f"INSERT INTO {Q_HIST} (doc_id, version, header, body, status, updated_at, updated_by, reason, proposal_id) "
                      f"VALUES (%s,%s,%s,%s,%s,UTC_TIMESTAMP(3),%s,%s,%s)", (did, ver, hjson, body, status, by, reason, proposal_id))
    return ver


# ---------------------------------------------------------------------------
# 서버 메모리의 판 번호·조정값 — /atlas/version 이 DB 를 읽지 않게
# ---------------------------------------------------------------------------
class DocsState:
    def __init__(self, server):
        self.server = server
        self.rules_version = None
        self.tuning_version = None
        self.tuning: Dict[str, float] = {}
        self.loaded_at = 0.0
        self.error: Optional[str] = None
        self._task = None

    async def load(self) -> None:
        async with _tx(self.server) as cur:
            await cur.execute(f"SELECT k, v FROM {Q_TUNING}")
            rows = await cur.fetchall()
        vals = {r["k"]: float(r["v"]) for r in rows}
        self.rules_version = int(vals.get("rules_version", 0))
        self.tuning_version = int(vals.get("tuning_version", 0))
        self.tuning = {k: v for k, v in vals.items() if k not in ("rules_version", "tuning_version")}
        self.loaded_at = time.time()
        self.error = None

    async def run(self) -> None:
        last_sync = 0.0
        while True:
            try:
                await self.load()
            except Exception as e:  # noqa: BLE001
                self.error = str(e)[:200]
                logger.warning(f"atlas docs: 상태 읽기 실패(다음 주기에 다시): {e}")
            if time.time() - last_sync >= SYNC_SEC:
                try:
                    await sync_chunks(self.server, EMB)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"atlas docs: 조각 맞추기 실패(다음 주기에 다시): {e}")
                last_sync = time.time()
            await asyncio.sleep(STATE_SEC)

    def ensure_started(self) -> None:
        if self._task is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self.run())


STATE: Optional[DocsState] = None
EMB = None


def _state_touch(rules: Optional[int] = None, tuning_v: Optional[int] = None, tuning: Optional[Tuple[str, float]] = None) -> None:
    if STATE is None:
        return
    if rules is not None:
        STATE.rules_version = rules
    if tuning_v is not None:
        STATE.tuning_version = tuning_v
    if tuning is not None:
        STATE.tuning[tuning[0]] = tuning[1]


# ---------------------------------------------------------------------------
# 검색 조각 — rule·guide 의 본문을 chunks 에 맞춘다(chunk_version 이 version 과 다른 문서만)
# ---------------------------------------------------------------------------
def split_body(body: str, limit: int = CHUNK_MAX) -> List[str]:
    """문단 경계에서 limit 이하로 나눈다. 한 문단이 limit 을 넘으면 그 문단만 글자 수로 자른다."""
    parts: List[str] = []
    cur = ""
    for para in body.split("\n\n"):
        para = para.strip("\n")
        if not para:
            continue
        while len(para) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(para[:limit])
            para = para[limit:]
        if cur and len(cur) + 2 + len(para) > limit:
            parts.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        parts.append(cur)
    return parts or [body[:limit] or "(빈 문서)"]


async def owned_file_scopes(cur) -> Dict[str, List[str]]:
    """전환 표지가 db 인 파일 종류 문서의 범위 — 이 범위의 검색 조각은 서버가 만든다(파일 업로더는 이 범위를 멈춰야 한다).
    워크스페이스 메모리: 그 워크스페이스의 모든 머신 행이 db 일 때만. 머신 문서: ('@machine_doc', 호스트|'global') 행이 db 인 범위."""
    await cur.execute(f"SELECT workspace, machine, mode FROM {Q_CUT}")
    by_ws: Dict[str, List[Tuple[str, str]]] = {}
    for r in await cur.fetchall():
        by_ws.setdefault(r["workspace"], []).append((r["machine"], r["mode"]))
    out: Dict[str, List[str]] = {"machine_doc": [], "workspace_memory": []}
    for ws, items in by_ws.items():
        if ws == MACHINE_DOC_CUT:
            out["machine_doc"] += ["global" if m == "global" else f"machine:{m}" for m, mode in items if mode == "db"]
        elif items and all(mode == "db" for _, mode in items):
            out["workspace_memory"].append(f"ws:{ws}")
    return out


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _purge_keys(server, source: str, prefix: str, keep: Tuple[str, ...] = ()) -> int:
    """`prefix` 또는 `prefix#…` 키의 조각·식별자 중 keep 에 없는 것을 지운다(절이 줄었을 때·옛 사본·퇴역)."""
    async with _tx(server) as cur:
        await cur.execute(f"SELECT DISTINCT origin_key FROM {atlas.Q_CHUNKS} WHERE source=%s AND (origin_key=%s OR origin_key LIKE %s)",
                          (source, prefix, _like_escape(prefix) + "#%"))
        stale = [r["origin_key"] for r in await cur.fetchall() if r["origin_key"] not in keep]
        for k in stale:
            await cur.execute(f"DELETE FROM {atlas.Q_IDENTS} WHERE source=%s AND origin_key=%s", (source, k))
            await cur.execute(f"DELETE FROM {atlas.Q_CHUNKS} WHERE source=%s AND origin_key=%s", (source, k))
    return len(stale)


def file_doc_groups(r: Dict[str, Any]) -> Tuple[str, List[Tuple[str, List[dict]]]]:
    """머신 문서·워크스페이스 메모리 한 판을 [(origin_key, 조각)] 으로. 키는 옛 파일 업로더의 키(header.legacy_origin_key)를 그대로 쓴다 —
    shown·opened 이력과 식별자가 끊기지 않게. 새 문서는 doc_id 가 키다."""
    header = _json(r["header"]) if not isinstance(r["header"], dict) else r["header"]
    header = header if isinstance(header, dict) else {}
    lk = header.get("legacy_origin_key") or ""
    prefix = lk or r["doc_id"]
    scope, kind = r["scope"], r["kind"]
    machine = "global" if scope == "global" else scope[len("machine:"):][:32] if scope.startswith("machine:") else scope[:32]
    project = "machine" if kind == "machine_doc" else (lk.split("/")[0] if "/" in lk else scope[len("ws:"):])
    base = {"machine": machine, "project": project, "item_at": int(r["at_ms"]), "doc_id": r["doc_id"], "doc_version": int(r["version"]),
            "doc_title": r["name"]}
    return prefix, doc_chunker.doc_groups(kind, prefix, r["body"] or "", base)


async def _sync_file_doc(server, emb, r: Dict[str, Any]) -> None:
    kind = r["kind"]
    if r["status"] == "retired":
        prefix = (_json(r["header"]) or {}).get("legacy_origin_key") or r["doc_id"]
        await _purge_keys(server, kind, prefix)
        return
    prefix, groups = file_doc_groups(r)
    for key, chunks in groups:
        for k in range(0, len(chunks), atlas.MAX_CHUNKS_PER_CALL):
            res = await atlas.do_add(server, emb, kind, key, chunks[k:k + atlas.MAX_CHUNKS_PER_CALL])
            bad = [x for x in res["results"] if x["status"] == "error"]
            if bad:
                raise RuntimeError(f"{r['doc_id']}: {bad[0].get('error')}")
    await _purge_keys(server, kind, prefix, tuple(k for k, _ in groups))
    if kind == "machine_doc" and r["scope"] == "global" and ":" in prefix:
        own, _, fname = prefix.partition(":")
        for host in MACHINES:   # 전역 문서는 한 벌 — 머신마다 올리던 사본을 지운다
            if host != own:
                await _purge_keys(server, kind, f"{host}:{fname}")


async def sync_chunks(server, emb, limit: int = 50) -> Dict[str, int]:
    if emb is None:
        return {"synced": 0}
    async with _tx(server) as cur:
        owned = await owned_file_scopes(cur)
        conds, params = [f"kind IN ({','.join(['%s'] * len(CHUNK_KINDS))})"], list(CHUNK_KINDS)
        for fk, scopes in owned.items():
            if scopes:
                conds.append(f"(kind=%s AND scope IN ({','.join(['%s'] * len(scopes))}))")
                params += [fk] + scopes
        await cur.execute(f"SELECT doc_id, kind, scope, name, status, header, body, version, "
                          f"TIMESTAMPDIFF(MICROSECOND, '1970-01-01 00:00:00', updated_at) DIV 1000 AS at_ms FROM {Q_DOCS} "
                          f"WHERE ({' OR '.join(conds)}) AND chunk_version <> version LIMIT %s", tuple(params) + (int(limit),))
        rows = await cur.fetchall()
    done = 0
    for r in rows:
        did, kind = r["doc_id"], r["kind"]
        if kind in FILE_KINDS:
            await _sync_file_doc(server, emb, r)
        elif r["status"] == "retired":
            async with _tx(server) as cur:
                await cur.execute(f"DELETE FROM {atlas.Q_IDENTS} WHERE source=%s AND origin_key=%s", (kind, did))
                await cur.execute(f"DELETE FROM {atlas.Q_CHUNKS} WHERE source=%s AND origin_key=%s", (kind, did))
        else:
            header = _json(r["header"]) or {}
            pieces = split_body(r["body"] or "")
            scope = r["scope"]
            meta = {"machine": scope[:32], "project": scope[3:] if scope.startswith("ws:") else None, "item_at": int(r["at_ms"]),
                    "title": (header.get("description") or r["name"])[:300] if isinstance(header, dict) else r["name"],
                    "doc_title": r["name"], "doc_id": did, "doc_version": int(r["version"]), "chunker_version": CHUNKER_VERSION}
            chunks = [{"chunk_no": i, "chunk_count": len(pieces), "document": p, "metadata": dict(meta)} for i, p in enumerate(pieces)]
            for k in range(0, len(chunks), atlas.MAX_CHUNKS_PER_CALL):
                res = await atlas.do_add(server, emb, kind, did, chunks[k:k + atlas.MAX_CHUNKS_PER_CALL])
                bad = [x for x in res["results"] if x["status"] == "error"]
                if bad:
                    raise RuntimeError(f"{did}: {bad[0].get('error')}")
        async with _tx(server) as cur:
            await cur.execute(f"UPDATE {Q_DOCS} SET chunk_version = %s WHERE doc_id = %s AND version = %s", (r["version"], did, r["version"]))
        done += 1
    if done:
        logger.info(f"atlas docs: 검색 조각 {done}개 문서 맞춤")
    return {"synced": done}


# ---------------------------------------------------------------------------
# 세션 도구
# ---------------------------------------------------------------------------
async def do_list_docs(server, kind: Optional[str] = None, scope: Optional[str] = None, status: str = "active",
                       query: Optional[str] = None, with_stats: bool = False, limit: int = 500) -> Dict[str, Any]:
    where, params = ["1=1"], []
    if kind:
        _check_kind(kind)
        where.append("kind = %s"); params.append(kind)
    if scope:
        _check_scope(scope)
        where.append("scope = %s"); params.append(scope)
    if status in ("active", "retired"):
        where.append("status = %s"); params.append(status)
    if query:
        where.append("(name LIKE %s OR body LIKE %s)"); params += [f"%{query}%", f"%{query}%"]
    async with _tx(server) as cur:
        await cur.execute(f"SELECT doc_id, kind, scope, name, status, version, chunk_version, updated_at, updated_by, "
                          f"JSON_VALUE(header,'$.description') AS description, JSON_VALUE(header,'$.event') AS event, "
                          f"JSON_VALUE(header,'$.action') AS action, CHAR_LENGTH(body) AS body_chars "
                          f"FROM {Q_DOCS} WHERE {' AND '.join(where)} ORDER BY kind, scope, name LIMIT %s",
                          tuple(params) + (max(1, min(int(limit), 2000)),))
        docs = await cur.fetchall()
        stats: Dict[str, Dict[str, Any]] = {}
        if with_stats and docs:
            await cur.execute(f"SELECT doc_id, SUM(at >= UTC_TIMESTAMP() - INTERVAL 7 DAY) AS n7, COUNT(*) AS n30, "
                              f"SUM(reaction = 'instant-rerun') AS instant, SUM(reaction = 'changed') AS changed, "
                              f"SUM(reaction IS NOT NULL) AS judged FROM {Q_RULEEV} "
                              f"WHERE at >= UTC_TIMESTAMP() - INTERVAL 30 DAY AND doc_id IS NOT NULL GROUP BY doc_id")
            for r in await cur.fetchall():
                j = int(r["judged"] or 0)
                stats[r["doc_id"]] = {"n7": int(r["n7"] or 0), "n30": int(r["n30"] or 0),
                                      "instant_rerun_rate": round(int(r["instant"] or 0) / j, 3) if j else None,
                                      "follow_rate": round(int(r["changed"] or 0) / j, 3) if j else None}
    out = []
    for d in docs:
        d = dict(d)
        d["updated_at"] = str(d["updated_at"])
        if with_stats:
            d["stats"] = stats.get(d["doc_id"], {"n7": 0, "n30": 0, "instant_rerun_rate": None, "follow_rate": None})
        out.append(d)
    return {"count": len(out), "docs": out}


async def do_get_doc(server, doc_id: str, version: Optional[int] = None, with_history: bool = False,
                     session_id: str = "", session_name: Optional[str] = None) -> Dict[str, Any]:
    async with _tx(server) as cur:
        if version is None:
            await cur.execute(f"SELECT doc_id, kind, scope, name, status, header, body, version, updated_at, updated_by FROM {Q_DOCS} WHERE doc_id=%s", (doc_id,))
        else:
            await cur.execute(f"SELECT h.doc_id, d.kind, d.scope, d.name, h.status, h.header, h.body, h.version, h.updated_at, h.updated_by "
                              f"FROM {Q_HIST} h JOIN {Q_DOCS} d ON d.doc_id=h.doc_id WHERE h.doc_id=%s AND h.version=%s", (doc_id, int(version)))
        row = await cur.fetchone()
        if not row:
            return {"doc_id": doc_id, "found": False}
        doc = dict(row)
        doc["header"] = _json(doc["header"])
        doc["updated_at"] = str(doc["updated_at"])
        await cur.execute(f"SELECT proposal_id, op, base_version, reason, proposed_by, proposed_at FROM {Q_PROP} "
                          f"WHERE doc_id=%s AND state='open' ORDER BY proposal_id", (doc_id,))
        doc["open_proposals"] = [{**dict(p), "id": f"P-{p['proposal_id']}", "proposed_at": str(p["proposed_at"])} for p in await cur.fetchall()]
        if with_history:
            await cur.execute(f"SELECT version, status, updated_at, updated_by, reason, proposal_id FROM {Q_HIST} WHERE doc_id=%s ORDER BY version", (doc_id,))
            doc["history"] = [{**dict(h), "updated_at": str(h["updated_at"])} for h in await cur.fetchall()]
        await atlas._log_events(cur, [{"event": "opened", "source": doc["kind"], "origin_key": doc_id, "chunk_no": None,
                                       "machine": caller_machine(), "session_id": session_id, "session_name": session_name}])
    doc["found"] = True
    return doc


def _needs_proposal(kind: str, scope: str, header: Dict[str, Any], writer: str, caller_ws: Optional[str]) -> Optional[str]:
    """바로 쓰지 않고 후보로 보낼 이유. None 이면 바로 쓴다."""
    if kind in APPROVAL_KINDS:
        return f"{kind} 는 늘 후보로만 바뀐다"
    if kind == "machine_doc":
        if (header or {}).get("bucket") == "C":
            return "C 버킷 머신 문서(사람이 판정)"
        if scope == "global":
            return "전역 머신 문서(4대에 바로 퍼짐)"
        if scope.startswith("machine:") and scope[8:] != writer:
            return f"다른 머신 범위({scope}, 쓰는 머신 {writer})"
    if scope.startswith("ws:") and scope[3:] != (caller_ws or ""):
        return f"다른 워크스페이스 범위({scope}, 호출 워크스페이스 {caller_ws})"
    return None


async def do_put_doc(server, kind: str, scope: str, name: str, body: str, base_version: int, reason: str,
                     header: Optional[Dict[str, Any]] = None, caller_ws: Optional[str] = None, retire: bool = False) -> Dict[str, Any]:
    _check_kind(kind); _check_scope(scope); _check_name(name)
    if not isinstance(body, str):
        raise ValueError("body must be a string")
    if not reason:
        raise ValueError("reason is required")
    writer = caller_machine()
    did = doc_id_of(kind, scope, name)
    async with _tx(server) as cur:
        await cur.execute(f"SELECT version, header, status FROM {Q_DOCS} WHERE doc_id=%s FOR UPDATE", (did,))
        row = await cur.fetchone()
        cur_ver = int(row["version"]) if row else 0
        if int(base_version) != cur_ver:
            return {"status": "conflict", "doc_id": did, "current_version": cur_ver,
                    "message": "base_version 이 지금 판과 다르다 — 지금 판을 읽고 다시 만든다"}
        hdr = header if header is not None else (_json(row["header"]) if row else {})
        why = _needs_proposal(kind, scope, hdr, writer, caller_ws)
        op = "retire" if retire else ("update" if row else "create")
        if why is None and not retire:
            ver = await _write_doc(cur, kind, scope, name, hdr, body, "active", writer, reason, None)
            return {"status": "written", "doc_id": did, "version": ver}
        await cur.execute(f"INSERT INTO {Q_PROP} (op, doc_id, kind, scope, name, base_version, header, body, reason, proposed_by, proposed_at) "
                          f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP(3))",
                          (op, did, kind, scope, name, cur_ver, json.dumps(hdr, ensure_ascii=False), body, reason, writer))
        await cur.execute("SELECT LAST_INSERT_ID() AS id")
        pid = int((await cur.fetchone())["id"])
    return {"status": "proposed", "proposal": f"P-{pid}", "proposal_id": pid, "doc_id": did, "why": why or "퇴역은 후보로만",
            "content_sha": content_sha(hdr, body)}


async def do_move_doc(server, doc_id: str, to_scope: str, reason: str) -> Dict[str, Any]:
    _check_scope(to_scope)
    writer = caller_machine()
    async with _tx(server) as cur:
        await cur.execute(f"SELECT kind, scope, name, version, header, body FROM {Q_DOCS} WHERE doc_id=%s AND status='active'", (doc_id,))
        row = await cur.fetchone()
        if not row:
            return {"status": "not_found", "doc_id": doc_id}
        await cur.execute(f"SELECT doc_id, JSON_VALUE(header,'$.mode') AS mode FROM {Q_DOCS} WHERE kind=%s AND name=%s AND scope<>%s AND status='active'",
                          (row["kind"], row["name"], row["scope"]))
        clashes = [dict(c) for c in await cur.fetchall()]
        await cur.execute(f"INSERT INTO {Q_PROP} (op, doc_id, kind, scope, name, to_scope, base_version, header, body, reason, proposed_by, proposed_at) "
                          f"VALUES ('move',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP(3))",
                          (doc_id, row["kind"], row["scope"], row["name"], to_scope, row["version"], row["header"], row["body"], reason, writer))
        await cur.execute("SELECT LAST_INSERT_ID() AS id")
        pid = int((await cur.fetchone())["id"])
    return {"status": "proposed", "proposal": f"P-{pid}", "proposal_id": pid, "doc_id": doc_id, "to_scope": to_scope,
            "same_name_elsewhere": clashes}


def _standing_covers(rule: Dict[str, Any], prop: Dict[str, Any]) -> bool:
    ids = rule.get("proposal_ids")
    if ids is not None and int(prop["proposal_id"]) not in {int(x) for x in ids}:
        return False
    for key in ("op", "kind", "scope"):
        allowed = rule.get(key)
        if allowed is not None and prop[key] not in (allowed if isinstance(allowed, list) else [allowed]):
            return False
    return True


async def do_decide_proposal(server, proposal_id: int, decision: str, approval_quote: Optional[str] = None,
                             standing_id: Optional[int] = None, test_sha: Optional[str] = None,
                             decided_by: Optional[str] = None) -> Dict[str, Any]:
    if decision not in ("approve", "reject"):
        raise ValueError("decision must be 'approve' or 'reject'")
    by = decided_by or caller_machine()
    async with _tx(server) as cur:
        await cur.execute(f"SELECT * FROM {Q_PROP} WHERE proposal_id=%s FOR UPDATE", (int(proposal_id),))
        p = await cur.fetchone()
        if not p:
            return {"status": "not_found", "proposal": f"P-{proposal_id}"}
        if p["state"] != "open":
            return {"status": "already_" + p["state"], "proposal": f"P-{proposal_id}"}
        if decision == "reject":
            await cur.execute(f"UPDATE {Q_PROP} SET state='rejected', decided_by=%s, approval_quote=%s, decided_at=UTC_TIMESTAMP(3) WHERE proposal_id=%s",
                              (by, approval_quote, int(proposal_id)))
            return {"status": "rejected", "proposal": f"P-{proposal_id}"}
        if not approval_quote and standing_id is None:
            raise PermissionError("approve needs approval_quote (a human turn) or standing_id")
        if standing_id is not None:
            await cur.execute(f"SELECT match_rule, revoked, expires_at FROM {Q_STAND} WHERE id=%s", (int(standing_id),))
            st = await cur.fetchone()
            if not st or int(st["revoked"]) or (st["expires_at"] is not None and str(st["expires_at"]) < time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())):
                raise PermissionError(f"standing approval {standing_id} is missing, revoked or expired")
            if not _standing_covers(_json(st["match_rule"]) or {}, p):
                raise PermissionError(f"standing approval {standing_id} does not cover P-{proposal_id}")
        header = _json(p["header"]) if p["header"] is not None else {}
        if p["kind"] == "rule" and p["op"] in ("create", "update") and test_sha != content_sha(header, p["body"] or ""):
            raise PermissionError("rule proposals need test_sha equal to the tested content (run the engine tests on this proposal)")
        await cur.execute(f"SELECT version, header, body FROM {Q_DOCS} WHERE doc_id=%s FOR UPDATE", (p["doc_id"],))
        cur_doc = await cur.fetchone()
        cur_ver = int(cur_doc["version"]) if cur_doc else 0
        if p["base_version"] is not None and int(p["base_version"]) != cur_ver:
            return {"status": "conflict", "proposal": f"P-{proposal_id}", "current_version": cur_ver,
                    "message": "후보를 만든 판 뒤에 문서가 바뀌었다 — 지금 판 기준으로 후보를 다시 만든다"}
        reason = f"P-{proposal_id}: {p['reason']}"
        applied: Dict[str, Any] = {}
        if p["op"] in ("create", "update"):
            applied["version"] = await _write_doc(cur, p["kind"], p["scope"], p["name"], header, p["body"] or "", "active", by, reason, int(proposal_id))
        elif p["op"] == "retire":
            applied["version"] = await _write_doc(cur, p["kind"], p["scope"], p["name"], _json(cur_doc["header"]) if cur_doc else {},
                                                  (cur_doc or {}).get("body") or "", "retired", by, reason, int(proposal_id))
        elif p["op"] == "move":
            if not cur_doc:
                return {"status": "not_found", "doc_id": p["doc_id"]}
            applied["to"] = doc_id_of(p["kind"], p["to_scope"], p["name"])
            applied["to_version"] = await _write_doc(cur, p["kind"], p["to_scope"], p["name"], _json(cur_doc["header"]),
                                                     cur_doc["body"], "active", by, reason, int(proposal_id))
            applied["from_version"] = await _write_doc(cur, p["kind"], p["scope"], p["name"], _json(cur_doc["header"]),
                                                       cur_doc["body"], "retired", by, reason, int(proposal_id))
        await cur.execute(f"UPDATE {Q_PROP} SET state='approved', decided_by=%s, approval_quote=%s, standing_id=%s, test_sha=%s, "
                          f"decided_at=UTC_TIMESTAMP(3) WHERE proposal_id=%s",
                          (by, approval_quote, standing_id, test_sha, int(proposal_id)))
        rv = await _bump(cur, "rules_version")
    _state_touch(rules=rv)
    return {"status": "approved", "proposal": f"P-{proposal_id}", "doc_id": p["doc_id"], "rules_version": rv, **applied}


async def do_add_standing_approval(server, scope_desc: str, match_rule: Dict[str, Any], quote: str, quote_session: str,
                                   quote_line: int, approver: str, expires_at: Optional[str] = None) -> Dict[str, Any]:
    if not (scope_desc and quote and quote_session and approver):
        raise ValueError("scope_desc, quote, quote_session and approver are required")
    if not isinstance(match_rule, dict) or not match_rule:
        raise ValueError("match_rule must be a non-empty object (proposal_ids / op / kind / scope)")
    async with _tx(server) as cur:
        await cur.execute(f"INSERT INTO {Q_STAND} (scope_desc, match_rule, quote, quote_session, quote_line, approver, created_at, expires_at) "
                          f"VALUES (%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP(3),%s)",
                          (scope_desc, json.dumps(match_rule, ensure_ascii=False), quote, quote_session, int(quote_line), approver, expires_at))
        await cur.execute("SELECT LAST_INSERT_ID() AS id")
        sid = int((await cur.fetchone())["id"])
    return {"status": "created", "standing_id": sid}


async def do_set_cutover(server, workspace: str, machine: str, mode: str, approval_quote: str) -> Dict[str, Any]:
    if mode not in ("file", "db"):
        raise ValueError("mode must be 'file' or 'db'")
    if not (workspace and machine and approval_quote):
        raise ValueError("workspace, machine and approval_quote are required")
    by = caller_machine()
    async with _tx(server) as cur:
        await cur.execute(f"INSERT INTO {Q_CUT} (workspace, machine, mode, switched_at, switched_by) VALUES (%s,%s,%s,UTC_TIMESTAMP(3),%s) "
                          f"ON DUPLICATE KEY UPDATE mode=VALUES(mode), switched_at=VALUES(switched_at), switched_by=VALUES(switched_by)",
                          (workspace, machine, mode, by))
        if mode == "db":   # 서버가 조각을 만들 범위가 되었으므로 그 문서들을 다음 맞춤 주기의 대상으로 만든다
            if workspace == MACHINE_DOC_CUT:
                await cur.execute(f"UPDATE {Q_DOCS} SET chunk_version = 0 WHERE kind = 'machine_doc' AND scope = %s",
                                  ("global" if machine == "global" else f"machine:{machine}",))
            else:
                await cur.execute(f"UPDATE {Q_DOCS} SET chunk_version = 0 WHERE kind = 'workspace_memory' AND scope = %s", (f"ws:{workspace}",))
        rv = await _bump(cur, "rules_version")
    _state_touch(rules=rv)
    return {"status": "set", "workspace": workspace, "machine": machine, "mode": mode, "rules_version": rv}


async def _rule_vectors(cur, scopes: List[str]) -> Dict[str, Any]:
    import numpy as np
    ph = ",".join(["%s"] * len(scopes))
    await cur.execute(f"SELECT d.doc_id, d.scope, d.name, JSON_VALUE(d.header,'$.mode') AS mode, VEC_ToText(c.embedding) AS emb "
                      f"FROM {Q_DOCS} d JOIN {atlas.Q_CHUNKS} c ON c.source='rule' AND c.origin_key=d.doc_id AND c.chunk_no=0 "
                      f"WHERE d.kind='rule' AND d.status='active' AND d.scope IN ({ph})", tuple(scopes))
    out = {}
    for r in await cur.fetchall():
        v = np.asarray(json.loads(r["emb"]), dtype=np.float32)
        n = float(np.linalg.norm(v))
        out[r["doc_id"]] = {"scope": r["scope"], "name": r["name"], "mode": r["mode"], "vec": v / n if n else v}
    return out


async def do_diff_rules(server, scope_a: str = "global", scope_b: str = "", threshold: float = 0.85) -> Dict[str, Any]:
    _check_scope(scope_a); _check_scope(scope_b)
    async with _tx(server) as cur:
        vecs = await _rule_vectors(cur, [scope_a, scope_b])
        await cur.execute(f"SELECT doc_id, COUNT(DISTINCT workspace) AS nws, MAX(workspace) AS ws FROM {Q_RULEEV} "
                          f"WHERE at >= UTC_TIMESTAMP() - INTERVAL 30 DAY AND doc_id LIKE %s GROUP BY doc_id", (f"rule:{scope_a}:%",))
        usage = {r["doc_id"]: (int(r["nws"]), r["ws"]) for r in await cur.fetchall()}
    a = {k: v for k, v in vecs.items() if v["scope"] == scope_a}
    b = {k: v for k, v in vecs.items() if v["scope"] == scope_b}
    pairs = []
    for ka, va in a.items():
        for kb, vb in b.items():
            sim = float(va["vec"] @ vb["vec"])
            if va["name"] == vb["name"]:
                rel = vb["mode"] or "replace"
                advice = "전역에 흡수 후보" if rel == "extend" and sim >= 0.9 else None
                pairs.append({"a": ka, "b": kb, "relation": f"같은 id({rel})", "similarity": round(sim, 3), "advice": advice})
            elif sim >= float(threshold):
                pairs.append({"a": ka, "b": kb, "relation": "다른 id·비슷함", "similarity": round(sim, 3), "advice": "중복 후보"})
    demote = [{"doc_id": k, "only_workspace": ws, "advice": "워크스페이스 강등 후보"} for k, (n, ws) in usage.items() if n == 1 and k in a]
    pairs.sort(key=lambda x: -x["similarity"])
    return {"scope_a": scope_a, "scope_b": scope_b, "pairs": pairs, "demote_candidates": demote,
            "note": "비슷함은 각 규칙의 첫 조각 임베딩 코사인이다(조각이 아직 없는 규칙은 빠진다)"}


async def do_rule_stats(server, doc_id: Optional[str] = None, days: int = 7) -> Dict[str, Any]:
    days = max(1, min(int(days), 90))
    where, params = [f"at >= UTC_TIMESTAMP() - INTERVAL {days} DAY", "doc_id IS NOT NULL"], []
    if doc_id:
        where.append("doc_id = %s"); params.append(doc_id)
    async with _tx(server) as cur:
        await cur.execute(f"SELECT doc_id, decision, reaction, COUNT(*) AS n, AVG(distance) AS avg_distance FROM {Q_RULEEV} "
                          f"WHERE {' AND '.join(where)} GROUP BY doc_id, decision, reaction", tuple(params))
        rows = await cur.fetchall()
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        d = out.setdefault(r["doc_id"], {"total": 0, "decisions": {}, "reactions": {}, "avg_distance": None})
        d["total"] += int(r["n"])
        d["decisions"][r["decision"]] = d["decisions"].get(r["decision"], 0) + int(r["n"])
        if r["reaction"]:
            d["reactions"][r["reaction"]] = d["reactions"].get(r["reaction"], 0) + int(r["n"])
        if r["avg_distance"] is not None:
            d["avg_distance"] = round(float(r["avg_distance"]), 4)
    return {"days": days, "docs": out}


# ---------------------------------------------------------------------------
# 파이프라인 도구
# ---------------------------------------------------------------------------
async def do_import_docs(server, rows: List[Dict[str, Any]], by: str) -> Dict[str, Any]:
    """파일에서 가져온 판을 바로 쓴다(후보를 거치지 않음). 본문·머리가 같으면 건너뛴다.
    가져오기가 아닌 경로(put_doc·decide_proposal)로 쓰인 판은 덮지 않는다 — 정본이 DB 로 넘어간 뒤의 편집을 동결된 파일 판이 되돌리지 않게."""
    counts = {"written": 0, "unchanged": 0, "kept_db": 0, "error": 0}
    errors = []
    kept = []
    changed_rules = False
    async with _tx(server) as cur:
        for r in rows:
            try:
                kind, scope, name = r["kind"], r["scope"], r["name"]
                _check_kind(kind); _check_scope(scope); _check_name(name)
                header, body = r.get("header") or {}, r.get("body") or ""
                did = doc_id_of(kind, scope, name)
                await cur.execute(f"SELECT header, body_sha, status, updated_by FROM {Q_DOCS} WHERE doc_id=%s", (did,))
                old = await cur.fetchone()
                sha = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
                if old and old["body_sha"] == sha and (_json(old["header"]) or {}) == header and old["status"] == "active":
                    counts["unchanged"] += 1
                    continue
                if old and not str(old["updated_by"] or "").endswith("/import"):
                    counts["kept_db"] += 1
                    kept.append(did)
                    continue
                await _write_doc(cur, kind, scope, name, header, body, "active", by, r.get("reason") or "import", None)
                counts["written"] += 1
                changed_rules = changed_rules or kind in APPROVAL_KINDS
            except Exception as e:  # noqa: BLE001
                counts["error"] += 1
                errors.append(f"{r.get('kind')}:{r.get('scope')}:{r.get('name')}: {str(e)[:120]}")
        rv = await _bump(cur, "rules_version") if changed_rules else None
    if rv is not None:
        _state_touch(rules=rv)
    return {"counts": counts, "errors": errors[:20], "kept_db": kept[:200], "rules_version": rv}


async def do_add_rule_events(server, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = 0
    async with _tx(server) as cur:
        for r in rows:
            await cur.execute(f"INSERT IGNORE INTO {Q_RULEEV} (at, machine, session_id, session_name, workspace, event_name, tool, doc_id, "
                              f"decision, subject_sha, input_sha, distance, reaction, event_uid) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                              (r["at"], r["machine"], r["session_id"], r.get("session_name"), r.get("workspace"), r["event_name"],
                               r.get("tool"), r.get("doc_id"), r["decision"], r.get("subject_sha"), r.get("input_sha"),
                               r.get("distance"), r.get("reaction"), r["event_uid"]))
            n += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    return {"inserted": n, "given": len(rows)}


REACTIONS = ("changed", "checked-rerun", "instant-rerun", "none")


async def do_set_rule_reactions(server, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """판정 뒤의 반응을 채운다. 비어 있는 행만 바꾸므로(이미 채운 행은 그대로) 같은 계산을 다시 보내도 값이 흔들리지 않는다."""
    n = 0
    async with _tx(server) as cur:
        for r in rows:
            if r.get("reaction") not in REACTIONS:
                raise ValueError(f"reaction must be one of {REACTIONS}")
            await cur.execute(f"UPDATE {Q_RULEEV} SET reaction=%s WHERE event_uid=%s AND reaction IS NULL", (r["reaction"], r["event_uid"]))
            n += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    return {"updated": n, "given": len(rows)}


async def do_set_tuning(server, k: str, v: float, reason: str) -> Dict[str, Any]:
    if not reason:
        raise ValueError("reason is required")
    async with _tx(server) as cur:
        await cur.execute(f"SELECT v, lo, hi FROM {Q_TUNING} WHERE k=%s FOR UPDATE", (k,))
        row = await cur.fetchone()
        if k in ("rules_version", "tuning_version"):
            raise PermissionError(f"{k} is maintained by the server")
        if row is None and not k.startswith("mute:"):
            raise ValueError(f"unknown tuning key {k!r} (only existing keys or mute:<doc_id>)")
        lo, hi = (row["lo"], row["hi"]) if row else (0.0, 1.0)
        if (lo is not None and float(v) < float(lo)) or (hi is not None and float(v) > float(hi)):
            raise PermissionError(f"{k}={v} is outside the allowed range [{lo}, {hi}]")
        await cur.execute(f"INSERT INTO {Q_TUNING} (k, v, lo, hi, updated_at, reason) VALUES (%s,%s,%s,%s,UTC_TIMESTAMP(3),%s) "
                          f"ON DUPLICATE KEY UPDATE v=VALUES(v), updated_at=VALUES(updated_at), reason=VALUES(reason)",
                          (k, float(v), lo, hi, reason))
        tv = await _bump(cur, "tuning_version")
    _state_touch(tuning_v=tv, tuning=(k, float(v)))
    return {"status": "set", "k": k, "v": float(v), "tuning_version": tv}


async def do_open_proposal(server, op: str, kind: str, scope: str, name: str, reason: str, by: str,
                           header: Optional[Dict[str, Any]] = None, body: Optional[str] = None,
                           base_version: Optional[int] = None, to_scope: Optional[str] = None) -> Dict[str, Any]:
    if op not in ("create", "update", "move", "retire"):
        raise ValueError("op must be create, update, move or retire")
    _check_kind(kind); _check_scope(scope); _check_name(name)
    did = doc_id_of(kind, scope, name)
    async with _tx(server) as cur:
        await cur.execute(f"INSERT INTO {Q_PROP} (op, doc_id, kind, scope, name, to_scope, base_version, header, body, reason, proposed_by, proposed_at) "
                          f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP(3))",
                          (op, did, kind, scope, name, to_scope, base_version, json.dumps(header, ensure_ascii=False) if header is not None else None,
                           body, reason, by))
        await cur.execute("SELECT LAST_INSERT_ID() AS id")
        pid = int((await cur.fetchone())["id"])
    return {"status": "proposed", "proposal": f"P-{pid}", "proposal_id": pid}


# ---------------------------------------------------------------------------
# 훅 전용 HTTP 경로
# ---------------------------------------------------------------------------
async def http_rules(server, ws: Optional[str], machine: Optional[str], since: Optional[int], nonce: str) -> Dict[str, Any]:
    """그 머신·워크스페이스에 걸리는 active 규칙 전부. nonce 를 규칙을 읽는 같은 SELECT 로 함께 뽑아 돌려준다 —
    서버의 DB 연결이 어긋나 앞 질의의 결과가 오면 행의 echo 가 요청과 달라 클라이언트가 연결 실패로 본다."""
    scopes = ["global"]
    if machine:
        scopes.append(f"machine:{machine}")
    ws_scope = f"ws:{ws}" if ws else None
    async with _tx(server) as cur:
        await cur.execute(f"SELECT %s AS echo, v AS rules_version, "
                          f"(SELECT mode FROM {Q_CUT} WHERE workspace=%s AND machine=%s) AS ws_mode FROM {Q_TUNING} WHERE k='rules_version'",
                          (nonce, ws or "", machine or ""))
        head = await cur.fetchone()
        if not head:
            raise RuntimeError("rules_version missing")
        rv = int(head["rules_version"])
        ws_mode = head["ws_mode"] or "file"
        if since is not None and int(since) == rv:
            return {"echo": head["echo"], "rules_version": rv, "ws_mode": ws_mode, "unchanged": True}
        want = scopes + ([ws_scope] if ws_scope and ws_mode == "db" else [])
        ph = ",".join(["%s"] * len(want))
        await cur.execute(f"SELECT %s AS echo, doc_id, scope, name, version, header, body FROM {Q_DOCS} "
                          f"WHERE kind='rule' AND status='active' AND scope IN ({ph}) ORDER BY scope, name", tuple([nonce] + want))
        rows = await cur.fetchall()
    bad = [r for r in rows if r["echo"] != nonce]
    if bad or head["echo"] != nonce:
        raise RuntimeError("echo mismatch — DB connection returned another query's result")
    rules = [{"doc_id": r["doc_id"], "scope": r["scope"], "name": r["name"], "version": int(r["version"]),
              "header": _json(r["header"]), "body": r["body"]} for r in rows]
    return {"echo": nonce, "rules_version": rv, "ws_mode": ws_mode, "unchanged": False, "rules": rules}


async def http_docs(server, kinds: List[str], scopes: List[str], nonce: str) -> Dict[str, Any]:
    """호스트 스크립트(머신 문서 로컬 사본·CLAUDE.md 생성)용 일괄 읽기. `get_doc` 과 달리 `opened` 를 남기지 않는다 —
    판이 바뀔 때마다 4대가 읽으면 사람·세션의 열람 신호가 묻히므로. `etag` 는 (doc_id, 판) 목록의 해시라 바뀌지 않았으면 쓰지 않게 한다."""
    for k in kinds:
        _check_kind(k)
    for s in scopes:
        _check_scope(s)
    if not kinds or not scopes:
        raise ValueError("kinds and scopes are required")
    kph, sph = ",".join(["%s"] * len(kinds)), ",".join(["%s"] * len(scopes))
    async with _tx(server) as cur:
        await cur.execute(f"SELECT %s AS echo, doc_id, kind, scope, name, version, header, body, body_sha, updated_at FROM {Q_DOCS} "
                          f"WHERE status='active' AND kind IN ({kph}) AND scope IN ({sph}) ORDER BY kind, scope, name",
                          tuple([nonce] + kinds + scopes))
        rows = await cur.fetchall()
    if any(r["echo"] != nonce for r in rows):
        raise RuntimeError("echo mismatch — DB connection returned another query's result")
    docs = [{"doc_id": r["doc_id"], "kind": r["kind"], "scope": r["scope"], "name": r["name"], "version": int(r["version"]),
             "header": _json(r["header"]), "body": r["body"], "body_sha": r["body_sha"], "updated_at": str(r["updated_at"])} for r in rows]
    etag = hashlib.sha256("\n".join(f"{d['doc_id']}@{d['version']}" for d in docs).encode("utf-8")).hexdigest()[:16]
    return {"echo": nonce, "etag": etag, "count": len(docs), "docs": docs}


async def http_owned(server) -> Dict[str, Any]:
    """서버가 검색 조각을 만드는 파일 종류 범위 — 파일 업로더가 이 키·프로젝트의 파일을 올리지 않게 알려 준다.
    전역 머신 문서는 한 벌이라 모든 머신 접두사로 펼친다. 퇴역한 문서도 포함한다(파일이 남아 있어도 다시 올리지 않게)."""
    async with _tx(server) as cur:
        owned = await owned_file_scopes(cur)
        prefixes: List[str] = []
        projects: List[str] = []
        if owned["machine_doc"]:
            ph = ",".join(["%s"] * len(owned["machine_doc"]))
            await cur.execute(f"SELECT scope, JSON_VALUE(header,'$.legacy_origin_key') AS lk FROM {Q_DOCS} WHERE kind='machine_doc' AND scope IN ({ph})",
                              tuple(owned["machine_doc"]))
            for r in await cur.fetchall():
                lk = r["lk"]
                if not lk:
                    continue
                if r["scope"] == "global" and ":" in lk:
                    fname = lk.partition(":")[2]
                    prefixes += [f"{h}:{fname}" for h in MACHINES]
                else:
                    prefixes.append(lk)
        if owned["workspace_memory"]:
            ph = ",".join(["%s"] * len(owned["workspace_memory"]))
            await cur.execute(f"SELECT DISTINCT JSON_VALUE(header,'$.legacy_origin_key') AS lk FROM {Q_DOCS} WHERE kind='workspace_memory' AND scope IN ({ph})",
                              tuple(owned["workspace_memory"]))
            projects = sorted({r["lk"].split("/")[0] for r in await cur.fetchall() if r["lk"] and "/" in r["lk"]})
    return {"machine_doc_prefixes": sorted(set(prefixes)), "workspace_projects": projects}


def register_docs(server, embedding_service) -> None:
    global STATE, EMB
    mcp = server.mcp
    EMB = embedding_service
    STATE = DocsState(server)
    STATE.ensure_started()

    from starlette.requests import Request
    from starlette.responses import JSONResponse

    @mcp.custom_route("/atlas/version", methods=["GET"])
    async def atlas_version(request: Request):
        STATE.ensure_started()
        if STATE.rules_version is None:
            try:
                await STATE.load()
            except Exception as e:  # noqa: BLE001
                return JSONResponse({"error": str(e)[:200]}, status_code=503)
        return JSONResponse({"rules_version": STATE.rules_version, "tuning_version": STATE.tuning_version,
                             "tuning": STATE.tuning, "loaded_age_sec": round(time.time() - STATE.loaded_at, 1),
                             "nonce": request.query_params.get("nonce")})

    @mcp.custom_route("/atlas/rules", methods=["GET"])
    async def atlas_rules(request: Request):
        STATE.ensure_started()
        qp = request.query_params
        nonce = qp.get("nonce")
        if not nonce or len(nonce) > 64:
            return JSONResponse({"error": "nonce is required (1..64 chars)"}, status_code=400)
        try:
            since = int(qp["since"]) if qp.get("since") not in (None, "") else None
            return JSONResponse(await http_rules(server, qp.get("ws"), qp.get("machine"), since, nonce))
        except Exception as e:  # noqa: BLE001
            logger.error(f"atlas docs: /atlas/rules 실패: {e}")
            return JSONResponse({"error": str(e)[:200]}, status_code=503)

    @mcp.custom_route("/atlas/docs", methods=["GET"])
    async def atlas_docs(request: Request):
        qp = request.query_params
        nonce = qp.get("nonce")
        if not nonce or len(nonce) > 64:
            return JSONResponse({"error": "nonce is required (1..64 chars)"}, status_code=400)
        try:
            kinds = [k for k in (qp.get("kinds") or "").split(",") if k]
            scopes = [s for s in (qp.get("scopes") or "").split(",") if s]
            return JSONResponse(await http_docs(server, kinds, scopes, nonce))
        except ValueError as e:
            return JSONResponse({"error": str(e)[:200]}, status_code=400)
        except Exception as e:  # noqa: BLE001
            logger.error(f"atlas docs: /atlas/docs 실패: {e}")
            return JSONResponse({"error": str(e)[:200]}, status_code=503)

    @mcp.custom_route("/atlas/owned", methods=["GET"])
    async def atlas_owned(request: Request):
        try:
            return JSONResponse(await http_owned(server))
        except Exception as e:  # noqa: BLE001
            logger.error(f"atlas docs: /atlas/owned 실패: {e}")
            return JSONResponse({"error": str(e)[:200]}, status_code=503)

    @mcp.custom_route("/atlas/search", methods=["POST"])
    async def atlas_search(request: Request):
        try:
            a = await request.json()
            res = await atlas.do_search(server, embedding_service, a["query"], a.get("sources"), int(a.get("per_source_n", 15)),
                                        int(a.get("return_n", 10)), int(a.get("top_n", 3)), a.get("before"), a.get("exclude_session_id"),
                                        a.get("exclude_after_line"), a.get("machine"), a.get("project"), a.get("session_id"),
                                        a.get("session_name"), bool(a.get("log_shown", True)), None, a.get("rank", "distance"),
                                        a.get("max_distance"), a.get("skip_log_keys"))
            return JSONResponse(res)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": str(e)[:200]}, status_code=503)

    @mcp.tool
    async def list_docs(kind: Optional[str] = None, scope: Optional[str] = None, status: str = "active",
                        query: Optional[str] = None, with_stats: bool = False, limit: int = 500) -> Dict[str, Any]:
        """List atlas docs (rules, CLAUDE.md sections, machine docs, workspace memory). with_stats adds 7/30-day rule hook stats."""
        STATE.ensure_started()
        return await do_list_docs(server, kind, scope, status, query, with_stats, limit)

    @mcp.tool
    async def get_doc(doc_id: str, version: Optional[int] = None, with_history: bool = False,
                      session_id: str = "", session_name: Optional[str] = None) -> Dict[str, Any]:
        """Open one atlas doc (current or a given version), its open proposals and optionally its history."""
        return await do_get_doc(server, doc_id, version, with_history, session_id, session_name)

    @mcp.tool
    async def put_doc(kind: str, scope: str, name: str, body: str, base_version: int, reason: str,
                      header: Optional[Dict[str, Any]] = None, caller_ws: Optional[str] = None, retire: bool = False) -> Dict[str, Any]:
        """Write a doc. Machine docs / workspace memory of your own scope are written directly; rules, CLAUDE.md sections,
        global or other-scope docs become a proposal P-<id>. base_version must equal the current version (0 for a new doc)."""
        STATE.ensure_started()
        return await do_put_doc(server, kind, scope, name, body, base_version, reason, header, caller_ws, retire)

    @mcp.tool
    async def move_doc(doc_id: str, to_scope: str, reason: str) -> Dict[str, Any]:
        """Propose moving a doc to another scope (global <-> ws:<name>). Lists same-name docs elsewhere."""
        return await do_move_doc(server, doc_id, to_scope, reason)

    @mcp.tool
    async def diff_rules(scope_a: str = "global", scope_b: str = "", threshold: float = 0.85) -> Dict[str, Any]:
        """Compare rules of two scopes: same ids, near-duplicates, and demotion candidates."""
        return await do_diff_rules(server, scope_a, scope_b, threshold)

    @mcp.tool
    async def rule_stats(doc_id: Optional[str] = None, days: int = 7) -> Dict[str, Any]:
        """Rule hook decisions and reactions per doc over the last days."""
        return await do_rule_stats(server, doc_id, days)

    @mcp.tool
    async def add_standing_approval(scope_desc: str, match_rule: Dict[str, Any], quote: str, quote_session: str,
                                    quote_line: int, approver: str, expires_at: Optional[str] = None) -> Dict[str, Any]:
        """Record a standing/bulk approval (the hook checks the quote is a human turn). match_rule: proposal_ids / op / kind / scope."""
        return await do_add_standing_approval(server, scope_desc, match_rule, quote, quote_session, quote_line, approver, expires_at)

    @mcp.tool
    async def set_cutover(workspace: str, machine: str, mode: str, approval_quote: str) -> Dict[str, Any]:
        """Switch where a workspace's rules are read from on one machine ('file' or 'db')."""
        return await do_set_cutover(server, workspace, machine, mode, approval_quote)

    @mcp.tool
    async def decide_proposal(proposal_id: int, decision: str, approval_quote: Optional[str] = None,
                              standing_id: Optional[int] = None, test_sha: Optional[str] = None,
                              decided_by: Optional[str] = None) -> Dict[str, Any]:
        """Approve or reject proposal P-<id>. Approve needs a human approval quote or a standing approval; rule proposals
        also need test_sha of the tested content. Refused if the doc changed since the proposal was made."""
        return await do_decide_proposal(server, proposal_id, decision, approval_quote, standing_id, test_sha, decided_by)

    @mcp.tool
    async def import_docs(rows: List[Dict[str, Any]], by: str, token: Optional[str] = None) -> Dict[str, Any]:
        """(pipeline only) Load docs from files as the current version, skipping unchanged ones."""
        _check_token(token)
        return await do_import_docs(server, rows, by)

    @mcp.tool
    async def add_rule_events(rows: List[Dict[str, Any]], token: Optional[str] = None) -> Dict[str, Any]:
        """(pipeline only) Load rule hook decision rows (deduplicated by event_uid)."""
        _check_token(token)
        return await do_add_rule_events(server, rows)

    @mcp.tool
    async def set_rule_reactions(rows: List[Dict[str, Any]], token: Optional[str] = None) -> Dict[str, Any]:
        """(pipeline only) Fill the reaction column of rule hook decision rows (only rows still empty)."""
        _check_token(token)
        return await do_set_rule_reactions(server, rows)

    @mcp.tool
    async def set_tuning(k: str, v: float, reason: str, token: Optional[str] = None) -> Dict[str, Any]:
        """(pipeline only) Set a tuning value within its allowed range."""
        _check_token(token)
        return await do_set_tuning(server, k, v, reason)

    @mcp.tool
    async def open_proposal(op: str, kind: str, scope: str, name: str, reason: str, by: str,
                            header: Optional[Dict[str, Any]] = None, body: Optional[str] = None,
                            base_version: Optional[int] = None, to_scope: Optional[str] = None,
                            token: Optional[str] = None) -> Dict[str, Any]:
        """(pipeline only) Open a proposal for a human to decide."""
        _check_token(token)
        return await do_open_proposal(server, op, kind, scope, name, reason, by, header, body, base_version, to_scope)

    logger.info(f"Registered atlas docs tools and routes ({Q_DOCS}, pipeline token {'on' if PIPELINE_TOKEN else 'off'})")


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2 and sys.argv[1] == "--print-ddl":
        print(ddl(sys.argv[2] if len(sys.argv) > 2 else ""))
