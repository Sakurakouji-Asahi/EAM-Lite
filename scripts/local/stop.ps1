[CmdletBinding()]
param()

. (Join-Path $PSScriptRoot "common.ps1")

try {
    Assert-EamWindows
    $repositoryRoot = Get-EamRepositoryRoot
    if (Invoke-EamCurrentReleaseDelegation -RepositoryRoot $repositoryRoot -ScriptName "stop.ps1") {
        exit $script:EamDelegatedExitCode
    }
    Ensure-EamDockerReady | Out-Null
    $state = Initialize-EamState -Mode local
    if (-not (Test-Path -LiteralPath $state.EnvFile -PathType Leaf)) {
        Write-Host "尚未发现本机稳定使用版配置，无需停止。"
        exit 0
    }
    $context = Get-EamComposeContext -Mode local -State $state -RepositoryRoot $repositoryRoot
    Invoke-EamCompose -Context $context -Arguments @("stop") | Out-Null
    Write-Host "EAM-Lite 本机稳定使用版已停止；数据库和附件卷均已保留。" -ForegroundColor Green
    exit 0
}
catch {
    Write-Host "停止失败：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
