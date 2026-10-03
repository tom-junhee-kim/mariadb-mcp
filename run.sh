#!/bin/bash
# mariadb-mcp 컨테이너 실행
# - 서버(DM300S3B-B33)의 ~/mariadb-mcp/ 에서 실행. .env·instances.json·logs 는 이 스크립트가 있는 디렉터리 것을 쓴다
# - instances.json으로 멀티 인스턴스 관리 (단일 컨테이너)
# - deploy.sh 또는 deploy.ps1이 이 스크립트를 원격 호출함
# - 사용법: run.sh [NAME] [HOST_PORT] [IMAGE]  (기본: mariadb-mcp 9001 codescent/mariadb-mcp:latest)
#   바꾸는 것은 호스트 쪽 포트뿐이다 — 컨테이너 안 9001 과 healthcheck 는 그대로다
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
NAME="${1:-mariadb-mcp}"
PORT="${2:-9001}"
IMAGE="${3:-codescent/mariadb-mcp:latest}"

docker run -d \
  --name "$NAME" \
  -h "$NAME" \
  --restart unless-stopped \
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
  "$IMAGE"
