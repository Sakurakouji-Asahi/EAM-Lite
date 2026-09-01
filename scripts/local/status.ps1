[CmdletBinding()]
param()

. (Join-Path $PSScriptRoot "common.ps1")

try {
    Assert-EamWindows
    $repositoryRoot = Get-EamRepositoryRoot
    if (Invoke-EamCurrentReleaseDelegation -RepositoryRoot $repositoryRoot -ScriptName "status.ps1") {
        exit $script:EamDelegatedExitCode
    }
    $stateRoot = Get-EamStateRoot -Mode local
    $envFile = Join-Path $stateRoot "compose.env"
    if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) {
        Write-Host "本机稳定使用版尚未初始化。"
        exit 0
    }
    Ensure-EamDockerReady | Out-Null
    $state = Initialize-EamState -Mode local
    $context = Get-EamComposeContext -Mode local -State $state -RepositoryRoot $repositoryRoot
    Write-Host "稳定使用版" -ForegroundColor Cyan
    Write-Host "  地址：$($context.Url)"
    Write-Host "  Compose project：$($context.Project)"
    Write-Host "  PostgreSQL volume：$($context.Project)_postgres_data"
    Write-Host "  Media volume：$($context.Project)_media_data"
    Write-Host "  Backup stage：$($context.Project)_backup_stage"
    $version = Get-EamVersionPayload -Url $context.Url
    if ($version -and (Test-EamHealth -Url $context.Url)) {
        Write-Host "  状态：运行正常" -ForegroundColor Green
        Write-Host "  版本：$($version.version)"
        Write-Host "  Commit：$($version.commit)"
        Write-Host "  环境：$($version.environment)"
        Write-Host "  数据库：$($version.database_vendor)"
    }
    else {
        Write-Host "  状态：未运行或健康检查未通过" -ForegroundColor Yellow
    }
    $composeStatus = Invoke-EamCompose -Context $context -Arguments @("ps", "--all") -AllowFailure
    if ($composeStatus.Output.Count -gt 0) {
        Write-Host ""
        $composeStatus.Output | ForEach-Object { Write-Host $_ }
    }
    exit 0
}
catch {
    Write-Host "状态检查失败：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
