[CmdletBinding()]
param([switch]$NoBrowser)

. (Join-Path $PSScriptRoot "common.ps1")

try {
    Assert-EamWindows
    $repositoryRoot = Get-EamRepositoryRoot
    Ensure-EamDockerReady | Out-Null
    $lanAddress = Get-EamPrimaryLanAddress
    $lanUrl = "http://$($lanAddress.IPAddress):8766"
    $identity = Get-EamDevelopmentIdentity -RepositoryRoot $repositoryRoot
    $state = Initialize-EamState -Mode dev
    Write-EamComposeEnvironment `
        -State $state `
        -Identity $identity `
        -DevelopmentLanAddress $lanAddress.IPAddress
    $context = Get-EamComposeContext -Mode dev -State $state -RepositoryRoot $repositoryRoot
    Assert-EamPortAvailable -Port 8766 -Url $context.Url -ExpectedEnvironment "development"
    Build-EamImage -RepositoryRoot $repositoryRoot -Identity $identity -Development
    Invoke-EamCompose -Context $context -Arguments @("up", "--detach", "--wait", "db") | Out-Null
    Invoke-EamCompose -Context $context -Arguments @("--profile", "release", "run", "--rm", "release") | Out-Null
    Invoke-EamComposeInteractive -Context $context -Arguments @("--profile", "release", "run", "--rm", "release", "python", "manage.py", "bootstrap_local_admin")
    Ensure-EamLanFirewallRule -Port 8766 | Out-Null
    Invoke-EamCompose -Context $context -Arguments @("up", "--detach", "app") | Out-Null
    $version = Wait-EamHealth -Url $context.Url -ExpectedCommit $identity.Commit
    Write-Host "开发环境局域网扫码测试已就绪。" -ForegroundColor Yellow
    Write-Host "电脑访问：$($context.Url)" -ForegroundColor Yellow
    Write-Host "手机访问：$lanUrl（手机须连接同一局域网）" -ForegroundColor Green
    Write-Host "本模式使用 HTTP；手机系统相机扫码可测试，网页直接调用摄像头可能要求 HTTPS。" -ForegroundColor Yellow
    Write-Host "页面顶部持续显示【开发环境】，数据库和稳定使用版完全隔离。" -ForegroundColor Yellow
    if (-not $NoBrowser) { Open-EamBrowser -Url $context.Url }
    exit 0
}
catch {
    Write-Host "局域网扫码测试环境启动失败：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
