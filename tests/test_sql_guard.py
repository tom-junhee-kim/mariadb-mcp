"""sql_guard.count_statements 시험. 실행: python tests/test_sql_guard.py

`;` 뒤에 주석만 있는 경우(`SELECT 1; -- 주석`)를 MariaDB 는 빈 문장 하나로 더 세지만, 실행되는 코드가 없어 한 문장으로 둔다.
그 밖의 사례는 실제 MariaDB 11.8 의 결과 집합 수와 판정이 같다(10-24 시험 DB 대조).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from sql_guard import count_statements, ensure_single_statement  # noqa: E402

ONE = [
    "SELECT 1",
    "SELECT 1;",
    "SELECT 1;   \n",
    "SELECT 1; -- 끝 주석",
    "SELECT 1; # 끝 주석",
    "SELECT 1; /* 끝 주석 */",
    "SELECT 1;;",
    "SELECT ';'",
    'SELECT ";"',
    "SELECT `a;b` FROM t",
    "SELECT 'it''s; fine'",
    "SELECT 'back\\'slash; x'",
    "SELECT 1 -- 주석 ; DROP TABLE t",
    "SELECT 1 /* ; DROP TABLE t */",
    "SELECT 1 # ; DROP TABLE t",
]
MANY = [
    "SELECT 1; SELECT 2",
    "SELECT 1;SELECT 2;",
    "SELECT ';'; DROP TABLE t",
    "SELECT 1; -- 주석\nDELETE FROM t",
    "SELECT 1 /* x */; UPDATE t SET a=1",
    "/*!40101 SET NAMES utf8 */; SELECT 1",
    "SELECT 1 /*! ; DROP TABLE t */",
    "SELECT 1 --; DROP TABLE t",   # `--` 뒤에 공백이 없으면 주석이 아니다(MariaDB)
]


def main():
    failed = 0
    for s in ONE:
        n = count_statements(s)
        ok = n == 1
        failed += 0 if ok else 1
        print(f"{'통과' if ok else '실패'} | 한 문장 {n} | {s!r}")
    for s in MANY:
        n = count_statements(s)
        ok = n >= 2
        failed += 0 if ok else 1
        print(f"{'통과' if ok else '실패'} | 여러 문장 {n} | {s!r}")
    try:
        ensure_single_statement("SELECT 1; SELECT 2")
        print("실패 | ensure_single_statement 가 막지 않음"); failed += 1
    except PermissionError:
        print("통과 | ensure_single_statement 가 막음")
    print(f"시험 {len(ONE) + len(MANY) + 1}개 중 실패 {failed}개")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
