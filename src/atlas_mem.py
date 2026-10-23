# atlas_mem.py — 아틀라스 메모리 정확 계산
#
# chunks 의 벡터를 프로세스 메모리(numpy)에 올려 코사인을 정확히 계산한다. MariaDB 벡터 인덱스는 근사 검색이고
# WHERE 가 인덱스가 뽑은 후보 안에서만 걸러져 운영 규모에서 후보를 잃으므로 검색에 쓰지 않는다.
# DB 는 정본이다 — 새 행은 id 순서로, 사라진 행은 id 목록 대조로 따라잡는다(UUIDv7 이라 id 순서 = 업로드 순서).
import asyncio
import os
import time
from typing import Any, Dict, List, Optional

import numpy as np

from config import logger

DIM = 1024
SRC_CODES = {"transcript": 0, "auto_memory": 1, "workspace_memory": 2, "machine_doc": 3, "rule": 4, "guide": 5}
REFRESH_SEC = float(os.getenv("ATLAS_MEM_REFRESH_SEC", "30"))
RECONCILE_SEC = float(os.getenv("ATLAS_MEM_RECONCILE_SEC", "300"))
RETRY_SEC = 30.0
PAGE = 2000
_COLS = ("id, source, origin_key, chunk_no, machine, project, session_name, title, item_at, turn_at_source, rule_score, "
         "content_hash, session_id, CAST(JSON_VALUE(metadata,'$.line_start') AS UNSIGNED), "
         "JSON_VALUE(metadata,'$.doc_title'), JSON_VALUE(metadata,'$.section_title'), embedding")


class MemoryIndex:
    def __init__(self, server, qtable: str, instance: str):
        self.server = server
        self.qtable = qtable          # `db`.`table` 꼴(따옴표 포함)
        self.instance = instance
        self.ready = False
        self.error: Optional[str] = None
        self.started = False
        self.n = 0                    # 쓴 칸 수(무효 칸 포함)
        self.cap = 0
        self.vecs = np.zeros((0, DIM), np.float32)
        self.valid = np.zeros(0, bool)
        self.item_at = np.zeros(0, np.int64)
        self.src = np.zeros(0, np.int8)
        self.sess = np.zeros(0, np.int32)
        self.line = np.zeros(0, np.int64)
        self.meta: List[Optional[tuple]] = []     # (id, origin_key, chunk_no, machine, project, session_name, title, turn_at_source, rule_score, content_hash, doc_title, section_title)
        self.slot_by_id: Dict[str, int] = {}
        self.sess_codes: Dict[str, int] = {}
        self.last_id = ""
        self.stats = {"loaded": 0, "added": 0, "removed": 0, "reconciles": 0, "last_refresh": 0.0, "load_sec": 0.0}

    # ---- 공간 -------------------------------------------------------------------------------------------------
    def _grow(self, need: int) -> None:
        if need <= self.cap:
            return
        new_cap = max(need, int(self.cap * 1.5), 8192)

        def ext(a, shape_tail=()):
            out = np.zeros((new_cap,) + shape_tail, a.dtype)
            out[:self.cap] = a[:self.cap]
            return out
        self.vecs = ext(self.vecs, (DIM,))
        self.valid = ext(self.valid)
        self.item_at = ext(self.item_at)
        self.src = ext(self.src)
        self.sess = ext(self.sess)
        self.line = ext(self.line)
        self.meta.extend([None] * (new_cap - self.cap))
        self.cap = new_cap

    def _add_rows(self, rows) -> int:
        """SQL 행(_COLS 순서)을 메모리에 넣는다. 이미 있는 id 는 건너뛴다. 넣은 수를 돌려준다."""
        fresh = [r for r in rows if r[0] not in self.slot_by_id]
        if not fresh:
            return 0
        self._grow(self.n + len(fresh))
        added = 0
        for r in fresh:
            (rid, source, okey, cno, machine, project, sname, title, item_at, tsrc, rule, chash, sid, line, dtitle, stitle, emb) = r
            code = SRC_CODES.get(source)
            if code is None or emb is None or len(emb) != DIM * 4:
                continue
            v = np.frombuffer(emb, dtype="<f4")
            norm = float(np.linalg.norm(v))
            if norm == 0.0:
                continue
            i = self.n
            self.vecs[i] = v / norm
            self.valid[i] = True
            self.item_at[i] = int(item_at) if item_at is not None else 0
            self.src[i] = code
            self.sess[i] = self.sess_codes.setdefault(sid, len(self.sess_codes)) if sid else -1
            self.line[i] = int(line) if line is not None else -1
            self.meta[i] = (rid, okey, int(cno), machine, project, sname, title, tsrc, rule, chash, dtitle, stitle)
            self.slot_by_id[rid] = i
            self.n += 1
            added += 1
            if rid > self.last_id:
                self.last_id = rid
        return added

    def _remove_ids(self, ids) -> int:
        n = 0
        for rid in ids:
            i = self.slot_by_id.pop(rid, None)
            if i is not None:
                self.valid[i] = False
                self.meta[i] = None
                n += 1
        return n

    def _compact_if_needed(self) -> None:
        live = len(self.slot_by_id)
        if self.n < 20000 or live > self.n * 0.75:
            return
        keep = np.flatnonzero(self.valid[:self.n])
        m = len(keep)
        for name in ("vecs", "valid", "item_at", "src", "sess", "line"):
            arr = getattr(self, name)
            arr[:m] = arr[keep]
            arr[m:self.n] = 0
        new_meta = [self.meta[i] for i in keep]
        self.meta[:m] = new_meta
        for j in range(m, self.n):
            self.meta[j] = None
        self.slot_by_id = {mt[0]: j for j, mt in enumerate(new_meta)}
        logger.info(f"atlas mem: 압축 {self.n} → {m} 칸")
        self.n = m

    # ---- DB 읽기 ----------------------------------------------------------------------------------------------
    async def _fetch(self, sql: str, params=()):
        pool = await self.server._get_pool(self.instance)
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                return await cur.fetchall()

    async def load_new(self) -> int:
        """마지막으로 본 id 뒤의 행을 읽어 올린다."""
        total = 0
        while True:
            rows = await self._fetch(f"SELECT {_COLS} FROM {self.qtable} WHERE id > %s ORDER BY id LIMIT {PAGE}", (self.last_id,))
            if not rows:
                break
            total += self._add_rows(rows)
            last = rows[-1][0]
            if last > self.last_id:
                self.last_id = last
            if len(rows) < PAGE:
                break
            await asyncio.sleep(0)    # 이벤트 루프에 양보
        self.stats["added"] += total
        return total

    async def reconcile(self) -> tuple:
        """DB 의 id 목록과 맞춘다: 사라진 행은 빼고, 빠진 행은 읽어 온다. (뺀 수, 넣은 수)"""
        ids_db = {r[0] for r in await self._fetch(f"SELECT id FROM {self.qtable}")}
        mem_ids = set(self.slot_by_id)
        gone = mem_ids - ids_db
        missing = sorted(ids_db - mem_ids)
        removed = self._remove_ids(gone)
        added = 0
        for k in range(0, len(missing), 500):
            part = missing[k:k + 500]
            ph = ",".join(["%s"] * len(part))
            rows = await self._fetch(f"SELECT {_COLS} FROM {self.qtable} WHERE id IN ({ph})", tuple(part))
            added += self._add_rows(rows)
            await asyncio.sleep(0)
        self._compact_if_needed()
        self.stats["removed"] += removed
        self.stats["added"] += added
        self.stats["reconciles"] += 1
        if removed or added:
            logger.info(f"atlas mem: 대조 — 뺀 행 {removed}, 넣은 행 {added}")
        return removed, added

    async def refresh_once(self) -> None:
        """시험·점검용: 새 행을 읽고 id 목록을 대조한다."""
        await self.load_new()
        await self.reconcile()
        self.stats["last_refresh"] = time.time()

    async def initial_load(self) -> None:
        t0 = time.time()
        await self.load_new()
        self.stats["loaded"] = len(self.slot_by_id)
        self.stats["load_sec"] = round(time.time() - t0, 1)
        self.stats["last_refresh"] = time.time()
        self.ready = True
        mb = self.vecs[:self.n].nbytes / 1e6
        logger.info(f"atlas mem: 적재 완료 {len(self.slot_by_id)}행 {self.stats['load_sec']}초 벡터 {mb:.0f}MB ({self.qtable})")

    async def run(self) -> None:
        """백그라운드 루프: 적재 → 주기적으로 새 행 읽기·id 대조."""
        while not self.ready:
            try:
                await self.initial_load()
            except Exception as e:  # noqa: BLE001
                self.error = str(e)[:200]
                logger.warning(f"atlas mem: 적재 실패 — {RETRY_SEC:.0f}초 뒤 다시: {e}")
                self.n = 0
                self.slot_by_id.clear()
                self.last_id = ""
                await asyncio.sleep(RETRY_SEC)
        self.error = None
        last_rec = time.time()
        while True:
            await asyncio.sleep(REFRESH_SEC)
            try:
                await self.load_new()
                self.stats["last_refresh"] = time.time()
                if time.time() - last_rec >= RECONCILE_SEC:
                    await self.reconcile()
                    last_rec = time.time()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"atlas mem: 갱신 실패(다음 주기에 다시): {e}")

    def ensure_started(self) -> None:
        """실행 중인 이벤트 루프가 있으면 백그라운드 루프를 한 번만 시작한다."""
        if self.started:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self.started = True
        loop.create_task(self.run())

    # ---- 검색 -------------------------------------------------------------------------------------------------
    def search(self, qvec, wants: Dict[str, int], before: Optional[int], exclude_sid: Optional[str],
               exclude_after_line: Optional[int]) -> Dict[str, List[Dict[str, Any]]]:
        """종류별 상위 후보(코사인 정확 계산). 필터를 먼저 걸고 계산하므로 후보를 잃지 않는다."""
        n = self.n
        q = np.asarray(qvec, dtype=np.float32)
        q = q / np.linalg.norm(q)
        sims = self.vecs[:n] @ q
        base = self.valid[:n].copy()
        if before is not None:
            base &= self.item_at[:n] < int(before)
        if exclude_sid:
            code = self.sess_codes.get(exclude_sid)
            if code is not None:
                same = self.sess[:n] == code
                if exclude_after_line is None:
                    base &= ~same
                else:
                    base &= ~(same & ((self.line[:n] < 0) | (self.line[:n] >= int(exclude_after_line))))
        out: Dict[str, List[Dict[str, Any]]] = {}
        for s, want in wants.items():
            idx = np.flatnonzero(base & (self.src[:n] == SRC_CODES[s]))
            if idx.size > want:
                idx = idx[np.argpartition(-sims[idx], want - 1)[:want]]
            idx = idx[np.argsort(-sims[idx])]
            out[s] = [self._row(int(i), float(sims[i])) for i in idx]
        return out

    def _row(self, i: int, sim: float) -> Dict[str, Any]:
        (rid, okey, cno, machine, project, sname, title, tsrc, rule, chash, dtitle, stitle) = self.meta[i]
        src = next(k for k, v in SRC_CODES.items() if v == int(self.src[i]))
        return {"_id": rid, "source": src, "origin_key": okey, "chunk_no": cno, "machine": machine, "project": project,
                "session_name": sname, "title": title, "item_at": int(self.item_at[i]), "turn_at_source": tsrc,
                "rule_score": None if rule is None else int(rule), "content_hash": chash,
                "doc_title": dtitle, "section_title": stitle, "distance": max(0.0, 1.0 - sim), "snippet": None}
