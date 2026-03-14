# MariaDB MCP

MariaDB MCP 서버 (Claude Code DB 연동)

## 서버 정보

- **배포 대상**: DM300S3B-B33-jhcheong (192.168.2.14)
- **이미지**: `codescent/mariadb-mcp:latest` (커스텀)
- **네트워크**: bridge (9001)
- **재시작 정책**: always

## 파일 구조

```
mariadb-mcp/
├── src/
│   ├── server.py              # MCP 서버 메인
│   ├── config.py              # 환경 변수 로딩 + 로깅
│   ├── custom_connection.py   # SafeConnection/SafePool (SQL 인젝션 방지)
│   ├── embeddings.py          # 임베딩 서비스
│   └── tests/                 # 테스트
├── .dockerignore
├── .env.example               # 환경 변수 템플릿
├── .env.production            # 운영 환경 변수 (gitignore 대상)
├── .gitattributes             # LF 정규화 + ps1만 CRLF
├── .gitignore                 # .env*, instances-*.json 제외
├── .python-version            # Python 3.11.9
├── Dockerfile                 # 멀티 스테이지 빌드
├── instances.example.json     # 멀티 인스턴스 설정 템플릿
├── instances.json             # 실제 인스턴스 설정 (gitignore 대상)
├── logs/                      # 서버 로그
├── pyproject.toml             # 프로젝트 메타데이터 (uv)
├── run.sh                     # docker run 명령
├── build.sh                   # 이미지 빌드 및 push
├── deploy.sh                  # macOS 배포 (rsync)
└── deploy.ps1                 # Windows 배포 (scp)
```

## 볼륨 마운트

| 컨테이너 경로 | 호스트 경로 | 용도 |
|---|---|---|
| `/app/instances.json` | `~/mariadb-mcp/instances.json` | 멀티 인스턴스 설정 (read-only) |
| `/app/logs` | `~/mariadb-mcp/logs/` | 서버 로그 |

## 인스턴스 구성

`instances.json`으로 여러 MariaDB 인스턴스를 관리한다. 각 MCP 도구 호출 시 `instance_name` 파라미터로 대상 인스턴스를 지정한다.

### 운영 인스턴스 (포트 9001 단일 컨테이너)

| instance_name | 대상 DB | 비고 |
|---------------|---------|------|
| raspberrypi | raspberrypi MariaDB | slave (read) |
| orangepi5plus | orangepi5plus MariaDB | master (write) |
| bflow-* | b-flow 관련 DB | bflow 서비스용 |
| RM8130N6Z64 | RM8130N6Z64 DB | 별도 서버 |

`instances.json` 존재 시 `DB_*` 환경 변수는 무시된다.

## 외부 접근 (Cloudflare Tunnel)

경로 기반으로 포트를 분리하여 단일 도메인에서 여러 인스턴스 그룹을 제공한다.

| URL | 포트 | 대상 인스턴스 |
|-----|------|--------------|
| `https://db.codescent.biz/codescent/sse` | 9001 | raspberrypi, orangepi5plus |
| `https://db.codescent.biz/brich/sse` | 9001 | b-flow-*, bflow-shoplinker |
| `https://db.codescent.biz/komid/sse` | 9001 | RM8130N6Z64 |

## 배포

```bash
# Windows
.\deploy.ps1

# macOS/Linux
./deploy.sh
```

배포 스크립트 실행 순서: 컨테이너 중지 → 파일 업로드 → `.env.production` → 원격 `.env` 복사 → `instances.json` 복사 → 이미지 pull → 컨테이너 시작
