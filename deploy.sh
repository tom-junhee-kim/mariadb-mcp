#!/bin/bash
# mariadb-mcp 배포 (macOS/Linux, rsync 기반)
# - 서버의 .env 와 instances.json 은 서버에만 둔다 — 자격증명이 든 운영 설정이라 배포가 올리지도 덮지도 지우지도
#   않는다. 둘 중 하나라도 없으면 아무것도 하지 않고 중단한다.
# - 소스 업로드와 이미지 빌드는 기존 컨테이너가 서비스하는 동안 한다. 빌드가 실패하면 운영은 그대로다.
#   순단은 전환(기존 정지 → 새 컨테이너 healthy) 구간뿐이다.
# - 전환은 rename 방식: 기존 컨테이너를 <NAME>-old 로 바꿔 정지 보관하고, 새 컨테이너가 healthy 가 되지 않거나
#   크래시 루프면 자동으로 되돌린다. -old 는 다음 배포에서 빌드가 성공한 뒤 로그를 백업하고 지운다.
# - 사용법: ./deploy.sh
#   MCP_DEPLOY_TARGET=test 이면 시험 대상(~/mariadb-mcp-test, 컨테이너 mariadb-mcp-test, 호스트 포트 19001,
#   이미지 codescent/mariadb-mcp:test)에 배포한다. 대상은 이 두 묶음만 허용한다.
#
# 정적분석 억제 사유(아래 disable 지시):
# - SC2029: ssh에 넘기는 명령 문자열 안의 변수는 로컬에서 확장되는 것이 의도다.
# - SC2088: REMOTE_DIR의 "~"는 로컬이 아니라 원격 셸(및 rsync·scp의 원격 경로 해석)이 확장한다.
# shellcheck disable=SC2029,SC2088
set -euo pipefail

HOST="DM300S3B-B33"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET="${MCP_DEPLOY_TARGET:-prod}"

# 0. 대상 결정 — 원격 정리(rsync --delete)가 빈 값·오타로 엉뚱한 디렉터리를 지우지 않게 고정 묶음만 허용한다.
case "$TARGET" in
  prod) NAME="mariadb-mcp"; PORT="9001"; REMOTE_DIR="~/mariadb-mcp"; IMAGE="codescent/mariadb-mcp:latest" ;;
  test) NAME="mariadb-mcp-test"; PORT="19001"; REMOTE_DIR="~/mariadb-mcp-test"; IMAGE="codescent/mariadb-mcp:test" ;;
  *) echo "ERROR: MCP_DEPLOY_TARGET 은 prod 또는 test 만 허용한다: '$TARGET'" >&2; exit 1 ;;
esac
OLD="${NAME}-old"
echo "==> 대상: $TARGET ($HOST:$REMOTE_DIR, $NAME, :$PORT, $IMAGE)"

# 서버 설정 확인 — run.sh 가 둘 다 쓴다. instances.json 이 없으면 docker 가 그 자리에 디렉터리를 만들어 기동이 깨진다.
if ! ssh "$HOST" "test -f $REMOTE_DIR/.env && test -f $REMOTE_DIR/instances.json"; then
  echo "ERROR: 서버 $REMOTE_DIR 에 .env 또는 instances.json 이 없습니다. 서버에서 직접 만든 뒤 다시 배포하세요." >&2
  exit 1
fi

# 1. 소스 업로드 — 기존 컨테이너는 계속 서비스한다
echo "==> Uploading files to $HOST:$REMOTE_DIR"
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
  --exclude='instances.json' \
  "$SCRIPT_DIR/" "$HOST:$REMOTE_DIR/"
ssh "$HOST" "mkdir -p $REMOTE_DIR/logs && chmod 755 $REMOTE_DIR/logs"

# 2. 이미지 빌드 — 운영 배포를 git 작업 트리에서 할 때만 커밋 태그를 함께 붙인다(시험은 떠도는 태그를 남기지 않는다)
TAGS="$IMAGE"
if [ "$TARGET" = "prod" ] && git -C "$SCRIPT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  SHA="$(git -C "$SCRIPT_DIR" rev-parse --short HEAD)"
  if [ -n "$(git -C "$SCRIPT_DIR" status --porcelain)" ]; then SHA="${SHA}-dirty"; fi
  TAGS="$TAGS codescent/mariadb-mcp:$SHA"
fi
echo "==> Building image ($TAGS)"
if ! ssh "$HOST" "bash $REMOTE_DIR/build.sh $TAGS"; then
  echo "ERROR: 이미지 빌드 실패 — 운영 컨테이너는 손대지 않았습니다." >&2
  exit 1
fi

# 3. 이전 세대 정리 — docker rm 은 회전 로그까지 지우므로 먼저 백업한다(서버 로그는 stderr 라 2>&1 필요).
#    판정은 docker logs 의 성공 여부다 — 로그 없이 죽은 컨테이너는 백업이 0바이트여도 정상이다.
echo "==> 이전 세대($OLD) 정리"
if ssh "$HOST" "docker inspect $OLD >/dev/null 2>&1"; then
  STAMP=$(date +%Y%m%d-%H%M%S)
  LOG_FILE="$REMOTE_DIR/logs/${NAME}-pre-transition-${STAMP}.log"
  if ! ssh "$HOST" "docker logs $OLD > $LOG_FILE 2>&1"; then
    echo "ERROR: $OLD 로그 백업 실패 — 지우지 않고 중단합니다(남아 있으면 이번 전환에서 이름이 충돌한다)." >&2
    exit 1
  fi
  SIZE=$(ssh "$HOST" "stat -c %s $LOG_FILE")
  ssh "$HOST" "docker rm $OLD"
  echo "  로그 백업(${SIZE} bytes) 후 제거 완료"
else
  echo "  이전 세대 없음"
fi

rollback() {
  # $OLD 가 포트를 쥔 채 정지돼 있으므로 새 컨테이너를 먼저 지운다. $OLD 가 없으면 되살릴 것이 없으니 새 컨테이너를 조사용으로 남긴다.
  ssh "$HOST" "docker logs --tail 100 $NAME" || true
  if ssh "$HOST" "docker inspect $OLD >/dev/null 2>&1"; then
    ssh "$HOST" "docker rm -f $NAME >/dev/null 2>&1 || true; docker rename $OLD $NAME && docker start $NAME"
    echo "롤백 완료: 이전 컨테이너가 다시 :$PORT 를 서비스합니다." >&2
  else
    echo "롤백 대상 $OLD 가 없습니다 — 새 컨테이너 $NAME 을 지우지 않고 남깁니다." >&2
  fi
  exit 1
}

# 4. 전환 — 여기부터 순단
echo "==> 전환: $NAME -> $OLD, 새 컨테이너 기동"
if ssh "$HOST" "docker inspect $NAME >/dev/null 2>&1"; then
  ssh "$HOST" "docker rename $NAME $OLD && docker stop -t 10 $OLD"
fi
if ! ssh "$HOST" "bash $REMOTE_DIR/run.sh $NAME $PORT $IMAGE"; then
  echo "ERROR: 새 컨테이너를 띄우지 못했습니다. 롤백합니다." >&2
  rollback
fi

# 5. healthy 대기 (최대 300초, 5초 간격). unless-stopped 라 죽어도 다시 뜨므로 Running 은 판정에 못 쓴다.
#    RestartCount 가 2를 넘으면 크래시 루프로 보고 바로 롤백한다.
echo "==> health 대기 (최대 300초)"
HEALTHY=0
for _ in $(seq 1 60); do
  sleep 5
  INFO=$(ssh "$HOST" "docker inspect --format '{{.State.Health.Status}} {{.RestartCount}}' $NAME 2>/dev/null" || true)
  STATUS="${INFO%% *}"
  RESTARTS="${INFO##* }"
  if [ "$STATUS" = "healthy" ]; then HEALTHY=1; break; fi
  case "$RESTARTS" in
    ''|*[!0-9]*) ;;
    *) if [ "$RESTARTS" -gt 2 ]; then echo "ERROR: 크래시 루프(RestartCount=$RESTARTS). 롤백합니다." >&2; rollback; fi ;;
  esac
done
if [ "$HEALTHY" -ne 1 ]; then
  echo "ERROR: 300초 안에 healthy 가 되지 않았습니다. 롤백합니다." >&2
  rollback
fi

echo "==> 전환 완료 (healthy)"
ssh "$HOST" "docker ps --filter name=^$NAME\$ --format '{{.Names}}  {{.Status}}  {{.Image}}'"
echo ""
echo "이전 컨테이너 $OLD 는 정지 상태로 남아 있다(처음 배포면 없다) — 다음 배포에서 빌드 성공 뒤 로그 백업 후 제거된다."
echo "지금 되돌리려면:"
echo "  ssh $HOST \"if docker inspect $OLD >/dev/null 2>&1; then docker rm -f $NAME && docker rename $OLD $NAME && docker start $NAME; else echo '백업 컨테이너 $OLD 가 없다 — 롤백할 수 없어 지우지 않는다'; fi\""
