[CmdletBinding()]
param()

. (Join-Path $PSScriptRoot "common.ps1")

try {
    Assert-EamWindows
    $repositoryRoot = Get-EamRepositoryRoot
    Ensure-EamDockerReady | Out-Null
    $state = Initialize-EamState -Mode dev
    if (-not (Test-Path -LiteralPath $state.EnvFile -PathType Leaf)) {
        Write-Host "尚未发现开发环境配置，无需停止。"
        exit 0
    }
    $context = Get-EamComposeContext -Mode dev -State $state -RepositoryRoot $repositoryRoot
    Invoke-EamCompose -Context $context -Arguments @("stop") | Out-Null
    Write-Host "开发环境已停止；稳定使用版和双方数据卷均未受影响。" -ForegroundColor Green
    exit 0
}
catch {
    Write-Host "开发环境停止失败：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
