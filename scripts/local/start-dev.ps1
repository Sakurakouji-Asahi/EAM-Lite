[CmdletBinding()]
param([switch]$NoBrowser)

. (Join-Path $PSScriptRoot "common.ps1")

try {
    Assert-EamWindows
    $repositoryRoot = Get-EamRepositoryRoot
    Ensure-EamDockerReady | Out-Null
    $identity = Get-EamDevelopmentIdentity -RepositoryRoot $repositoryRoot
    $state = Initialize-EamState -Mode dev
    Write-EamComposeEnvironment -State $state -Identity $identity
    $context = Get-EamComposeContext -Mode dev -State $state -RepositoryRoot $repositoryRoot
    Assert-EamPortAvailable -Port 8766 -Url $context.Url -ExpectedEnvironment "development"
    Build-EamImage -RepositoryRoot $repositoryRoot -Identity $identity -Development
    Invoke-EamCompose -Context $context -Arguments @("up", "--detach", "--wait", "db") | Out-Null
    Invoke-EamCompose -Context $context -Arguments @("--profile", "release", "run", "--rm", "release") | Out-Null
    Invoke-EamComposeInteractive -Context $context -Arguments @("--profile", "release", "run", "--rm", "release", "python", "manage.py", "bootstrap_local_admin")
    Invoke-EamCompose -Context $context -Arguments @("up", "--detach", "app") | Out-Null
    $version = Wait-EamHealth -Url $context.Url -ExpectedCommit $identity.Commit
    Write-Host "开发环境已就绪：$($context.Url)" -ForegroundColor Yellow
    Write-Host "页面顶部将持续显示【开发环境】；数据库、附件和端口均不与稳定版共享。" -ForegroundColor Yellow
    if (-not $NoBrowser) { Open-EamBrowser -Url $context.Url }
    exit 0
}
catch {
    Write-Host "开发环境启动失败：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
