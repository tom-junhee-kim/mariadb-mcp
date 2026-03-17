#!/bin/bash
# mariadb-mcp 컨테이너 실행
# - 서버(DM300S3B-B33)의 ~/mariadb-mcp/ 에서 실행
# - instances.json으로 멀티 인스턴스 관리 (단일 컨테이너)
# - deploy.sh 또는 deploy.ps1이 이 스크립트를 원격 호출함
docker run -d \
  --name mariadb-mcp \
  -h mariadb-mcp \
  --restart unless-stopped \
  --add-host=host.docker.internal:host-gateway \
  --env-file ~/mariadb-mcp/.env \
  --log-opt max-size=10m \
  --log-opt max-file=3 \
  -v ~/mariadb-mcp/instances.json:/app/instances.json:ro \
  -v ~/mariadb-mcp/logs:/app/logs \
  -p 9001:9001 \
  --health-cmd='bash -c "echo > /dev/tcp/localhost/9001" || exit 1' \
  --health-interval=30s \
  --health-timeout=10s \
  --health-retries=3 \
  --health-start-period=15s \
  codescent/mariadb-mcp:latest
