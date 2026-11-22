#!/bin/bash
# mariadb-mcp 컨테이너 실행
# - 서버(DM300S3B-B33)의 ~/mariadb-mcp/ 에서 실행. .env·instances.json·logs 는 이 스크립트가 있는 디렉터리 것을 쓴다
# - instances.json으로 멀티 인스턴스 관리 (단일 컨테이너)
# - k3s 로 이전했다 — 이 스크립트는 되돌림(파드를 내린 뒤)에만 ALLOW_DOCKER_RUN=1 로 직접 실행한다. 배포는 deploy.sh
# - 사용법: run.sh [NAME] [HOST_PORT] [IMAGE] [컨테이너 명령 인자…]  (기본: mariadb-mcp 9001 codescent/mariadb-mcp:latest, 명령은 이미지 기본값)
#   바꾸는 것은 호스트 쪽 포트뿐이다 — 컨테이너 안 9001 과 healthcheck 는 그대로다
#   IMAGE 뒤 인자는 컨테이너 명령으로 그대로 넘어간다(예: python src/server.py --transport http …)
#   MCP_EXTRA_ENV="KEY=VAL KEY2=VAL2" 로 -e 를 더한다(값에 공백 불가)
set -euo pipefail
# ===== 이 서비스는 k3s 로 이전했다 — 이 스크립트로 Docker 컨테이너를 만들지 않는다. =====
# 옛 Docker 컨테이너와 k3s 파드가 함께 돌면 포트·상태가 중복된다. 되돌림(파드 replicas 0 확인 뒤)에만 ALLOW_DOCKER_RUN=1 로 실행한다.
if [ "${ALLOW_DOCKER_RUN:-}" != "1" ]; then
  echo "ERROR: mariadb-mcp 는 k3s 로 이전했다 — 이 스크립트는 Docker 컨테이너를 만든다. 배포는 deploy.sh, 되돌림은 운영 문서를 보라." >&2
  exit 1
fi
DIR="$(cd "$(dirname "$0")" && pwd)"
NAME="${1:-mariadb-mcp}"
PORT="${2:-9001}"
IMAGE="${3:-codescent/mariadb-mcp:latest}"
if [ "$#" -gt 3 ]; then shift 3; else shift "$#"; fi
EXTRA_ENV=()
for kv in ${MCP_EXTRA_ENV:-}; do EXTRA_ENV+=(-e "$kv"); done

docker run -d \
  --name "$NAME" \
  -h "$NAME" \
  --restart on-failure \
  --add-host=host.docker.internal:host-gateway \
  --env-file "$DIR/.env" \
  --log-opt max-size=10m \
  --log-opt max-file=3 \
  -v "$DIR/instances.json:/app/instances.json:ro" \
  -v "$DIR/logs:/app/logs" \
  -p "$PORT:9001" \
  --health-cmd='bash -c "echo > /dev/tcp/localhost/9001" || exit 1' \
  --health-interval=30s \
  --health-timeout=10s \
  --health-retries=3 \
  --health-start-period=15s \
  ${EXTRA_ENV[@]+"${EXTRA_ENV[@]}"} \
  "$IMAGE" "$@"
