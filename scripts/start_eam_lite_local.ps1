param(
    [switch]$OpenBrowserWhenReady
)

$ErrorActionPreference = "Stop"
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}

$containerName = "eam-lite-sprint0-pg"
$databaseName = "eam_lite_sprint1_browser"
$databasePort = "54320"
$serverPort = 8765
$localUrl = "http://127.0.0.1:$serverPort/"

if ($OpenBrowserWhenReady) {
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        try {
            $response = Invoke-WebRequest -Uri "${localUrl}login/" -UseBasicParsing -TimeoutSec 1
            if (
                $response.StatusCode -ge 200 -and
                $response.StatusCode -lt 400 -and
                $response.Content -match "登录 EAM-Lite"
            ) {
                Start-Process -FilePath $localUrl
                exit 0
            }
        }
        catch {
        }
        Start-Sleep -Milliseconds 500
    }
    exit 0
}

function Invoke-QuietNativeCommand {
    param([scriptblock]$Command)

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "SilentlyContinue"
        & $Command *> $null
        return $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
}

try {
    $repoRoot = Split-Path -Parent $PSScriptRoot
    $managePy = Join-Path $repoRoot "manage.py"
    $pythonExe = Join-Path $repoRoot ".venv\Scripts\python.exe"

    if (-not (Test-Path -LiteralPath $managePy -PathType Leaf)) {
        throw "无法定位 EAM-Lite 仓库：未找到 $managePy"
    }

    Set-Location -LiteralPath $repoRoot

    $existingListener = Get-NetTCPConnection -LocalPort $serverPort -State Listen -ErrorAction SilentlyContinue
    if ($existingListener) {
        $eamAlreadyRunning = $false
        try {
            $existingResponse = Invoke-WebRequest -Uri "${localUrl}login/" -UseBasicParsing -TimeoutSec 3
            $eamAlreadyRunning = (
                $existingResponse.StatusCode -ge 200 -and
                $existingResponse.StatusCode -lt 400 -and
                $existingResponse.Content -match "登录 EAM-Lite"
            )
        }
        catch {
        }
        if (-not $eamAlreadyRunning) {
            throw "端口 $serverPort 已被其他程序占用，请先关闭该程序后重试。"
        }

        Write-Host "检测到 EAM-Lite 已在运行，正在打开浏览器。" -ForegroundColor Green
        Start-Process -FilePath $localUrl
        exit 0
    }

    if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
        throw "未找到本地 Python 环境：$pythonExe。请先创建 .venv 并安装项目依赖。"
    }

    $dockerCommand = Get-Command docker.exe -ErrorAction SilentlyContinue
    if (-not $dockerCommand) {
        throw "未找到 Docker 命令。请先安装 Docker Desktop。"
    }
    $dockerExe = $dockerCommand.Source

    $dockerReady = ((Invoke-QuietNativeCommand { & $dockerExe info }) -eq 0)
    if (-not $dockerReady) {
        $dockerDesktopProcess = Get-Process -Name "Docker Desktop" -ErrorAction SilentlyContinue
        if (-not $dockerDesktopProcess) {
            $dockerDesktopCandidates = @(
                "${env:ProgramFiles}\Docker\Docker\Docker Desktop.exe",
                "${env:LOCALAPPDATA}\Docker\Docker Desktop.exe"
            )
            if (${env:ProgramFiles(x86)}) {
                $dockerDesktopCandidates += "${env:ProgramFiles(x86)}\Docker\Docker\Docker Desktop.exe"
            }
            $dockerDesktopExe = $dockerDesktopCandidates |
                Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
                Select-Object -First 1
            if (-not $dockerDesktopExe) {
                throw "Docker Desktop 未运行，且未找到 Docker Desktop.exe。请手动启动 Docker Desktop 后重试。"
            }

            Write-Host "正在启动 Docker Desktop，请稍候……" -ForegroundColor Cyan
            Start-Process -FilePath $dockerDesktopExe -WindowStyle Hidden
        }
        else {
            Write-Host "Docker Desktop 正在启动，请稍候……" -ForegroundColor Cyan
        }

        for ($attempt = 0; $attempt -lt 90; $attempt++) {
            Start-Sleep -Seconds 2
            if ((Invoke-QuietNativeCommand { & $dockerExe info }) -eq 0) {
                $dockerReady = $true
                break
            }
        }
        if (-not $dockerReady) {
            throw "等待 Docker Desktop 就绪超时，请确认 Docker Desktop 可以正常启动。"
        }
    }

    if ((Invoke-QuietNativeCommand { & $dockerExe inspect $containerName }) -ne 0) {
        throw "未找到数据库容器 $containerName。请先创建该容器。"
    }

    $containerRunning = [string](& $dockerExe inspect --format "{{.State.Running}}" $containerName 2>$null)
    if ($LASTEXITCODE -ne 0) {
        throw "无法读取数据库容器 $containerName 的状态。"
    }
    if ($containerRunning.Trim() -ne "true") {
        Write-Host "正在启动数据库容器 $containerName……" -ForegroundColor Cyan
        if ((Invoke-QuietNativeCommand { & $dockerExe start $containerName }) -ne 0) {
            throw "数据库容器 $containerName 启动失败。请确认本机端口 $databasePort 未被占用或系统保留。"
        }
    }

    $containerEnv = @(& $dockerExe inspect --format "{{range .Config.Env}}{{println .}}{{end}}" $containerName 2>$null)
    if ($LASTEXITCODE -ne 0) {
        throw "无法读取数据库容器配置。"
    }
    $postgresUser = $null
    $postgresPassword = $null
    foreach ($line in $containerEnv) {
        if ($line -match "^POSTGRES_USER=(.*)$") {
            $postgresUser = $Matches[1]
        }
        elseif ($line -match "^POSTGRES_PASSWORD=(.*)$") {
            $postgresPassword = $Matches[1]
        }
    }
    if ([string]::IsNullOrWhiteSpace($postgresUser) -or [string]::IsNullOrWhiteSpace($postgresPassword)) {
        throw "数据库容器缺少 POSTGRES_USER 或 POSTGRES_PASSWORD 配置。"
    }

    $postgresReady = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        if ((Invoke-QuietNativeCommand { & $dockerExe exec $containerName pg_isready -U $postgresUser -d $databaseName }) -eq 0) {
            $postgresReady = $true
            break
        }
        Start-Sleep -Seconds 1
    }
    if (-not $postgresReady) {
        throw "数据库容器已启动，但 PostgreSQL 未在预期时间内就绪。"
    }

    $defaultRoute = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix "0.0.0.0/0" -ErrorAction Stop |
        Where-Object { $_.NextHop -ne "0.0.0.0" -and $_.State -ne "Invalid" } |
        Sort-Object -Property RouteMetric, InterfaceMetric |
        Select-Object -First 1
    if (-not $defaultRoute) {
        throw "未找到可用的 IPv4 默认路由，无法生成手机扫码地址。"
    }

    $lanIp = Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $defaultRoute.InterfaceIndex -ErrorAction Stop |
        Where-Object {
            $_.AddressState -eq "Preferred" -and
            $_.IPAddress -ne "127.0.0.1" -and
            $_.IPAddress -notlike "169.254.*"
        } |
        Select-Object -First 1 -ExpandProperty IPAddress
    if ([string]::IsNullOrWhiteSpace($lanIp)) {
        throw "未找到默认网络接口的局域网 IPv4 地址，无法生成手机扫码地址。"
    }

    $localVarDir = Join-Path $repoRoot "var\local"
    $secretKeyPath = Join-Path $localVarDir "secret_key.txt"
    New-Item -ItemType Directory -Path $localVarDir -Force | Out-Null
    if (Test-Path -LiteralPath $secretKeyPath -PathType Leaf) {
        $secretKey = [System.IO.File]::ReadAllText($secretKeyPath).Trim()
    }
    else {
        $secretBytes = New-Object byte[] 64
        $random = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        try {
            $random.GetBytes($secretBytes)
        }
        finally {
            $random.Dispose()
        }
        $secretKey = [Convert]::ToBase64String($secretBytes)
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($secretKeyPath, $secretKey, $utf8NoBom)
    }
    if ([string]::IsNullOrWhiteSpace($secretKey)) {
        throw "本地 SECRET_KEY 文件为空：$secretKeyPath"
    }

    $env:SECRET_KEY = $secretKey
    $env:DEBUG = "true"
    $env:ALLOWED_HOSTS = "127.0.0.1,localhost,$lanIp"
    $env:QR_BASE_URL = "http://${lanIp}:$serverPort"
    $env:DB_ENGINE = "postgresql"
    $env:DB_NAME = $databaseName
    $env:DB_USER = $postgresUser
    $env:DB_PASSWORD = $postgresPassword
    $env:DB_HOST = "127.0.0.1"
    $env:DB_PORT = $databasePort

    Write-Host "正在执行数据库迁移……" -ForegroundColor Cyan
    & $pythonExe $managePy migrate --noinput
    if ($LASTEXITCODE -ne 0) {
        throw "数据库迁移失败，请查看上方错误信息。"
    }

    $lanUrl = "http://${lanIp}:$serverPort/"
    Write-Host ""
    Write-Host "EAM-Lite 已准备启动。" -ForegroundColor Green
    Write-Host "电脑访问：$localUrl"
    Write-Host "手机访问：$lanUrl（手机须连接同一局域网）"
    Write-Host "按 Ctrl+C 停止服务。" -ForegroundColor Yellow
    Write-Host ""

    try {
        $powerShellExe = (Get-Process -Id $PID).Path
        $helperArguments = "-NoLogo -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -OpenBrowserWhenReady"
        Start-Process -FilePath $powerShellExe -ArgumentList $helperArguments -WindowStyle Hidden
    }
    catch {
        Write-Host "浏览器未能自动打开，请手动访问 $localUrl" -ForegroundColor Yellow
    }

    & $pythonExe $managePy runserver "0.0.0.0:$serverPort" --noreload
    if ($LASTEXITCODE -ne 0) {
        throw "EAM-Lite 服务异常退出。"
    }
}
catch {
    Write-Host ""
    Write-Host "启动失败：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
