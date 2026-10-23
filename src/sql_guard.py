"""실행할 SQL 이 한 문장인지 센다.

접속의 MULTI_STATEMENTS 비트를 끄는 것(custom_connection.SafeConnection)은 DB 에 바로 붙을 때만 듣는다.
MaxScale 을 거치면 MaxScale 이 DB 와 맺는 연결이 따로라 그 비트가 DB 에 닿지 않아 여러 문장이 그대로 실행된다.
그래서 서버가 보내기 전에 직접 센다.
"""


def count_statements(sql: str) -> int:
    """문장 수. 문자열·식별자 따옴표와 주석 안의 `;` 는 세지 않고, 끝에 붙은 `;` 하나는 문장을 늘리지 않는다.

    MariaDB 가 실행하는 `/*! … */`·`/*M! … */` 주석은 코드로 본다(그 안의 `;` 도 센다).
    """
    n = len(sql)
    i = 0
    count = 0
    has_code = False   # 지금 문장에 주석·공백이 아닌 글자가 나왔는가
    while i < n:
        c = sql[i]
        if c in ("'", '"', "`"):
            q = c
            i += 1
            while i < n:
                if sql[i] == "\\" and q != "`":
                    i += 2
                    continue
                if sql[i] == q:
                    if i + 1 < n and sql[i + 1] == q:   # 따옴표 두 번은 글자 하나
                        i += 2
                        continue
                    break
                i += 1
            i += 1
            has_code = True
            continue
        if c == "#" or (c == "-" and sql.startswith("--", i) and (i + 2 >= n or sql[i + 2] in " \t\r\n")):
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if c == "/" and sql.startswith("/*", i):
            if sql.startswith("/*!", i) or sql.startswith("/*M!", i):
                i += 3 if sql[i + 2] == "!" else 4   # 실행되는 주석: 여는 표시만 건너뛰고 안은 코드로 읽는다
                has_code = True
                continue
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == ";":
            if has_code:
                count += 1
            has_code = False
            i += 1
            continue
        if not c.isspace():
            has_code = True
        i += 1
    if has_code:
        count += 1
    return count


def ensure_single_statement(sql: str) -> None:
    if count_statements(sql) > 1:
        raise PermissionError("Operation forbidden: multiple SQL statements in one call are not allowed.")
