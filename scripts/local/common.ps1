Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}

$script:StableProject = "eam-lite-local"
$script:DevelopmentProject = "eam-lite-dev"
$script:StableUrl = "http://127.0.0.1:8765"
$script:DevelopmentUrl = "http://127.0.0.1:8766"
$script:PostgresImage = "postgres:18.6-alpine@sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2"
$script:CaddyImage = "caddy:2.11.4-alpine@sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648"

function Get-EamRepositoryRoot {
    return (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
}

function Assert-EamWindows {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw "本机一键脚本只支持 Windows 10/11。"
    }
}

function Get-EamStateRoot {
    param([ValidateSet("local", "dev")][string]$Mode)

    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        throw "无法定位当前用户的 LOCALAPPDATA。"
    }
    return (Join-Path $env:LOCALAPPDATA ("EAM-Lite\" + $Mode))
}

function ConvertTo-EamComposePath {
    param([Parameter(Mandatory = $true)][string]$Path)
    return ([System.IO.Path]::GetFullPath($Path) -replace "\\", "/")
}

function Get-EamDocumentsDirectory {
    $documents = [Environment]::GetFolderPath([Environment+SpecialFolder]::MyDocuments)
    if ([string]::IsNullOrWhiteSpace($documents)) {
        $documents = Join-Path $env:USERPROFILE "Documents"
    }
    return $documents
}

function New-EamRandomSecret {
    param([int]$ByteCount = 48)

    $bytes = New-Object byte[] $ByteCount
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    }
    finally {
        $generator.Dispose()
    }
    return [Convert]::ToBase64String($bytes)
}

function Protect-EamFileForCurrentUser {
    param([Parameter(Mandatory = $true)][string]$Path)

    try {
        $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        & icacls.exe $Path /inheritance:r /grant:r "${identity}:(F)" /c *> $null
        if ($LASTEXITCODE -ne 0) {
            throw "icacls 返回 $LASTEXITCODE"
        }
    }
    catch {
        Write-Warning "无法收紧文件 ACL；请确认只有当前 Windows 用户可访问：$Path"
    }
}

function Write-EamRestrictedText {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Value
    )

    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Value, $encoding)
    Protect-EamFileForCurrentUser -Path $Path
}

function Initialize-EamState {
    param([ValidateSet("local", "dev")][string]$Mode)

    $stateRoot = Get-EamStateRoot -Mode $Mode
    $secretRoot = Join-Path $stateRoot "secrets"
    $temporaryRoot = Join-Path $stateRoot "tmp"
    $logRoot = Join-Path $stateRoot "logs"
    foreach ($path in @($stateRoot, $secretRoot, $temporaryRoot, $logRoot)) {
        New-Item -ItemType Directory -Path $path -Force | Out-Null
    }

    $secretNames = @(
        "secret_key",
        "db_admin_password",
        "db_migration_password",
        "db_runtime_password",
        "backup_key"
    )
    foreach ($name in $secretNames) {
        $secretPath = Join-Path $secretRoot ($name + ".txt")
        if (-not (Test-Path -LiteralPath $secretPath -PathType Leaf)) {
            Write-EamRestrictedText -Path $secretPath -Value (New-EamRandomSecret)
        }
        else {
            Protect-EamFileForCurrentUser -Path $secretPath
        }
    }

    $placeholder = Join-Path $temporaryRoot "passphrase-placeholder.txt"
    if (-not (Test-Path -LiteralPath $placeholder -PathType Leaf)) {
        Write-EamRestrictedText -Path $placeholder -Value (New-EamRandomSecret)
    }
    else {
        Protect-EamFileForCurrentUser -Path $placeholder
    }
    $emptyPackage = Join-Path $temporaryRoot "empty.eambak"
    if (-not (Test-Path -LiteralPath $emptyPackage -PathType Leaf)) {
        [System.IO.File]::WriteAllBytes($emptyPackage, [byte[]]@())
        Protect-EamFileForCurrentUser -Path $emptyPackage
    }
    else {
        Protect-EamFileForCurrentUser -Path $emptyPackage
    }

    $backupOutput = Join-Path (Get-EamDocumentsDirectory) "EAM-Lite备份"
    New-Item -ItemType Directory -Path $backupOutput -Force | Out-Null

    return [pscustomobject]@{
        Mode = $Mode
        Root = $stateRoot
        Secrets = $secretRoot
        Temporary = $temporaryRoot
        Logs = $logRoot
        EnvFile = Join-Path $stateRoot "compose.env"
        ReleaseMarker = Join-Path $stateRoot "last-release.json"
        LastPortableBackup = Join-Path $stateRoot "last-portable-backup.json"
        CurrentReleasePointer = Join-Path $stateRoot "current-release.txt"
        BackupOutput = $backupOutput
        PlaceholderPassphrase = $placeholder
        EmptyPackage = $emptyPackage
    }
}

function Get-EamDockerExecutable {
    $command = Get-Command docker.exe -ErrorAction SilentlyContinue
    if (-not $command) {
        throw "未找到 Docker。请先安装 Docker Desktop，然后重新运行。"
    }
    return $command.Source
}

function Test-EamDockerReady {
    param([Parameter(Mandatory = $true)][string]$DockerExe)

    $result = & $DockerExe info --format "{{.ServerVersion}}" 2>$null
    return ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace(($result -join "")))
}

function Start-EamDockerDesktop {
    $candidates = @(
        (Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Docker\Docker\Docker Desktop.exe")
    ) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    $desktop = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    if (-not $desktop) {
        throw "Docker Desktop 尚未运行，也未在默认位置找到。请手动启动 Docker Desktop。"
    }
    Write-Host "正在启动 Docker Desktop，请稍候……" -ForegroundColor Cyan
    Start-Process -FilePath $desktop -WindowStyle Hidden | Out-Null
}

function Ensure-EamDockerReady {
    $dockerExe = Get-EamDockerExecutable
    if (Test-EamDockerReady -DockerExe $dockerExe) {
        return $dockerExe
    }
    Start-EamDockerDesktop
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        Start-Sleep -Seconds 2
        if (Test-EamDockerReady -DockerExe $dockerExe) {
            Write-Host "Docker Engine 已就绪。" -ForegroundColor Green
            return $dockerExe
        }
        if (($attempt + 1) % 10 -eq 0) {
            Write-Host "仍在等待 Docker Engine……" -ForegroundColor DarkGray
        }
    }
    throw "Docker Engine 在 120 秒内未就绪。脚本未重置 Docker，也未删除任何镜像或数据卷。"
}

function Resolve-EamGitExecutable {
    $command = Get-Command git.exe -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $candidates = @(
        (Join-Path $env:ProgramFiles "Git\cmd\git.exe"),
        (Join-Path $env:ProgramFiles "Git\bin\git.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Git\cmd\git.exe"),
        (Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\native\git\cmd\git.exe")
    )
    foreach ($candidate in $candidates) {
        if (-not [string]::IsNullOrWhiteSpace($candidate) -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return $candidate
        }
    }
    return $null
}

function Invoke-EamGit {
    param(
        [Parameter(Mandatory = $true)][string]$RepositoryRoot,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [switch]$AllowFailure
    )

    $gitExe = Resolve-EamGitExecutable
    if (-not $gitExe) {
        throw "未找到 Git，无法验证当前源码工作树。GitHub Release 解压版不需要 Git。"
    }
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = & $gitExe -C $RepositoryRoot @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($exitCode -ne 0 -and -not $AllowFailure) {
        throw "Git 检查失败：$($output -join [Environment]::NewLine)"
    }
    return [pscustomobject]@{ ExitCode = $exitCode; Output = @($output) }
}

function Test-EamGitRepository {
    param([Parameter(Mandatory = $true)][string]$RepositoryRoot)
    $gitExe = Resolve-EamGitExecutable
    if (-not $gitExe) {
        if (Test-Path -LiteralPath (Join-Path $RepositoryRoot ".git")) {
            throw "检测到 Git 工作树，但电脑上找不到可执行的 Git。请安装 Git，或改用完整 GitHub Release 解压包。"
        }
        return $false
    }
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = & $gitExe -C $RepositoryRoot rev-parse --is-inside-work-tree 2>&1
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    $result = [pscustomobject]@{ ExitCode = $exitCode; Output = @($output) }
    return ($result.ExitCode -eq 0 -and ($result.Output -join "").Trim() -eq "true")
}

function Get-EamStableIdentity {
    param([Parameter(Mandatory = $true)][string]$RepositoryRoot)

    $versionPath = Join-Path $RepositoryRoot "VERSION"
    if (-not (Test-Path -LiteralPath $versionPath -PathType Leaf)) {
        throw "缺少 VERSION，无法确认运行版本。"
    }
    $version = (Get-Content -LiteralPath $versionPath -Raw -Encoding UTF8).Trim()
    if (Test-EamGitRepository -RepositoryRoot $RepositoryRoot) {
        $dirty = Invoke-EamGit -RepositoryRoot $RepositoryRoot -Arguments @("status", "--porcelain=v1", "--untracked-files=all")
        if (-not [string]::IsNullOrWhiteSpace(($dirty.Output -join "`n"))) {
            throw "稳定使用版要求工作区完全干净。请提交/移走改动，或改用【启动开发环境.cmd】。"
        }
        $head = ((Invoke-EamGit -RepositoryRoot $RepositoryRoot -Arguments @("rev-parse", "HEAD")).Output -join "").Trim()
        $branch = ((Invoke-EamGit -RepositoryRoot $RepositoryRoot -Arguments @("branch", "--show-current")).Output -join "").Trim()
        $tags = (Invoke-EamGit -RepositoryRoot $RepositoryRoot -Arguments @("tag", "--points-at", "HEAD", "--list", "v[0-9]*")).Output
        $formalTag = @($tags | Where-Object { $_ -match '^v\d+\.\d+\.\d+([.-][0-9A-Za-z.-]+)?$' } | Select-Object -First 1)
        $sourceLabel = ""
        if ($branch -eq "main") {
            $remote = Invoke-EamGit -RepositoryRoot $RepositoryRoot -Arguments @("rev-parse", "refs/remotes/origin/main") -AllowFailure
            if ($remote.ExitCode -ne 0) {
                throw "未找到 origin/main，无法证明稳定版来源。请先联网执行更新。"
            }
            $remoteHead = ($remote.Output -join "").Trim()
            if ($head -ne $remoteHead) {
                throw "当前 main 与 origin/main 的精确 commit 不一致。请先运行【更新EAM-Lite.cmd】。"
            }
            $sourceLabel = "origin/main"
        }
        elseif ($formalTag.Count -gt 0) {
            $sourceLabel = $formalTag[0]
        }
        else {
            throw "稳定使用版只能运行干净的 main 或正式版本标签。当前分支为 $branch；请改用开发环境。"
        }
        return [pscustomobject]@{
            Kind = "git"
            Version = $version
            Commit = $head
            BuildTime = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
            AppImage = "eam-lite-local:$head"
            PostgresImage = $script:PostgresImage
            CaddyImage = $script:CaddyImage
            Source = $sourceLabel
            Repository = "Sakurakouji-Asahi/EAM-Lite"
        }
    }

    $manifestPath = Join-Path $RepositoryRoot "release-manifest.json"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "当前目录既不是 Git 仓库，也没有 release-manifest.json。"
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($field in @("version", "commit", "app_image", "app_image_digest", "postgres_image", "caddy_image", "created_at", "repository")) {
        if (-not ($manifest.PSObject.Properties.Name -contains $field) -or [string]::IsNullOrWhiteSpace([string]$manifest.$field)) {
            throw "Release 清单缺少字段：$field"
        }
    }
    if ($manifest.version -ne $version) {
        throw "Release 清单版本与 VERSION 不一致。"
    }
    if ($manifest.commit -notmatch '^[0-9a-fA-F]{40}$') {
        throw "Release 清单 commit 格式非法。"
    }
    if ($manifest.app_image_digest -notmatch '^sha256:[0-9a-fA-F]{64}$') {
        throw "Release 清单镜像 digest 格式非法。"
    }
    if ($manifest.app_image -match '(^|:)latest$') {
        throw "Release 清单不得引用 latest 镜像。"
    }
    return [pscustomobject]@{
        Kind = "release"
        Version = [string]$manifest.version
        Commit = [string]$manifest.commit
        BuildTime = [string]$manifest.created_at
        AppImage = ([string]$manifest.app_image + "@" + [string]$manifest.app_image_digest)
        PostgresImage = [string]$manifest.postgres_image
        CaddyImage = [string]$manifest.caddy_image
        Source = "GitHub Release"
        Repository = [string]$manifest.repository
    }
}

function Get-EamDevelopmentIdentity {
    param([Parameter(Mandatory = $true)][string]$RepositoryRoot)

    if (-not (Test-EamGitRepository -RepositoryRoot $RepositoryRoot)) {
        throw "开发环境必须从 Git clone/worktree 目录运行。"
    }
    $head = ((Invoke-EamGit -RepositoryRoot $RepositoryRoot -Arguments @("rev-parse", "HEAD")).Output -join "").Trim()
    $dirty = Invoke-EamGit -RepositoryRoot $RepositoryRoot -Arguments @("status", "--porcelain=v1", "--untracked-files=all")
    $commit = if ([string]::IsNullOrWhiteSpace(($dirty.Output -join "`n"))) { $head } else { $head + "-dirty" }
    $version = (Get-Content -LiteralPath (Join-Path $RepositoryRoot "VERSION") -Raw -Encoding UTF8).Trim()
    $runtimeSource = @(
        [System.IO.File]::ReadAllText((Join-Path $RepositoryRoot "deploy\Dockerfile"))
        [System.IO.File]::ReadAllText((Join-Path $RepositoryRoot "requirements\production.lock"))
    ) -join "`n"
    $runtimeBytes = [System.Text.Encoding]::UTF8.GetBytes($runtimeSource)
    $hasher = [System.Security.Cryptography.SHA256]::Create()
    try {
        $runtimeHash = ([BitConverter]::ToString($hasher.ComputeHash($runtimeBytes)) -replace "-", "").ToLowerInvariant()
    }
    finally {
        $hasher.Dispose()
    }
    return [pscustomobject]@{
        Kind = "git-dev"
        Version = $version
        Commit = $commit
        BuildCommit = $head
        BuildTime = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        AppImage = "eam-lite-dev:runtime-" + $runtimeHash.Substring(0, 12)
        PostgresImage = $script:PostgresImage
        Source = "当前开发工作区"
    }
}

function Write-EamComposeEnvironment {
    param(
        [Parameter(Mandatory = $true)]$State,
        [Parameter(Mandatory = $true)]$Identity,
        [string]$DevelopmentLanAddress = ""
    )

    $isDev = $State.Mode -eq "dev"
    $adminUser = if ($isDev) { "eam_lite_dev_admin" } else { "eam_lite_admin" }
    $migrationUser = if ($isDev) { "eam_lite_dev_migration" } else { "eam_lite_migration" }
    $runtimeUser = if ($isDev) { "eam_lite_dev_runtime" } else { "eam_lite_runtime" }
    $databaseName = if ($isDev) { "eam_lite_dev" } else { "eam_lite_local" }
    $lines = @(
        "EAM_VERSION=`"$($Identity.Version)`"",
        "EAM_BUILD_COMMIT=`"$($Identity.Commit)`"",
        "EAM_BUILD_TIME=`"$($Identity.BuildTime)`"",
        "EAM_APP_IMAGE=`"$($Identity.AppImage)`"",
        "EAM_DEV_IMAGE=`"$($Identity.AppImage)`"",
        "EAM_POSTGRES_IMAGE=`"$($Identity.PostgresImage)`"",
        "EAM_CADDY_IMAGE=`"$($script:CaddyImage)`"",
        "EAM_DATABASE_NAME=`"$databaseName`"",
        "EAM_DEV_DATABASE_NAME=`"$databaseName`"",
        "POSTGRES_ADMIN_USER=`"$adminUser`"",
        "POSTGRES_MIGRATION_USER=`"$migrationUser`"",
        "POSTGRES_RUNTIME_USER=`"$runtimeUser`"",
        "EAM_SECRET_KEY_FILE=`"$(ConvertTo-EamComposePath (Join-Path $State.Secrets 'secret_key.txt'))`"",
        "EAM_DB_ADMIN_PASSWORD_FILE=`"$(ConvertTo-EamComposePath (Join-Path $State.Secrets 'db_admin_password.txt'))`"",
        "EAM_DB_MIGRATION_PASSWORD_FILE=`"$(ConvertTo-EamComposePath (Join-Path $State.Secrets 'db_migration_password.txt'))`"",
        "EAM_DB_RUNTIME_PASSWORD_FILE=`"$(ConvertTo-EamComposePath (Join-Path $State.Secrets 'db_runtime_password.txt'))`"",
        "EAM_BACKUP_KEY_FILE=`"$(ConvertTo-EamComposePath (Join-Path $State.Secrets 'backup_key.txt'))`"",
        "EAM_PORTABLE_PASSPHRASE_FILE=`"$(ConvertTo-EamComposePath $State.PlaceholderPassphrase)`"",
        "EAM_PORTABLE_OUTPUT_DIR=`"$(ConvertTo-EamComposePath $State.BackupOutput)`"",
        "EAM_PORTABLE_BACKUP_FILE=`"$(ConvertTo-EamComposePath $State.EmptyPackage)`""
    )
    if ($isDev) {
        if ([string]::IsNullOrWhiteSpace($DevelopmentLanAddress)) {
            $developmentBindAddress = "127.0.0.1"
            $developmentAllowedHosts = "127.0.0.1,localhost"
            $developmentTrustedOrigins = "http://127.0.0.1:8766"
            $developmentQrBaseUrl = "http://127.0.0.1:8766"
        }
        else {
            $parsedAddress = $null
            if (-not [System.Net.IPAddress]::TryParse($DevelopmentLanAddress, [ref]$parsedAddress)) {
                throw "局域网测试地址不是有效的 IP：$DevelopmentLanAddress"
            }
            $developmentBindAddress = "0.0.0.0"
            $developmentAllowedHosts = "127.0.0.1,localhost,$DevelopmentLanAddress"
            $developmentTrustedOrigins = "http://127.0.0.1:8766,http://${DevelopmentLanAddress}:8766"
            $developmentQrBaseUrl = "http://${DevelopmentLanAddress}:8766"
        }
        $lines += @(
            "EAM_DEV_BIND_ADDRESS=`"$developmentBindAddress`"",
            "EAM_DEV_ALLOWED_HOSTS=`"$developmentAllowedHosts`"",
            "EAM_DEV_CSRF_TRUSTED_ORIGINS=`"$developmentTrustedOrigins`"",
            "EAM_DEV_QR_BASE_URL=`"$developmentQrBaseUrl`""
        )
    }
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($State.EnvFile, $lines, $encoding)
    Protect-EamFileForCurrentUser -Path $State.EnvFile
}

function Get-EamPrimaryLanAddress {
    $defaultRoute = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix "0.0.0.0/0" -ErrorAction Stop |
        Where-Object { $_.NextHop -ne "0.0.0.0" -and $_.State -ne "Invalid" } |
        Sort-Object -Property RouteMetric, InterfaceMetric |
        Select-Object -First 1
    if (-not $defaultRoute) {
        throw "未找到可用的 IPv4 默认路由，无法启动局域网扫码测试。"
    }

    $address = Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $defaultRoute.InterfaceIndex -ErrorAction Stop |
        Where-Object {
            $_.AddressState -eq "Preferred" -and
            $_.IPAddress -ne "127.0.0.1" -and
            $_.IPAddress -notlike "169.254.*"
        } |
        Select-Object -First 1
    if (-not $address -or [string]::IsNullOrWhiteSpace([string]$address.IPAddress)) {
        throw "未找到当前默认网络接口的局域网 IPv4 地址。"
    }
    return [pscustomobject]@{
        IPAddress = [string]$address.IPAddress
        PrefixLength = [int]$address.PrefixLength
        InterfaceIndex = [int]$defaultRoute.InterfaceIndex
        InterfaceAlias = [string]$defaultRoute.InterfaceAlias
    }
}

function Ensure-EamLanFirewallRule {
    param([int]$Port = 8766)

    $displayName = "EAM-Lite 开发环境局域网扫码 $Port"
    try {
        $existing = Get-NetFirewallRule -DisplayName $displayName -ErrorAction SilentlyContinue
        if (-not $existing) {
            $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
            $principal = New-Object System.Security.Principal.WindowsPrincipal($identity)
            $isAdministrator = $principal.IsInRole(
                [System.Security.Principal.WindowsBuiltInRole]::Administrator
            )
            if (-not $isAdministrator) {
                Write-Warning "当前窗口没有管理员权限，未添加专用防火墙规则。Docker Desktop 已允许时手机仍可直接访问；否则请以管理员身份运行本入口。"
                return $false
            }
            New-NetFirewallRule `
                -DisplayName $displayName `
                -Direction Inbound `
                -Action Allow `
                -Protocol TCP `
                -LocalPort $Port `
                -RemoteAddress LocalSubnet `
                -Profile Any `
                -ErrorAction Stop | Out-Null
        }
        return $true
    }
    catch {
        Write-Warning "无法自动开放 Windows 防火墙端口 $Port。若手机无法访问，请以管理员身份运行本入口，或手工允许 TCP $Port。"
        return $false
    }
}

function Get-EamComposeContext {
    param(
        [ValidateSet("local", "dev")][string]$Mode,
        [Parameter(Mandatory = $true)]$State,
        [Parameter(Mandatory = $true)][string]$RepositoryRoot
    )

    $project = if ($Mode -eq "local") { $script:StableProject } else { $script:DevelopmentProject }
    $composeFile = if ($Mode -eq "local") { "compose.local.yaml" } else { "compose.dev.yaml" }
    return [pscustomobject]@{
        Mode = $Mode
        Project = $project
        RepositoryRoot = $RepositoryRoot
        EnvFile = $State.EnvFile
        ComposeFile = Join-Path $RepositoryRoot ("deploy\" + $composeFile)
        Url = if ($Mode -eq "local") { $script:StableUrl } else { $script:DevelopmentUrl }
    }
}

function Invoke-EamCompose {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [switch]$AllowFailure
    )

    $base = @(
        "compose",
        "--project-name", $Context.Project,
        "--env-file", $Context.EnvFile,
        "--file", $Context.ComposeFile
    )
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = & docker.exe @base @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($exitCode -ne 0 -and -not $AllowFailure) {
        throw "Docker Compose 操作失败：$($output -join [Environment]::NewLine)"
    }
    return [pscustomobject]@{ ExitCode = $exitCode; Output = @($output) }
}

function Invoke-EamComposeInteractive {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    $base = @(
        "compose",
        "--project-name", $Context.Project,
        "--env-file", $Context.EnvFile,
        "--file", $Context.ComposeFile
    )
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & docker.exe @base @Arguments
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($exitCode -ne 0) {
        throw "交互式 Docker Compose 操作失败，退出码 $exitCode。"
    }
}

function Pull-EamImage {
    param([Parameter(Mandatory = $true)][string]$Image)

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & docker.exe pull $Image
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($exitCode -ne 0) {
        throw "镜像下载失败：$Image"
    }
}

function Build-EamImage {
    param(
        [Parameter(Mandatory = $true)][string]$RepositoryRoot,
        [Parameter(Mandatory = $true)]$Identity,
        [switch]$Development
    )

    if ($Development) {
        & docker.exe image inspect $Identity.AppImage *> $null
        if ($LASTEXITCODE -eq 0) {
            return
        }
    }
    else {
        $inspect = & docker.exe image inspect $Identity.AppImage --format "{{ index .Config.Labels `"org.opencontainers.image.revision`" }}" 2>$null
        if ($LASTEXITCODE -eq 0 -and ($inspect -join "").Trim() -eq $Identity.Commit) {
            return
        }
    }
    Write-Host "正在构建与 commit $($Identity.Commit.Substring(0, [Math]::Min(12, $Identity.Commit.Length))) 一致的应用镜像……" -ForegroundColor Cyan
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & docker.exe build `
            --file (Join-Path $RepositoryRoot "deploy\Dockerfile") `
            --tag $Identity.AppImage `
            --build-arg "APP_VERSION=$($Identity.Version)" `
            --build-arg "BUILD_COMMIT=$($Identity.Commit)" `
            --build-arg "BUILD_TIME=$($Identity.BuildTime)" `
            $RepositoryRoot
        $buildExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($buildExitCode -ne 0) {
        throw "应用镜像构建失败。"
    }
}

function Get-EamVersionPayload {
    param([Parameter(Mandatory = $true)][string]$Url)

    try {
        return Invoke-RestMethod -Uri ($Url.TrimEnd('/') + "/version/") -TimeoutSec 3
    }
    catch {
        return $null
    }
}

function Test-EamHealth {
    param([Parameter(Mandatory = $true)][string]$Url)

    try {
        $result = Invoke-RestMethod -Uri ($Url.TrimEnd('/') + "/healthz/") -TimeoutSec 3
        return $result.status -eq "ok"
    }
    catch {
        return $false
    }
}

function Wait-EamHealth {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$ExpectedCommit,
        [int]$TimeoutSeconds = 120
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        if (Test-EamHealth -Url $Url) {
            $version = Get-EamVersionPayload -Url $Url
            if ($version -and $version.commit -eq $ExpectedCommit) {
                return $version
            }
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    throw "服务未在 $TimeoutSeconds 秒内通过健康检查和版本一致性检查。"
}

function Get-EamPortOwner {
    param([Parameter(Mandatory = $true)][int]$Port)

    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $listener) {
        return $null
    }
    $process = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
    return [pscustomobject]@{
        Pid = $listener.OwningProcess
        ProcessName = if ($process) { $process.ProcessName } else { "未知进程" }
    }
}

function Assert-EamPortAvailable {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$ExpectedEnvironment
    )

    $owner = Get-EamPortOwner -Port $Port
    if (-not $owner) {
        return
    }
    $version = Get-EamVersionPayload -Url $Url
    if ($version -and $version.environment -eq $ExpectedEnvironment) {
        return
    }
    throw "端口 $Port 已被 $($owner.ProcessName)（PID $($owner.Pid)）占用。脚本不会自动结束该进程。"
}

function Open-EamBrowser {
    param([Parameter(Mandatory = $true)][string]$Url)
    Start-Process -FilePath $Url | Out-Null
}

function Save-EamReleaseMarker {
    param(
        [Parameter(Mandatory = $true)]$State,
        [Parameter(Mandatory = $true)]$Identity
    )

    $payload = [ordered]@{
        version = $Identity.Version
        commit = $Identity.Commit
        image = $Identity.AppImage
        released_at = (Get-Date).ToUniversalTime().ToString("o")
    } | ConvertTo-Json
    Write-EamRestrictedText -Path $State.ReleaseMarker -Value $payload
}

function Test-EamReleaseMarker {
    param(
        [Parameter(Mandatory = $true)]$State,
        [Parameter(Mandatory = $true)]$Identity
    )

    if (-not (Test-Path -LiteralPath $State.ReleaseMarker -PathType Leaf)) {
        return $false
    }
    try {
        $marker = Get-Content -LiteralPath $State.ReleaseMarker -Raw -Encoding UTF8 | ConvertFrom-Json
        return $marker.commit -eq $Identity.Commit -and $marker.image -eq $Identity.AppImage
    }
    catch {
        return $false
    }
}

function New-EamPassphraseFile {
    param(
        [Parameter(Mandatory = $true)]$State,
        [Parameter(Mandatory = $true)][Security.SecureString]$Passphrase
    )

    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Passphrase)
    try {
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
        $path = Join-Path $State.Temporary ("migration-passphrase-" + [guid]::NewGuid().ToString("N") + ".txt")
        Write-EamRestrictedText -Path $path -Value $plain
        return $path
    }
    finally {
        if ($pointer -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        }
        $plain = $null
    }
}

function Test-EamSecureStringsEqual {
    param(
        [Parameter(Mandatory = $true)][Security.SecureString]$First,
        [Parameter(Mandatory = $true)][Security.SecureString]$Second
    )

    $firstPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($First)
    $secondPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Second)
    try {
        $firstText = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($firstPointer)
        $secondText = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secondPointer)
        return ($firstText.Length -ge 12 -and $firstText -ceq $secondText)
    }
    finally {
        if ($firstPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($firstPointer) }
        if ($secondPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secondPointer) }
        $firstText = $null
        $secondText = $null
    }
}

function Remove-EamPassphraseFile {
    param(
        [Parameter(Mandatory = $true)]$State,
        [Parameter(Mandatory = $true)][string]$Path
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return
    }
    $resolvedRoot = [System.IO.Path]::GetFullPath($State.Temporary).TrimEnd('\') + '\'
    $resolvedPath = [System.IO.Path]::GetFullPath($Path)
    if (-not $resolvedPath.StartsWith($resolvedRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝清理不在 EAM-Lite 临时目录中的口令文件。"
    }
    try {
        $length = (Get-Item -LiteralPath $resolvedPath).Length
        if ($length -gt 0 -and $length -le 4096) {
            [System.IO.File]::WriteAllBytes($resolvedPath, (New-Object byte[] $length))
        }
    }
    finally {
        Remove-Item -LiteralPath $resolvedPath -Force
    }
}

function Set-EamTemporaryComposeValue {
    param(
        [Parameter(Mandatory = $true)]$State,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Value
    )

    $lines = Get-Content -LiteralPath $State.EnvFile -Encoding UTF8
    $prefix = $Name + "="
    $replacement = $Name + "=`"" + (ConvertTo-EamComposePath $Value) + "`""
    $found = $false
    $updated = foreach ($line in $lines) {
        if ($line.StartsWith($prefix, [StringComparison]::Ordinal)) {
            $found = $true
            $replacement
        }
        else {
            $line
        }
    }
    if (-not $found) {
        $updated += $replacement
    }
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($State.EnvFile, $updated, $encoding)
    Protect-EamFileForCurrentUser -Path $State.EnvFile
}

function Invoke-EamCurrentReleaseDelegation {
    param(
        [Parameter(Mandatory = $true)][string]$RepositoryRoot,
        [Parameter(Mandatory = $true)][string]$ScriptName,
        [string[]]$ForwardArguments = @()
    )

    if (Test-EamGitRepository -RepositoryRoot $RepositoryRoot) {
        return $false
    }
    $state = Initialize-EamState -Mode local
    if (-not (Test-Path -LiteralPath $state.CurrentReleasePointer -PathType Leaf)) {
        return $false
    }
    $currentRoot = (Get-Content -LiteralPath $state.CurrentReleasePointer -Raw -Encoding UTF8).Trim()
    if ([string]::IsNullOrWhiteSpace($currentRoot)) {
        return $false
    }
    $resolvedCurrent = [System.IO.Path]::GetFullPath($currentRoot)
    $resolvedHere = [System.IO.Path]::GetFullPath($RepositoryRoot)
    if ($resolvedCurrent -eq $resolvedHere) {
        return $false
    }
    $target = Join-Path $resolvedCurrent ("scripts\local\" + $ScriptName)
    if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
        throw "当前版本指针无效：$target"
    }
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $target @ForwardArguments
    $script:EamDelegatedExitCode = $LASTEXITCODE
    return $true
}
