#!/bin/bash
# macOS용 배포 스크립트 (rsync 기반)
# - .env.production → 서버의 .env로 복사
# - 로컬 mariadb-mcp/ 파일을 서버로 동기화 후 이미지 빌드 & 컨테이너 재시작
# - 사용법: ./deploy.sh
set -euo pipefail

HOST="DM300S3B-B33-jhcheong"
REMOTE_DIR="~/mariadb-mcp"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 기존 8개 컨테이너 정리 (최초 배포 시에만 필요, 이후에는 무해)
echo "==> Stopping old containers"
ssh "$HOST" 'for c in mariadb-mcp-raspberrypi mariadb-mcp-orangepi5plus mariadb-mcp-RM8130N6Z64 mariadb-mcp-rm8130n6z64 mariadb-mcp-b-flow-new-temp mariadb-mcp-b-flow-standalone mariadb-mcp-b-flow-middleware-auth mariadb-mcp-b-flow-push mariadb-mcp-bflow-shoplinker; do docker stop $c 2>/dev/null; docker rm $c 2>/dev/null; done || true'

# 새 컨테이너 정리
echo "==> Stopping current containers"
ssh "$HOST" 'for c in mariadb-mcp-main mariadb-mcp-bflow mariadb-mcp-RM8130N6Z64 mariadb-mcp-rm8130n6z64; do docker stop $c 2>/dev/null; docker rm $c 2>/dev/null; done || true'

# 파일 동기화
echo "==> Uploading files to $HOST:$REMOTE_DIR"
ssh "$HOST" "mkdir -p $REMOTE_DIR"
rsync -av --delete \
  --exclude='.env*' \
  --exclude='instances-*.json' \
  --exclude='deploy.*' \
  --exclude='.gitignore' \
  --exclude='.gitattributes' \
  --exclude='*.example.json' \
  --exclude='README.md' \
  --exclude='LICENSE' \
  --exclude='docker-compose.yml' \
  --exclude='.venv/' \
  --exclude='__pycache__/' \
  --exclude='logs/' \
  --exclude='src/tests/' \
  "$SCRIPT_DIR/" "$HOST:$REMOTE_DIR/"

# .env.production → 서버의 .env로 복사
echo "==> Deploying .env.production as .env"
if [ -f "$SCRIPT_DIR/.env.production" ]; then
    scp "$SCRIPT_DIR/.env.production" "$HOST:$REMOTE_DIR/.env"
    ssh "$HOST" "chmod 600 $REMOTE_DIR/.env"
else
    echo "ERROR: .env.production not found. Create it from .env.example first." >&2
    exit 1
fi

# 이미지 빌드
echo "==> Building Docker image on remote"
ssh "$HOST" "cd $REMOTE_DIR && bash build.sh"

# 컨테이너 시작
echo "==> Starting mariadb-mcp containers"
ssh "$HOST" "bash $REMOTE_DIR/run.sh"

# 기동 확인
echo "==> Waiting for startup..."
sleep 5
ssh "$HOST" "docker ps --filter name=mariadb-mcp --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'"
echo "Done."
