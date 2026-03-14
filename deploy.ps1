# mariadb-mcp 배포 (Windows → Linux)
# - deploy.sh와 동일한 결과 보장 (rsync --delete 대체)
# - 사용법: .\deploy.ps1
$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

$Host_ = "DM300S3B-B33-jhcheong"
$RemoteDir = "~/mariadb-mcp"

# .env.production 확인
if (-not (Test-Path ".env.production")) {
    Write-Error ".env.production 파일이 없습니다. .env.example을 참고하여 생성하세요."
    exit 1
}

# 기존 컨테이너 정리 (레거시 + 이전 3대 구성 + 현재 단일)
Write-Host "==> Stopping old containers"
$OldContainers = @(
    "mariadb-mcp-raspberrypi", "mariadb-mcp-orangepi5plus",
    "mariadb-mcp-RM8130N6Z64", "mariadb-mcp-rm8130n6z64",
    "mariadb-mcp-b-flow-new-temp", "mariadb-mcp-b-flow-standalone",
    "mariadb-mcp-b-flow-middleware-auth", "mariadb-mcp-b-flow-push",
    "mariadb-mcp-bflow-shoplinker",
    "mariadb-mcp-main", "mariadb-mcp-bflow",
    "mariadb-mcp"
)
$OldContainersStr = $OldContainers -join " "
ssh $Host_ "for c in $OldContainersStr; do docker stop `$c 2>/dev/null; docker rm `$c 2>/dev/null; done || true"

# --- 파일 동기화 (deploy.sh rsync --delete 대체) ---
# 제외 대상 (deploy.sh --exclude와 동일)
$ExcludeNames = @('deploy.sh', 'deploy.ps1', '.gitignore', '.gitattributes', 'README.md', 'LICENSE', 'docker-compose.yml')
$ExcludePatterns = @('.env*', '.git*', 'instances-*.json', '*.example.json')
$ExcludeDirs = @('.git', '.venv', '__pycache__', 'logs')

# 임시 디렉토리에 배포 대상만 복사
$TempDir = Join-Path $env:TEMP "deploy-mariadb-mcp"
if (Test-Path $TempDir) { Remove-Item -Recurse -Force $TempDir }
New-Item -ItemType Directory -Path $TempDir | Out-Null

Get-ChildItem -Path $ScriptDir -Force | Where-Object {
    $n = $_.Name
    if ($ExcludeNames -contains $n) { return $false }
    if ($_.PSIsContainer -and ($ExcludeDirs -contains $n)) { return $false }
    foreach ($p in $ExcludePatterns) { if ($n -like $p) { return $false } }
    return $true
} | ForEach-Object {
    if ($_.PSIsContainer) {
        Copy-Item $_.FullName (Join-Path $TempDir $_.Name) -Recurse
    } else {
        Copy-Item $_.FullName $TempDir
    }
}

# src/tests/ 제거 (배포 대상에서 제외)
$tempSrcTests = Join-Path $TempDir "src/tests"
if (Test-Path $tempSrcTests) { Remove-Item -Recurse -Force $tempSrcTests }

# 업로드
Write-Host "==> Uploading files to ${Host_}:${RemoteDir}"
ssh $Host_ "mkdir -p $RemoteDir"

foreach ($item in @(Get-ChildItem $TempDir -Force)) {
    if ($item.PSIsContainer) {
        ssh $Host_ "rm -rf $RemoteDir/$($item.Name)"
        scp -r $item.FullName "${Host_}:${RemoteDir}/"
    } else {
        scp $item.FullName "${Host_}:${RemoteDir}/"
    }
    Write-Host "  $($item.Name)"
}

# 원격 정리 (업로드되지 않은 파일/디렉토리 제거)
Write-Host "==> Cleaning up old files on remote"
$UploadedNames = @(Get-ChildItem $TempDir -Force | ForEach-Object { [regex]::Escape($_.Name) })
$PreserveRegex = '^(' + ($UploadedNames -join '|') + '|\.env.*|logs)$'
ssh $Host_ "cd $RemoteDir && ls -A | grep -v -E '$PreserveRegex' | xargs -r rm -rf"

Remove-Item -Recurse -Force $TempDir

# CRLF→LF 변환
Write-Host "==> Converting CRLF to LF on remote"
ssh $Host_ "cd $RemoteDir && find . -type f \( -name '*.sh' -o -name '*.py' -o -name '*.conf' -o -name '*.yaml' -o -name 'Dockerfile' -o -name '.dockerignore' \) -exec sed -i 's/\r$//' {} +"

# .env.production → .env
Write-Host "==> Deploying .env.production as .env"
scp ".env.production" "${Host_}:${RemoteDir}/.env"
ssh $Host_ "sed -i 's/\r$//' $RemoteDir/.env"

# 이미지 빌드
Write-Host "==> Building Docker image on remote"
ssh $Host_ "cd $RemoteDir && bash build.sh"

# 컨테이너 시작
Write-Host "==> Starting mariadb-mcp container"
ssh $Host_ "bash $RemoteDir/run.sh"

# 기동 확인
Write-Host "==> Waiting for startup..."
Start-Sleep -Seconds 5
ssh $Host_ "docker ps --filter name=mariadb-mcp --format 'table {{.Names}}`t{{.Status}}`t{{.Ports}}'"
Write-Host "Done."
