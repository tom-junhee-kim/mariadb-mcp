#!/bin/bash
# mariadb-mcp 이미지 빌드 (서버에서 실행)
# - Dockerfile로부터 커스텀 이미지 생성
# - deploy.sh/deploy.ps1이 이 스크립트를 원격 호출함
cd ~/mariadb-mcp && docker build -t codescent/mariadb-mcp:latest .
