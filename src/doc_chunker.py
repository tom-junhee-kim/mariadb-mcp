"""머신 문서·워크스페이스 메모리를 검색 조각으로 나눈다 — 파일 업로더(atlas-pipeline.py)의 나누기와 같은 입력에 같은 조각을 낸다.

정본이 DB(`docs`)로 넘어가면 이 조각을 서버가 만들고(`atlas_docs.sync_chunks`), 그 전에는 파일 업로더가 만든다. 두 쪽의 결과가 달라지면
같은 키의 조각이 번갈아 다시 임베딩되므로 상수·함수를 업로더와 맞춰 둔다(동일성은 `toolkit/atlas-pipeline/eval/chunk_parity.py` 가 전수 대조한다).
"""
import re
from typing import Dict, List, Tuple

CHUNK_MAX = 800            # 임베딩 모델 입력 512토큰 안에 드는 글자 수
CHUNK_MIN_FILL = 300       # 경계를 찾을 때 한 조각이 최소한 채울 글자 수
CONTEXT_CHARS = 160        # 조각 앞뒤 문맥 길이(식별자 토큰 상한과 같다)
CHUNKER_VERSION = "1"      # 파일 업로더와 같은 값 — 같은 내용이면 서버가 임베딩 전에 건너뛴다

_SENT_END = re.compile(r"(?:[.!?。]|다\.|요\.)\s")


def split_chunks(text: str, limit: int = CHUNK_MAX) -> List[Tuple[int, int]]:
    """text 를 limit 자 이하의 연속 구간 [(시작, 끝)] 으로 나눈다. 문단 → 줄 → 문장 → 공백 → 글자 수 순으로 경계를 찾는다."""
    out: List[Tuple[int, int]] = []
    n = len(text)
    start = 0
    while start < n:
        while start < n and text[start].isspace():
            start += 1
        if start >= n:
            break
        if n - start <= limit:
            end = n
        else:
            window = text[start:start + limit]
            floor = CHUNK_MIN_FILL
            cut = window.rfind("\n\n", floor)
            if cut >= 0:
                cut += 2
            else:
                cut = window.rfind("\n", floor)
                if cut >= 0:
                    cut += 1
                else:
                    cut = -1
                    for m in _SENT_END.finditer(window):
                        if m.end() >= floor:
                            cut = m.end()
                    if cut < 0:
                        cut = window.rfind(" ", floor)
                        cut = cut + 1 if cut >= 0 else limit
            end = start + cut
        e = end
        while e > start and text[e - 1].isspace():
            e -= 1
        if e > start:
            out.append((start, e))
        start = end
    return out


def chunk_records(text: str, base_meta: dict, limit: int = CHUNK_MAX) -> List[dict]:
    """add_atlas 의 chunks 인자 목록. 앞뒤 문맥(before/after)은 원문의 이웃 글자다."""
    spans = split_chunks(text, limit)
    total = len(spans)
    return [{"chunk_no": i, "chunk_count": total, "document": text[s:e],
             "before": text[max(0, s - CONTEXT_CHARS):s], "after": text[e:e + CONTEXT_CHARS],
             "metadata": dict(base_meta)} for i, (s, e) in enumerate(spans)]


def parse_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    """`---` 머리말의 단순 `키: 값` 줄을 읽는다. (머리말, 본문)"""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    meta = {}
    for line in text[3:end].splitlines():
        if ":" in line and not line.startswith((" ", "\t", "-")):
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip().strip("'\"")
    return meta, text[end + 4:].lstrip("\n")


def split_sections(body: str) -> List[Tuple[str, str]]:
    """`## ` 제목 줄로 절을 나눈다. 0번은 첫 `## ` 앞의 부분이다. [(절 제목, 절 본문)]"""
    parts: List[Tuple[str, List[str]]] = [("", [])]
    for line in body.splitlines():
        if line.startswith("## "):
            parts.append((line[3:].strip(), [line]))
        else:
            parts[-1][1].append(line)
    return [(t, "\n".join(ls).strip()) for t, ls in parts if "\n".join(ls).strip()]


def doc_groups(kind: str, key_prefix: str, text: str, base: dict) -> List[Tuple[str, List[dict]]]:
    """문서 하나를 [(origin_key, 조각 목록)] 으로. 머신 문서는 `##` 절마다 키 `<접두>#<절 번호>`, 워크스페이스 메모리는 파일 전체가 키 하나다."""
    fm, body = parse_frontmatter(text)
    doc_title = fm.get("name") or base.get("doc_title") or ""
    base = dict(base, doc_title=doc_title, chunker_version=CHUNKER_VERSION)
    for k in ("name", "description", "type"):
        if fm.get(k):
            base[f"fm_{k}"] = fm[k]
    if fm.get("description"):
        base["title"] = fm["description"][:300]   # chunks.title 은 VARCHAR(300)
    if kind == "machine_doc":
        groups = []
        for no, (title, sect) in enumerate(split_sections(body)):
            groups.append((f"{key_prefix}#{no}", chunk_records(sect, dict(base, section_no=no, section_title=title or doc_title))))
        return groups
    return [(key_prefix, chunk_records(text, base))]
