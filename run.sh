#!/bin/bash
# mariadb-mcp 컨테이너 실행 (3개 인스턴스 그룹)
# - 서버(DM300S3B-B33)의 ~/mariadb-mcp/ 에서 실행
# - instances-*.json을 볼륨 마운트로 주입 (컨테이너 내부 /app/instances.json)
# - 컨테이너 내부 포트는 항상 9001, 호스트 포트만 다름
# - deploy.sh 또는 deploy.ps1이 이 스크립트를 원격 호출함
#
# 포트 매핑:
#   9001: raspberrypi + orangepi5plus (기본)
#   9002: b-flow-* + bflow-*
#   9003: rm8130n6z64

DIR=~/mariadb-mcp

for entry in "mariadb-mcp-main:9001:instances-9001.json" \
             "mariadb-mcp-bflow:9002:instances-9002.json" \
             "mariadb-mcp-rm8130n6z64:9003:instances-9003.json"; do
  IFS=: read -r name port instances <<< "$entry"
  docker run -d \
    --name "$name" \
    -h "$name" \
    --restart always \
    --env-file "$DIR/.env" \
    -v "$DIR/$instances:/app/instances.json:ro" \
    -p "$port:9001" \
    codescent/mariadb-mcp:latest
done
