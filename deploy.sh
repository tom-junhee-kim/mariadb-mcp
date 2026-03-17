#!/bin/bash
# macOS/Linux용 배포 스크립트 (rsync 기반)
# - .env.production → 서버의 .env로 복사
# - instances.json을 서버로 동기화 후 이미지 빌드 & 컨테이너 재시작
# - 사용법: ./deploy.sh
set -euo pipefail

HOST="DM300S3B-B33"
REMOTE_DIR="~/mariadb-mcp"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# .env.production 확인
if [ ! -f "$SCRIPT_DIR/.env.production" ]; then
  echo "ERROR: .env.production 파일이 없습니다. .env.example을 참고하여 생성하세요." >&2
  exit 1
fi

# 컨테이너 중지 (배포 중 설정 불일치 방지)
echo "==> Stopping mariadb-mcp container"
ssh "$HOST" "docker stop mariadb-mcp 2>/dev/null || true"

# 파일 동기화 (.env*, deploy 스크립트, .git*, README.md, Python/빌드 제외)
echo "==> Uploading files to $HOST:$REMOTE_DIR"
ssh "$HOST" "mkdir -p $REMOTE_DIR"
rsync -av --delete \
  --exclude='.env*' \
  --exclude='deploy.*' \
  --exclude='.git*' \
  --exclude='README.md' \
  --exclude='LICENSE' \
  --exclude='*.example.json' \
  --exclude='docker-compose.yml' \
  --exclude='.venv/' \
  --exclude='__pycache__/' \
  --exclude='logs/' \
  --exclude='src/tests/' \
  "$SCRIPT_DIR/" "$HOST:$REMOTE_DIR/"

# .env.production → 서버의 .env로 복사
echo "==> Deploying .env.production as .env"
scp "$SCRIPT_DIR/.env.production" "$HOST:$REMOTE_DIR/.env"
ssh "$HOST" "chmod 600 $REMOTE_DIR/.env"

# 로그 디렉토리 생성
echo "==> Ensuring logs directory"
ssh "$HOST" "mkdir -p $REMOTE_DIR/logs && chmod 755 $REMOTE_DIR/logs"

# 이미지 빌드
echo "==> Building mariadb-mcp image"
ssh "$HOST" "bash $REMOTE_DIR/build.sh"

# 컨테이너 재시작 (기존 컨테이너 제거 후 run.sh 실행)
echo "==> Restarting mariadb-mcp container"
ssh "$HOST" "docker rm mariadb-mcp 2>/dev/null || true && bash $REMOTE_DIR/run.sh"

# 기동 확인
echo "==> Waiting for startup..."
sleep 5
ssh "$HOST" "docker ps --filter name=mariadb-mcp --format '{{.Status}}'"
echo "Done."
