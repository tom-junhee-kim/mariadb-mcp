#!/bin/bash
set -euo pipefail
# mariadb-mcp 이미지 빌드 (서버에서 실행)
# - 이 스크립트가 있는 디렉터리를 빌드 컨텍스트로 쓴다
# - 사용법: build.sh [IMAGE ...]  받은 태그 전부를 붙인다 (기본: codescent/mariadb-mcp:latest)
# - deploy.sh/deploy.ps1이 이 스크립트를 원격 호출함
DIR="$(cd "$(dirname "$0")" && pwd)"
TAGS=("$@")
if [ ${#TAGS[@]} -eq 0 ]; then TAGS=("codescent/mariadb-mcp:latest"); fi
ARGS=()
for t in "${TAGS[@]}"; do ARGS+=(-t "$t"); done
# BUILD_NETWORK(선택): 빌드 컨테이너 네트워크(예 host). 서버의 Docker 기본 DNS 가 컨테이너에서 안 닿을 때의 임시 우회용 — 비우면 기본 동작
NET=(); if [ -n "${BUILD_NETWORK:-}" ]; then NET=(--network "$BUILD_NETWORK"); fi
docker build ${NET[@]+"${NET[@]}"} "${ARGS[@]}" "$DIR"
