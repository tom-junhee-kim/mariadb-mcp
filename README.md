# MariaDB MCP

MariaDB MCP 서버. instances.json으로 멀티 인스턴스를 단일 프로세스에서 관리한다.

## 서버 정보

- **배포 대상**: k3s 파드(DM300S3B-B33 노드 고정). 운영은 streamable-http(호스트 9002, 경로 `/mcp`), 시험은 SSE(호스트 19001, 평소 replicas 0)
- **이미지**: `mariadb-mcp:sha-<커밋 7자>` 를 서버 registry 에 올리고 매니페스트에 digest 까지 고정한다(커스텀 — python:3.11.15-slim + uv 0.11.31, 의존성은 `uv.lock` 고정)
- **환경값**: `.env` 값과 `instances.json` 은 SOPS Secret 이 정본이다(저장소·서버 파일이 아니다)
- **Docker 컨테이너는 쓰지 않는다**: 부팅 때 서버의 boot-order 서비스도 띄우지 않는다. `run.sh` 는 되돌림 전용(가드)이다

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

## 파드 마운트

| 컨테이너 경로 | 출처 | 모드 | 용도 |
|---|---|---|---|
| `/app/instances.json` | Secret | ro | 멀티 인스턴스 설정 |
| `/app/logs` | emptyDir | rw | 서버 로그 |
| `/tmp` | emptyDir | rw | 임시 |

## 환경 변수

서버의 `~/mariadb-mcp/.env` 가 정본이다 — 서버에서 직접 만들고 고친다(`chmod 600`). 배포 스크립트는 `.env` 와
`instances.json` 을 올리지도 덮지도 지우지도 않으며, 둘 중 하나라도 없으면 배포를 시작하지 않는다.
`.env.example` 참조. 주요 변수:

- `TZ` — 타임존 (Asia/Seoul)
- `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` — 단일 인스턴스 모드용 (instances.json 존재 시 무시)
- `MCP_READ_ONLY` — 읽기 전용 모드
- `EMBEDDING_PROVIDER` — 임베딩 프로바이더 (openai, gemini, huggingface)
- `LOG_LEVEL` / `LOG_FILE` — 로깅 설정

## 배포 (k3s)

```bash
# macOS/Linux — 시험 대상(기본)
bash ./deploy.sh
# 시험을 통과한 뒤 운영
MCP_DEPLOY_TARGET=http bash ./deploy.sh
```

`deploy.sh` 의 흐름: 작업 트리가 깨끗한지 확인(태그 `sha-<커밋 7자>`) → registry 에 같은 태그가 있으면 건너뛰고, 없으면 DM300(amd64)에 소스 업로드·빌드·push → 이미지 digest 읽기 → gitops 이미지 줄을 `태그@digest` 로 커밋(Flux 가 반영) → Flux 반영·rollout 확인.
내부 값(registry·namespace·Deployment 이름)은 저장소 밖 설정 파일(`MCP_DEPLOY_CONF`)에서 읽는다. `DRY_RUN=1` 은 서버·gitops 에 쓰지 않고 확인만 한다.

시험 절차: 시험 Deployment 는 평소 replicas 0 이라 `deploy.sh`(test)는 이미지만 바꾼다 → 배포 저장소에서 replicas 1 로 커밋 → `tests/test_atlas_docs_live.py` 를 19001 로 실행 → 통과하면 `MCP_DEPLOY_TARGET=http` → 시험을 replicas 0 으로 되돌리는 커밋.
롤백은 배포 저장소에서 이전 태그로 되돌리는 커밋이다(`kubectl scale`·`set image` 는 Flux 가 되돌린다). 옛 Docker 배포(`deploy.ps1`·`run.sh`)는 `ALLOW_DOCKER_RUN=1` 일 때만 돈다.

의존성 판을 올릴 때는 `pyproject.toml` 의 `[tool.uv] exclude-newer` 를 옮기고 `uv lock --upgrade` 로 `uv.lock` 을 갱신해 함께 커밋한다.

## 운영

### 상태 확인
```bash
kubectl -n <namespace> get pods -l app.kubernetes.io/name=mariadb-mcp-http
```

### 로그 확인
```bash
kubectl -n <namespace> logs deploy/mariadb-mcp-http
```
