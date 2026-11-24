#!/bin/bash
# mariadb-mcp 배포 (macOS/Linux) — DM300(amd64)에서 빌드 → registry 에 핀 태그 push → gitops 이미지 태그 커밋(bump.sh) → Flux 가 반영 → rollout 확인
# - k3s 로 이전했다: 이 스크립트는 Docker 컨테이너를 만들지 않는다. 환경값(.env·instances.json)은 SOPS Secret 이 정본이라 서버 파일을 쓰지 않는다.
# - 사용법: [MCP_DEPLOY_TARGET=test|http] ./deploy.sh      기본 test
#     test = 시험 Deployment(평소 replicas 0), http = 운영 streamable-http. 운영 배포 전에 시험으로 먼저 확인한다.
#     DRY_RUN=1 이면 서버·gitops 에 아무것도 쓰지 않고 가능한 데까지 확인만 한다(registry 에 태그가 있으면 digest 와 바뀔 줄까지 본다).
# - 태그는 sha-<커밋 7자>. 작업 트리가 깨끗해야 하고(태그가 내용과 어긋나지 않게), http 는 커밋이 원격에 올라가 있어야 한다.
#   registry 에 같은 태그가 이미 있으면 빌드·push 를 건너뛰고 그 이미지를 쓴다(태그 불변).
# - 내부 값(registry·namespace·Deployment 이름)은 저장소 밖 설정 파일에서 읽는다: MCP_DEPLOY_CONF (기본 <이 저장소 옆 폴더>/codescent-claude-workspace/tools/gitops-image/mariadb-mcp.conf, CS_WORKSPACE 로 바꿀 수 있다).
# - 필요: ssh DM300S3B-B33(registry 로그인 있음), $CS_WORKSPACE/tools/gitops-image/bump.sh, kubectl + kubeconfig(KUBECONFIG 또는 설정 파일의 KCFG_DEFAULT — 없으면 rollout 확인만 건너뜀)
# - 롤백: 배포 저장소(gitops)에서 이전 태그로 되돌리는 커밋. kubectl scale·set image 는 Flux 가 되돌린다.
# 정적분석 억제: SC2029 ssh 에 넘기는 문자열의 변수는 로컬에서 펼치는 것이 의도, SC2088 REMOTE_DIR 의 ~ 는 원격 셸·rsync 가 펼친다.
# shellcheck disable=SC2029,SC2088
set -euo pipefail

HOST="DM300S3B-B33"
REMOTE_DIR="~/mariadb-mcp"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"   # -P: 워크스페이스 docker/ 심링크로 불러도 실체 경로(저장소 옆 폴더 기준)가 나온다
TARGET="${MCP_DEPLOY_TARGET:-test}"
DRY="${DRY_RUN:-0}"
WS="${CS_WORKSPACE:-$(cd "$SCRIPT_DIR/.." && pwd)/codescent-claude-workspace}"
CONF="${MCP_DEPLOY_CONF:-$WS/tools/gitops-image/mariadb-mcp.conf}"
BUMP="${GITOPS_BUMP:-$WS/tools/gitops-image/bump.sh}"

case "$TARGET" in
  test|http) ;;
  *) echo "ERROR: MCP_DEPLOY_TARGET 은 test·http 만 허용한다: '$TARGET'" >&2; exit 1 ;;
esac
[ -f "$CONF" ] || { echo "ERROR: 설정 파일이 없다($CONF) — MCP_DEPLOY_CONF 로 경로를 준다. 아무것도 바꾸지 않았다." >&2; exit 1; }
# shellcheck source=/dev/null
. "$CONF"
: "${REG:?설정 파일에 REG 가 없다}" "${NS:?설정 파일에 NS 가 없다}"
NS_VAR="NS_${TARGET}"; NS="${!NS_VAR:-$NS}"   # 대상마다 칸이 다르면 설정 파일의 NS_<대상> 이 NS 보다 앞선다
DEPLOY_VAR="DEPLOY_${TARGET}"
DEPLOY_NAME="${!DEPLOY_VAR:?설정 파일에 $DEPLOY_VAR 가 없다}"
KCFG="${KUBECONFIG:-${KCFG_DEFAULT:-}}"
[ -x "$BUMP" ] || { echo "ERROR: bump.sh 를 찾지 못했다($BUMP) — GITOPS_BUMP 로 경로를 준다." >&2; exit 1; }

if [ -n "$(git -C "$SCRIPT_DIR" status --porcelain)" ]; then
  echo "ERROR: 작업 트리에 커밋되지 않은 변경이 있다 — 태그(sha-<커밋>)가 내용과 어긋나므로 커밋한 뒤 다시 한다." >&2
  exit 1
fi
TAG="sha-$(git -C "$SCRIPT_DIR" rev-parse --short=7 HEAD)"
if [ "$TARGET" = "http" ] && [ -z "$(git -C "$SCRIPT_DIR" branch -r --contains HEAD 2>/dev/null)" ]; then
  echo "ERROR: 운영(http) 배포는 원격에 올라간 커밋만 한다 — 먼저 push 한다." >&2
  exit 1
fi
IMAGE="$REG/mariadb-mcp:$TAG"
echo "==> 대상: $TARGET ($NS/$DEPLOY_NAME), 이미지 $IMAGE"

ssh "$HOST" true || { echo "ERROR: $HOST 에 ssh 로 닿지 않는다 — 아무것도 바꾸지 않았다." >&2; exit 1; }
if ssh "$HOST" "docker manifest inspect $IMAGE >/dev/null 2>&1"; then
  IN_REGISTRY=1
  echo "==> registry 에 $TAG 가 이미 있다 — 빌드·push 를 건너뛴다"
else
  IN_REGISTRY=0
fi

if [ "$IN_REGISTRY" = 0 ]; then
  if [ "$DRY" = 1 ]; then
    echo "==> DRY_RUN — registry 에 $TAG 가 없다: 실제로는 소스 업로드→빌드→push→bump 를 한다. 여기서 멈춘다."
    exit 0
  fi
  echo "==> 소스 업로드 $HOST:$REMOTE_DIR (서버의 .env·instances.json·logs 는 건드리지 않는다)"
  rsync -a --delete \
    --exclude='.env*' --exclude='deploy.*' --exclude='.git*' --exclude='README.md' --exclude='LICENSE' \
    --exclude='*.example.json' --exclude='docker-compose.yml' --exclude='.venv/' --exclude='__pycache__/' \
    --exclude='logs/' --exclude='src/tests/' --exclude='instances.json' \
    "$SCRIPT_DIR/" "$HOST:$REMOTE_DIR/"
  echo "==> 이미지 빌드 $IMAGE (실패하면 여기서 멈춘다 — 운영은 그대로)"
  ssh "$HOST" "bash $REMOTE_DIR/build.sh $IMAGE" || { echo "ERROR: 이미지 빌드 실패 — 아무것도 반영하지 않았다." >&2; exit 1; }
  echo "==> push $IMAGE (큰 새 레이어가 413 으로 거부되면 DM300 의 registry 우회 설정을 확인한다)"
  ssh "$HOST" "docker push -q $IMAGE" || { echo "ERROR: push 실패 — 아무것도 반영하지 않았다." >&2; exit 1; }
else
  ssh "$HOST" "docker pull -q $IMAGE >/dev/null"
fi

DIGEST="$(ssh "$HOST" "docker image inspect $IMAGE --format '{{range .RepoDigests}}{{println .}}{{end}}'" | grep "^$REG/mariadb-mcp@sha256:" | head -1 | sed 's/.*@//')"
[[ "$DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "ERROR: 이미지 digest 를 읽지 못했다('$DIGEST') — 아무것도 반영하지 않았다." >&2; exit 1; }
PIN="$TAG@$DIGEST"

echo "==> gitops 이미지 태그 반영: mariadb-mcp-$TARGET $PIN"
"$BUMP" "mariadb-mcp-$TARGET" "$PIN"
if [ "$DRY" = 1 ]; then echo "==> DRY_RUN — rollout 확인은 건너뛴다."; exit 0; fi

if ! { command -v kubectl >/dev/null 2>&1 && [ -n "$KCFG" ] && [ -f "$KCFG" ]; }; then
  echo "(kubectl·kubeconfig 가 없어 rollout 확인은 건너뜀 — kubectl -n $NS get pods 로 본다)"
  exit 0
fi
K=(kubectl --kubeconfig "$KCFG" -n "$NS")
echo "==> Flux 반영 대기(최대 10분)"
APPLIED=0
for _ in $(seq 1 60); do
  cur="$("${K[@]}" get deploy "$DEPLOY_NAME" -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)"
  if [ "$cur" = "$IMAGE@$DIGEST" ]; then APPLIED=1; break; fi
  sleep 10
done
[ "$APPLIED" = 1 ] || { echo "ERROR: 10분 안에 Flux 가 $DEPLOY_NAME 에 반영하지 않았다 — kubectl -n $NS get deploy $DEPLOY_NAME 로 본다." >&2; exit 1; }
REPLICAS="$("${K[@]}" get deploy "$DEPLOY_NAME" -o jsonpath='{.spec.replicas}')"
if [ "$REPLICAS" = 0 ]; then
  echo "==> 이미지만 바꿨다($DEPLOY_NAME replicas 0). 시험하려면:"
  echo "    1) 배포 저장소에서 $DEPLOY_NAME 을 replicas 1 로 커밋 → Flux 반영 → 파드 Ready"
  echo "    2) tests/test_atlas_docs_live.py 를 시험 주소(:19001)로 실행(ATLAS_TEST_TOKEN 필요)"
  echo "    3) 통과하면 MCP_DEPLOY_TARGET=http ./deploy.sh → 끝나면 시험을 replicas 0 으로 되돌리는 커밋"
  exit 0
fi
"${K[@]}" rollout status "deploy/$DEPLOY_NAME" --timeout=300s
echo "==> 완료: $DEPLOY_NAME 이 $PIN 로 동작한다."
if [ "$TARGET" = "test" ]; then echo "    시험이 끝나면 배포 저장소에서 replicas 0 으로 되돌리는 커밋을 한다."; fi
