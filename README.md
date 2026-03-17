# MariaDB MCP

MariaDB MCP 서버. instances.json으로 멀티 인스턴스를 단일 컨테이너에서 관리한다.

## 서버 정보

- **배포 대상**: DM300S3B-B33 (192.168.2.14)
- **이미지**: `codescent/mariadb-mcp:latest` (커스텀 — python:3.11-slim + uv)
- **컨테이너**: `mariadb-mcp`
- **네트워크**: bridge (9001)
- **재시작 정책**: unless-stopped

## 파일 구조

```
mariadb-mcp/
├── src/                        # MCP 서버 소스
├── .dockerignore
├── .env.example
├── .gitattributes
├── .gitignore
├── build.sh
├── deploy.ps1
├── deploy.sh
├── Dockerfile                  # 멀티 스테이지 빌드 (builder + runtime)
├── instances.example.json      # 멀티 인스턴스 설정 템플릿
├── pyproject.toml
├── README.md
└── run.sh
```

## 볼륨 마운트

| 컨테이너 경로 | 호스트 경로 | 모드 | 용도 |
|---|---|---|---|
| `/app/instances.json` | `~/mariadb-mcp/instances.json` | ro | 멀티 인스턴스 설정 |
| `/app/logs` | `~/mariadb-mcp/logs` | rw | 서버 로그 |

## 환경 변수

`.env.example` 참조. 주요 변수:

- `TZ` — 타임존 (Asia/Seoul)
- `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` — 단일 인스턴스 모드용 (instances.json 존재 시 무시)
- `MCP_READ_ONLY` — 읽기 전용 모드
- `EMBEDDING_PROVIDER` — 임베딩 프로바이더 (openai, gemini, huggingface)
- `LOG_LEVEL` / `LOG_FILE` — 로깅 설정

## 배포

```bash
# macOS/Linux
./deploy.sh

# Windows
.\deploy.ps1
```

배포 흐름:
1. 파일 동기화 (rsync / scp)
2. `.env.production` → 서버의 `.env`로 복사
3. 로그 디렉토리 생성
4. 이미지 빌드 → 컨테이너 재시작

## 운영

### 상태 확인
```bash
ssh DM300S3B-B33 "docker ps --filter name=mariadb-mcp"
```

### 로그 확인
```bash
ssh DM300S3B-B33 "docker logs mariadb-mcp"
```
