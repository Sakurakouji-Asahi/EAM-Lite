[CmdletBinding()]
param([switch]$NoBrowser)

. (Join-Path $PSScriptRoot "common.ps1")

try {
    Assert-EamWindows
    $repositoryRoot = Get-EamRepositoryRoot
    if (Invoke-EamCurrentReleaseDelegation -RepositoryRoot $repositoryRoot -ScriptName "start.ps1" -ForwardArguments $(if ($NoBrowser) { @("-NoBrowser") } else { @() })) {
        exit $script:EamDelegatedExitCode
    }
    Ensure-EamDockerReady | Out-Null
    $identity = Get-EamStableIdentity -RepositoryRoot $repositoryRoot
    $state = Initialize-EamState -Mode local
    Write-EamComposeEnvironment -State $state -Identity $identity
    $context = Get-EamComposeContext -Mode local -State $state -RepositoryRoot $repositoryRoot

    $runningVersion = Get-EamVersionPayload -Url $context.Url
    if ((Test-EamHealth -Url $context.Url) -and $runningVersion -and $runningVersion.commit -eq $identity.Commit) {
        Write-Host "EAM-Lite 已在运行，版本一致。" -ForegroundColor Green
        if (-not $NoBrowser) { Open-EamBrowser -Url $context.Url }
        exit 0
    }

    Assert-EamPortAvailable -Port 8765 -Url $context.Url -ExpectedEnvironment "local"
    if ($identity.Kind -eq "git") {
        Build-EamImage -RepositoryRoot $repositoryRoot -Identity $identity
    }
    else {
        Write-Host "正在获取 Release 精确镜像（含 digest）……" -ForegroundColor Cyan
        & docker.exe pull $identity.AppImage
        if ($LASTEXITCODE -ne 0) { throw "Release 镜像下载失败。请检查网络后重试。" }
    }

    Invoke-EamCompose -Context $context -Arguments @("up", "--detach", "--wait", "db") | Out-Null
    $needsRelease = -not (Test-EamReleaseMarker -State $state -Identity $identity)
    if (-not $needsRelease) {
        $check = Invoke-EamCompose -Context $context -Arguments @("--profile", "release", "run", "--rm", "release", "python", "manage.py", "migrate", "--check") -AllowFailure
        $needsRelease = $check.ExitCode -ne 0
    }
    if ($needsRelease) {
        Write-Host "正在执行单一 release 步骤（迁移、静态文件、运行权限）……" -ForegroundColor Cyan
        Invoke-EamCompose -Context $context -Arguments @("--profile", "release", "run", "--rm", "release") | Out-Null
    }

    Write-Host "正在检查首次管理员初始化……" -ForegroundColor Cyan
    Invoke-EamComposeInteractive -Context $context -Arguments @("--profile", "release", "run", "--rm", "release", "python", "manage.py", "bootstrap_local_admin")
    Invoke-EamCompose -Context $context -Arguments @("up", "--detach", "--wait", "app", "caddy") | Out-Null
    $version = Wait-EamHealth -Url $context.Url -ExpectedCommit $identity.Commit
    Save-EamReleaseMarker -State $state -Identity $identity
    Write-Host "EAM-Lite 已就绪：$($context.Url)" -ForegroundColor Green
    Write-Host "版本 $($version.version)，commit $($version.commit.Substring(0, 12))，数据库 $($version.database_vendor)。" -ForegroundColor DarkGray
    if (-not $NoBrowser) { Open-EamBrowser -Url $context.Url }
    exit 0
}
catch {
    Write-Host "启动失败：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
