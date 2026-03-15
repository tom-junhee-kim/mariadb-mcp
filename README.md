# MariaDB MCP

MariaDB MCP 서버 (Claude Code DB 연동). 멀티 인스턴스를 단일 이미지로 관리하며, 인스턴스 그룹별로 3개 컨테이너를 실행한다.

## 서버 정보

- **배포 대상**: DM300S3B-B33-jhcheong (192.168.2.14)
- **이미지**: `codescent/mariadb-mcp:latest` (커스텀)
- **컨테이너**: `mariadb-mcp-main` (9001), `mariadb-mcp-bflow` (9002), `mariadb-mcp-RM8130N6Z64` (9003)
- **네트워크**: bridge (9001, 9002, 9003)
- **재시작 정책**: always

## 파일 구조

```
mariadb-mcp/
├── src/
│   ├── __init__.py
│   ├── server.py              # MCP 서버 메인
│   ├── main.py
│   ├── config.py              # 환경 변수 로딩 + 로깅
│   ├── custom_connection.py   # SafeConnection/SafePool (SQL 인젝션 방지)
│   ├── embeddings.py          # 임베딩 서비스
│   └── tests/                 # 테스트 (배포 제외)
├── .env.example               # 환경 변수 템플릿
├── .env.production            # 운영 환경 변수 (gitignore)
├── Dockerfile                 # 멀티 스테이지 빌드 (python:3.11-slim + uv)
├── instances.example.json     # 멀티 인스턴스 설정 템플릿
├── instances.json             # 실제 인스턴스 설정 (gitignore)
├── logs/                      # 서버 로그
├── pyproject.toml             # 프로젝트 메타데이터 (uv)
├── run.sh                     # docker run 명령 (3개 컨테이너)
├── build.sh                   # 이미지 빌드 (docker build)
├── deploy.sh                  # macOS 배포 (rsync)
├── deploy.ps1                 # Windows 배포 (scp)
└── README.md
```

## 볼륨 마운트

| 컨테이너 경로 | 호스트 경로 | 모드 | 용도 |
|---|---|---|---|
| `/app/instances.json` | `~/mariadb-mcp/instances-{port}.json` | ro | 인스턴스 그룹별 설정 |
| `/app/logs` | `~/mariadb-mcp/logs/` | rw | 서버 로그 |

## 환경 변수

`.env.example` 참조. 주요 변수:

- `TZ` -- 타임존 (Asia/Seoul)
- `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` -- 단일 인스턴스 모드용 (instances.json 존재 시 무시)
- `MCP_READ_ONLY` -- 읽기 전용 모드
- `MCP_MAX_POOL_SIZE` -- 커넥션 풀 크기
- `EMBEDDING_PROVIDER` -- 임베딩 프로바이더 (openai, gemini, huggingface)
- `LOG_LEVEL` / `LOG_FILE` -- 로깅 설정

## 인스턴스 구성

`instances-{port}.json`으로 인스턴스 그룹별 MariaDB 인스턴스를 관리한다. 각 MCP 도구 호출 시 `instance_name` 파라미터로 대상 인스턴스를 지정한다.

| 컨테이너 | 호스트 포트 | 인스턴스 파일 | 대상 DB |
|-----------|------------|---------------|---------|
| `mariadb-mcp-main` | 9001 | `instances-9001.json` | raspberrypi, orangepi5plus |
| `mariadb-mcp-bflow` | 9002 | `instances-9002.json` | b-flow-*, bflow-* |
| `mariadb-mcp-RM8130N6Z64` | 9003 | `instances-9003.json` | RM8130N6Z64 |

## 외부 접근 (Cloudflare Tunnel)

| URL | 포트 | 대상 인스턴스 |
|-----|------|--------------|
| `https://db.codescent.biz/codescent/sse` | 9001 | raspberrypi, orangepi5plus |
| `https://db.codescent.biz/brich/sse` | 9002 | b-flow-*, bflow-shoplinker |
| `https://db.codescent.biz/komid/sse` | 9003 | RM8130N6Z64 |

## 배포

```bash
# macOS
./deploy.sh

# Windows
.\deploy.ps1
```

배포 스크립트 실행 순서: 컨테이너 중지 → 파일 업로드 → `.env.production` → 원격 `.env` 복사 → 이미지 빌드 → 컨테이너 시작 (3개)

## 운영

### 상태 확인
```bash
ssh DM300S3B-B33-jhcheong "docker ps --filter name=mariadb-mcp"
```

### 로그 확인
```bash
ssh DM300S3B-B33-jhcheong "docker logs mariadb-mcp-main"
ssh DM300S3B-B33-jhcheong "docker logs mariadb-mcp-bflow"
ssh DM300S3B-B33-jhcheong "docker logs mariadb-mcp-RM8130N6Z64"
```
