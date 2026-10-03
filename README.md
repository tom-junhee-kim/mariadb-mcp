# MariaDB MCP

MariaDB MCP 서버. instances.json으로 멀티 인스턴스를 단일 컨테이너에서 관리한다.

## 서버 정보

- **배포 대상**: DM300S3B-B33 (192.168.2.14)
- **이미지**: `codescent/mariadb-mcp:latest` + 커밋 태그 `codescent/mariadb-mcp:<sha>` (커스텀 — python:3.11.15-slim + uv 0.11.31, 의존성은 `uv.lock` 고정)
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
├── .python-version
├── build.sh
├── deploy.ps1
├── deploy.sh
├── Dockerfile                  # 멀티 스테이지 빌드 (builder + runtime)
├── instances.example.json      # 멀티 인스턴스 설정 템플릿
├── pyproject.toml
├── README.md
├── run.sh
└── uv.lock                     # 의존성 잠금 — 재빌드가 같은 판을 설치한다
```

## 볼륨 마운트

| 컨테이너 경로 | 호스트 경로 | 모드 | 용도 |
|---|---|---|---|
| `/app/instances.json` | `~/mariadb-mcp/instances.json` | ro | 멀티 인스턴스 설정 |
| `/app/logs` | `~/mariadb-mcp/logs` | rw | 서버 로그 |

## 환경 변수

서버의 `~/mariadb-mcp/.env` 가 정본이다 — 서버에서 직접 만들고 고친다(`chmod 600`). 배포 스크립트는 `.env` 와
`instances.json` 을 올리지도 덮지도 지우지도 않으며, 둘 중 하나라도 없으면 배포를 시작하지 않는다.
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
1. 서버 `.env`·`instances.json` 확인 (없으면 중단)
2. 소스 동기화 (rsync / scp) — 기존 컨테이너는 계속 서비스한다
3. 서버에서 이미지 빌드 — 실패하면 여기서 중단하고 운영은 그대로다
4. 이전 세대 `mariadb-mcp-old` 가 있으면 로그를 `logs/` 에 백업하고 제거
5. 전환: 운영 컨테이너를 `mariadb-mcp-old` 로 바꿔 정지 → 새 컨테이너 기동 (순단은 여기서 healthy 까지)
6. healthy 대기(최대 300초) — 실패하거나 크래시 루프면 `mariadb-mcp-old` 로 자동 롤백

`mariadb-mcp-old` 는 다음 배포까지 정지 상태로 남는다. 배포가 끝나면 되돌리는 명령을 출력한다.
`MCP_DEPLOY_TARGET=test` 로 실행하면 시험 대상(`~/mariadb-mcp-test`, 컨테이너 `mariadb-mcp-test`, 호스트 포트 19001)에 배포한다.

의존성 판을 올릴 때는 `pyproject.toml` 의 `[tool.uv] exclude-newer` 를 옮기고 `uv lock --upgrade` 로 `uv.lock` 을 갱신해 함께 커밋한다.

## 운영

### 상태 확인
```bash
ssh DM300S3B-B33 "docker ps --filter name=mariadb-mcp"
```

### 로그 확인
```bash
ssh DM300S3B-B33 "docker logs mariadb-mcp"
```
