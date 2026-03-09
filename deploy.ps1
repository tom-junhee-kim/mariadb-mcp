# Windows용 배포 스크립트 (scp 기반, rsync 없는 환경)
# - 로컬 mariadb-mcp/ 파일을 서버로 업로드 후 이미지 빌드 & 컨테이너 재시작
# - 사용법: .\deploy.ps1
$ErrorActionPreference = "Stop"

$Host_ = "DM300S3B-B33-jhcheong"
$RemoteDir = "~/mariadb-mcp"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
# 배포 대상 파일 목록
# .env, instances-*.json은 서버에서 직접 관리 (비밀번호 포함)
$Files = @(".gitattributes", "Dockerfile", ".dockerignore", "build.sh", "run.sh", "pyproject.toml", ".python-version", "uv.lock")
# 배포 대상 디렉토리 목록
$Dirs = @("src")
# 제외할 src 하위 디렉토리
$ExcludeSrcDirs = @("tests")

# 기존 8개 컨테이너 정리 (최초 배포 시에만 필요, 이후에는 무해)
Write-Host "==> Stopping old containers"
$OldContainers = @(
    "mariadb-mcp-raspberrypi", "mariadb-mcp-orangepi5plus",
    "mariadb-mcp-RM8130N6Z64", "mariadb-mcp-b-flow-new-temp",
    "mariadb-mcp-b-flow-standalone", "mariadb-mcp-b-flow-middleware-auth",
    "mariadb-mcp-b-flow-push", "mariadb-mcp-bflow-shoplinker"
)
$OldContainersStr = $OldContainers -join " "
ssh $Host_ "for c in $OldContainersStr; do docker stop `$c 2>/dev/null; docker rm `$c 2>/dev/null; done"

# 새 컨테이너 정리
Write-Host "==> Stopping current containers"
ssh $Host_ "for c in mariadb-mcp-main mariadb-mcp-bflow mariadb-mcp-RM8130N6Z64 mariadb-mcp-rm8130n6z64; do docker stop `$c 2>/dev/null; docker rm `$c 2>/dev/null; done"

# 파일 업로드
Write-Host "==> Uploading files to ${Host_}:${RemoteDir}"
ssh $Host_ "mkdir -p $RemoteDir"
foreach ($f in $Files) {
    $local = Join-Path $ScriptDir $f
    if (Test-Path $local) {
        scp $local "${Host_}:${RemoteDir}/$f"
        Write-Host "  $f"
    }
}
# 디렉토리 업로드
foreach ($d in $Dirs) {
    $local = Join-Path $ScriptDir $d
    if (Test-Path $local) {
        ssh $Host_ "mkdir -p $RemoteDir/$d"
        # src/ 내에서 tests/ 제외하고 업로드
        $items = Get-ChildItem -Path $local -Recurse -File | Where-Object {
            $rel = $_.FullName.Substring($local.Length + 1)
            $exclude = $false
            foreach ($ex in $ExcludeSrcDirs) {
                if ($rel -like "$ex\*" -or $rel -like "$ex/*") { $exclude = $true; break }
            }
            -not $exclude
        }
        foreach ($item in $items) {
            $rel = $item.FullName.Substring($local.Length + 1).Replace("\", "/")
            $remoteSubDir = Split-Path $rel -Parent
            if ($remoteSubDir) {
                ssh $Host_ "mkdir -p $RemoteDir/$d/$remoteSubDir"
            }
            scp $item.FullName "${Host_}:${RemoteDir}/$d/$rel"
            Write-Host "  $d/$rel"
        }
    }
}

# Windows에서 scp한 파일은 CRLF 줄바꿈 → LF로 변환
Write-Host "==> Converting CRLF to LF on remote"
ssh $Host_ "cd $RemoteDir && find . -name '*.sh' -o -name '*.py' -o -name 'Dockerfile' -o -name '.dockerignore' | xargs -r sed -i 's/\r$//'"

# 배포 대상 외 잔여 파일 정리 — 보존: .env, instances-*.json, .gitattributes, Dockerfile, .dockerignore, *.sh, src/, pyproject.toml, .python-version, uv.lock, logs/
Write-Host "==> Cleaning up old files on remote"
ssh $Host_ "cd $RemoteDir && ls -A | grep -v -E '^(\.env|instances-.*\.json|\.gitattributes|Dockerfile|\.dockerignore|build\.sh|run\.sh|src|pyproject\.toml|\.python-version|uv\.lock|logs)$' | xargs -r rm -rf"

# 이미지 빌드
Write-Host "==> Building Docker image on remote"
ssh $Host_ "cd $RemoteDir && bash build.sh"

# 컨테이너 시작
Write-Host "==> Starting mariadb-mcp containers"
ssh $Host_ "bash $RemoteDir/run.sh"

# 기동 확인
Write-Host "==> Waiting for startup..."
Start-Sleep -Seconds 5
ssh $Host_ "docker ps --filter name=mariadb-mcp --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'"
Write-Host "Done."
