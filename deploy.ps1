# mariadb-mcp 배포 (Windows → Linux)
# - deploy.sh 와 같은 순서·같은 결과 (rsync --delete 대신 임시 디렉터리 + scp + 원격 정리)
# - 서버의 .env 와 instances.json 은 서버에만 둔다 — 자격증명이 든 운영 설정이라 배포가 올리지도 덮지도 지우지도
#   않는다. 둘 중 하나라도 없으면 아무것도 하지 않고 중단한다.
# - 소스 업로드와 이미지 빌드는 기존 컨테이너가 서비스하는 동안 한다. 순단은 전환(기존 정지 → 새 컨테이너 healthy) 구간뿐이다.
# - 전환은 rename 방식: 기존 컨테이너를 <NAME>-old 로 바꿔 정지 보관하고, 새 컨테이너가 healthy 가 되지 않거나
#   크래시 루프면 자동으로 되돌린다. -old 는 다음 배포에서 빌드가 성공한 뒤 로그를 백업하고 지운다.
# - PowerShell 5.1 은 네이티브 명령(ssh·scp)의 비영 종료로 멈추지 않는다 — 모든 원격 호출 뒤 $LASTEXITCODE 를 본다.
# - 사용법: .\deploy.ps1
#   $env:MCP_DEPLOY_TARGET = 'test' 이면 시험 대상(~/mariadb-mcp-test, 컨테이너 mariadb-mcp-test, 호스트 포트 19001,
#   이미지 codescent/mariadb-mcp:test)에 배포한다. 대상은 이 두 묶음만 허용한다.
$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

$Host_ = "DM300S3B-B33"
$Target = if ($env:MCP_DEPLOY_TARGET) { $env:MCP_DEPLOY_TARGET } else { "prod" }

# 0. 대상 결정 — 원격 정리(rm -rf)가 빈 값·오타로 엉뚱한 디렉터리를 지우지 않게 고정 묶음만 허용한다.
switch -CaseSensitive ($Target) {
    "prod" { $Name = "mariadb-mcp"; $Port = "9001"; $RemoteDir = "~/mariadb-mcp"; $Image = "codescent/mariadb-mcp:latest" }
    "test" { $Name = "mariadb-mcp-test"; $Port = "19001"; $RemoteDir = "~/mariadb-mcp-test"; $Image = "codescent/mariadb-mcp:test" }
    default { Write-Error "MCP_DEPLOY_TARGET 은 prod 또는 test 만 허용한다: '$Target'"; exit 1 }
}
$Old = "$Name-old"
Write-Host "==> 대상: $Target (${Host_}:$RemoteDir, $Name, :$Port, $Image)"

function Invoke-Ssh([string]$Command) {
    # ssh 의 stdout 을 PowerShell 파이프로 받으면(변수 대입·| Out-Null 등) SSH 세션 안에서 실행할 때 ssh 가 끝나지 않는다.
    # 출력은 OS 수준 파일 리다이렉트로 받고 종료 코드는 프로세스에서 읽는다.
    $out = [IO.Path]::GetTempFileName(); $err = [IO.Path]::GetTempFileName()
    try {
        $arg = '"' + $Command.Replace('"', '\"') + '"'
        $p = Start-Process -FilePath ssh -ArgumentList @($Host_, $arg) -NoNewWindow -Wait -PassThru -RedirectStandardOutput $out -RedirectStandardError $err
        [pscustomobject]@{ Code = $p.ExitCode; Out = [IO.File]::ReadAllText($out).TrimEnd(); Err = [IO.File]::ReadAllText($err).TrimEnd() }
    } finally { Remove-Item -LiteralPath $out, $err -ErrorAction Ignore }
}

function Invoke-Remote([string]$Command) {
    $r = Invoke-Ssh $Command
    if ($r.Out) { Write-Host $r.Out }
    if ($r.Code -ne 0) {
        if ($r.Err) { Write-Host $r.Err }
        throw "원격 명령 실패(exit $($r.Code)): $Command"
    }
}

function Test-Remote([string]$Command) {
    return ((Invoke-Ssh $Command).Code -eq 0)
}

function Invoke-Rollback {
    # $Old 가 포트를 쥔 채 정지돼 있으므로 새 컨테이너를 먼저 지운다. $Old 가 없으면 되살릴 것이 없으니 새 컨테이너를 조사용으로 남긴다.
    $r = Invoke-Ssh "docker logs --tail 100 $Name"
    Write-Host ($r.Out + "`n" + $r.Err)
    if (Test-Remote "docker inspect $Old >/dev/null 2>&1") {
        Invoke-Remote "docker rm -f $Name >/dev/null 2>&1 || true; docker rename $Old $Name && docker start $Name"
        Write-Host "롤백 완료: 이전 컨테이너가 다시 :$Port 를 서비스합니다."
    } else {
        Write-Host "롤백 대상 $Old 가 없습니다 - 새 컨테이너 $Name 을 지우지 않고 남깁니다."
    }
    exit 1
}

# 서버 설정 확인 — run.sh 가 둘 다 쓴다. instances.json 이 없으면 docker 가 그 자리에 디렉터리를 만들어 기동이 깨진다.
if (-not (Test-Remote "test -f $RemoteDir/.env && test -f $RemoteDir/instances.json")) {
    Write-Error "서버 $RemoteDir 에 .env 또는 instances.json 이 없습니다. 서버에서 직접 만든 뒤 다시 배포하세요."
    exit 1
}

# 1. 소스 업로드 — 기존 컨테이너는 계속 서비스한다 (deploy.sh rsync --delete 대체)
$ExcludeNames = @('deploy.sh', 'deploy.ps1', 'README.md', 'LICENSE', 'docker-compose.yml', 'instances.json')
$ExcludePatterns = @('.env*', '.git*', '*.example.json')
$ExcludeDirs = @('.git', '.venv', '__pycache__', 'logs')

$TempDir = Join-Path $env:TEMP "deploy-$Name"
if (Test-Path -LiteralPath $TempDir) { Remove-Item -LiteralPath $TempDir -Recurse -Force }
New-Item -ItemType Directory -Path $TempDir | Out-Null

Get-ChildItem -LiteralPath $ScriptDir -Force | Where-Object {
    $n = $_.Name
    if ($ExcludeNames -contains $n) { return $false }
    if ($_.PSIsContainer -and ($ExcludeDirs -contains $n)) { return $false }
    foreach ($p in $ExcludePatterns) { if ($n -like $p) { return $false } }
    return $true
} | ForEach-Object {
    if ($_.PSIsContainer) {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $TempDir $_.Name) -Recurse
    } else {
        Copy-Item -LiteralPath $_.FullName -Destination $TempDir
    }
}

$tempSrcTests = Join-Path $TempDir "src/tests"
if (Test-Path -LiteralPath $tempSrcTests) { Remove-Item -LiteralPath $tempSrcTests -Recurse -Force }
$tempPyCache = Join-Path $TempDir "src/__pycache__"
if (Test-Path -LiteralPath $tempPyCache) { Remove-Item -LiteralPath $tempPyCache -Recurse -Force }

Write-Host "==> Uploading files to ${Host_}:${RemoteDir}"
Invoke-Remote "mkdir -p $RemoteDir"
foreach ($item in @(Get-ChildItem -LiteralPath $TempDir -Force)) {
    if ($item.PSIsContainer) {
        Invoke-Remote "rm -rf $RemoteDir/$($item.Name)"
        scp -r $item.FullName "${Host_}:${RemoteDir}/"
    } else {
        scp $item.FullName "${Host_}:${RemoteDir}/"
    }
    if ($LASTEXITCODE -ne 0) { throw "업로드 실패(exit $LASTEXITCODE): $($item.Name)" }
    Write-Host "  $($item.Name)"
}

# 원격 정리 (업로드되지 않은 파일/디렉터리 제거 — .env·instances.json·logs 는 보존)
Write-Host "==> Cleaning up old files on remote"
$UploadedNames = @(Get-ChildItem -LiteralPath $TempDir -Force | ForEach-Object { [regex]::Escape($_.Name) })
$PreserveRegex = '^(' + ($UploadedNames -join '|') + '|\.env.*|logs|instances\.json)$'
Invoke-Remote "cd $RemoteDir && ls -A | grep -v -E '$PreserveRegex' | xargs -r rm -rf"

Remove-Item -LiteralPath $TempDir -Recurse -Force

Write-Host "==> Converting CRLF to LF on remote"
Invoke-Remote "cd $RemoteDir && find . -type f ! -name 'instances.json' ! -name '.env' \( -name '*.sh' -o -name '*.py' -o -name '*.conf' -o -name '*.cnf' -o -name '*.cf' -o -name '*.yaml' -o -name '*.yml' -o -name '*.toml' -o -name '*.json' -o -name '*.ini' -o -name '*.sql' -o -name '*.pem' -o -name '*.lock' -o -name 'Dockerfile' -o -name '.dockerignore' -o -name '.python-version' \) -exec sed -i 's/\r$//' {} +"
# scp 는 권한을 보존하지 않는다
Invoke-Remote "cd $RemoteDir && find . -name '*.sh' -exec chmod +x {} + && mkdir -p logs && chmod 755 logs"

# 2. 이미지 빌드 — 운영 배포를 git 작업 트리에서 할 때만 커밋 태그를 함께 붙인다(시험은 떠도는 태그를 남기지 않는다)
$Tags = $Image
if ($Target -eq "prod") {
    # git 밖(스크래치 사본)이면 stderr 를 내므로 이 구간만 Stop 을 풀어 예외 대신 $LASTEXITCODE 로 가른다
    $ErrorActionPreference = "Continue"
    $sha = git -C $ScriptDir rev-parse --short HEAD 2>$null
    $inRepo = ($LASTEXITCODE -eq 0 -and $sha)
    $dirty = if ($inRepo) { git -C $ScriptDir status --porcelain } else { $null }
    $ErrorActionPreference = "Stop"
    if ($inRepo) {
        if ($dirty) { $sha = "$sha-dirty" }
        $Tags = "$Tags codescent/mariadb-mcp:$sha"
    }
}
Write-Host "==> Building image ($Tags)"
$build = Invoke-Ssh "bash $RemoteDir/build.sh $Tags"
if ($build.Code -ne 0) {
    Write-Host ((($build.Out + "`n" + $build.Err) -split "`n" | Select-Object -Last 40) -join "`n")
    Write-Error "이미지 빌드 실패 - 운영 컨테이너는 손대지 않았습니다."
    exit 1
}

# 3. 이전 세대 정리 — docker rm 은 회전 로그까지 지우므로 먼저 백업한다(서버 로그는 stderr 라 2>&1 필요).
#    판정은 docker logs 의 성공 여부다 — 로그 없이 죽은 컨테이너는 백업이 0바이트여도 정상이다.
Write-Host "==> 이전 세대($Old) 정리"
if (Test-Remote "docker inspect $Old >/dev/null 2>&1") {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $logFile = "$RemoteDir/logs/$Name-pre-transition-$stamp.log"
    if (-not (Test-Remote "docker logs $Old > $logFile 2>&1")) {
        Write-Error "$Old 로그 백업 실패 - 지우지 않고 중단합니다(남아 있으면 이번 전환에서 이름이 충돌한다)."
        exit 1
    }
    $size = (Invoke-Ssh "stat -c %s $logFile").Out
    Invoke-Remote "docker rm $Old"
    Write-Host "  로그 백업($size bytes) 후 제거 완료"
} else {
    Write-Host "  이전 세대 없음"
}

# 4. 전환 — 여기부터 순단
Write-Host "==> 전환: $Name -> $Old, 새 컨테이너 기동"
if (Test-Remote "docker inspect $Name >/dev/null 2>&1") {
    Invoke-Remote "docker rename $Name $Old && docker stop -t 10 $Old"
}
if (-not (Test-Remote "bash $RemoteDir/run.sh $Name $Port $Image")) {
    Write-Host "ERROR: 새 컨테이너를 띄우지 못했습니다. 롤백합니다."
    Invoke-Rollback
}

# 5. healthy 대기 (최대 300초, 5초 간격). unless-stopped 라 죽어도 다시 뜨므로 Running 은 판정에 못 쓴다.
#    RestartCount 가 2를 넘으면 크래시 루프로 보고 바로 롤백한다.
Write-Host "==> health 대기 (최대 300초)"
$healthy = $false
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 5
    $info = (Invoke-Ssh "docker inspect --format '{{.State.Health.Status}} {{.RestartCount}}' $Name 2>/dev/null").Out.Trim()
    $parts = $info -split ' '
    if ($parts[0] -eq 'healthy') { $healthy = $true; break }
    if ($parts.Count -ge 2 -and $parts[1] -match '^\d+$' -and [int]$parts[1] -gt 2) {
        Write-Host "ERROR: 크래시 루프(RestartCount=$($parts[1])). 롤백합니다."
        Invoke-Rollback
    }
}
if (-not $healthy) {
    Write-Host "ERROR: 300초 안에 healthy 가 되지 않았습니다. 롤백합니다."
    Invoke-Rollback
}

Write-Host "==> 전환 완료 (healthy)"
Write-Host (Invoke-Ssh "docker ps --filter name=^$Name`$ --format '{{.Names}}  {{.Status}}  {{.Image}}'").Out
Write-Host ""
Write-Host "이전 컨테이너 $Old 는 정지 상태로 남아 있다(처음 배포면 없다) - 다음 배포에서 빌드 성공 뒤 로그 백업 후 제거된다."
Write-Host "지금 되돌리려면:"
Write-Host "  ssh $Host_ `"if docker inspect $Old >/dev/null 2>&1; then docker rm -f $Name && docker rename $Old $Name && docker start $Name; else echo '백업 컨테이너 $Old 가 없다 - 롤백할 수 없어 지우지 않는다'; fi`""
