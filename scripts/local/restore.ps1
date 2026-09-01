[CmdletBinding()]
param([string]$BackupFile, [switch]$NoBrowser)

. (Join-Path $PSScriptRoot "common.ps1")

$passphraseFile = $null
$state = $null
try {
    Assert-EamWindows
    $repositoryRoot = Get-EamRepositoryRoot
    $forward = @()
    if ($BackupFile) { $forward += @("-BackupFile", $BackupFile) }
    if ($NoBrowser) { $forward += "-NoBrowser" }
    if (Invoke-EamCurrentReleaseDelegation -RepositoryRoot $repositoryRoot -ScriptName "restore.ps1" -ForwardArguments $forward) {
        exit $script:EamDelegatedExitCode
    }
    Ensure-EamDockerReady | Out-Null
    $identity = Get-EamStableIdentity -RepositoryRoot $repositoryRoot
    $state = Initialize-EamState -Mode local
    Write-EamComposeEnvironment -State $state -Identity $identity
    $context = Get-EamComposeContext -Mode local -State $state -RepositoryRoot $repositoryRoot

    if (Test-EamHealth -Url $context.Url) {
        throw "当前稳定实例正在运行。恢复只允许全新空实例；请在新电脑上执行，或先另建空的本机实例。"
    }
    if (-not $BackupFile) {
        Add-Type -AssemblyName System.Windows.Forms
        $dialog = New-Object System.Windows.Forms.OpenFileDialog
        $dialog.Title = "选择 EAM-Lite 便携备份"
        $dialog.Filter = "EAM-Lite 便携备份 (*.eambak)|*.eambak"
        $dialog.InitialDirectory = $state.BackupOutput
        if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
            Write-Host "已取消恢复。"
            exit 0
        }
        $BackupFile = $dialog.FileName
    }
    $BackupFile = [System.IO.Path]::GetFullPath($BackupFile)
    if (-not (Test-Path -LiteralPath $BackupFile -PathType Leaf) -or [System.IO.Path]::GetExtension($BackupFile) -ne ".eambak") {
        throw "请选择有效的 .eambak 文件。"
    }

    $first = Read-Host "迁移密码" -AsSecureString
    $second = Read-Host "再次输入迁移密码" -AsSecureString
    if (-not (Test-EamSecureStringsEqual -First $first -Second $second)) {
        throw "两次迁移密码不一致，或密码少于 12 个字符。"
    }
    $passphraseFile = New-EamPassphraseFile -State $state -Passphrase $first
    Set-EamTemporaryComposeValue -State $state -Name "EAM_PORTABLE_PASSPHRASE_FILE" -Value $passphraseFile
    Set-EamTemporaryComposeValue -State $state -Name "EAM_PORTABLE_BACKUP_FILE" -Value $BackupFile

    if ($identity.Kind -eq "git") {
        Build-EamImage -RepositoryRoot $repositoryRoot -Identity $identity
    }
    else {
        & docker.exe pull $identity.AppImage
        if ($LASTEXITCODE -ne 0) { throw "Release 镜像下载失败。" }
    }
    Invoke-EamCompose -Context $context -Arguments @("stop", "app", "caddy") -AllowFailure | Out-Null
    Invoke-EamCompose -Context $context -Arguments @("up", "--detach", "--wait", "db") | Out-Null
    Write-Host "正在验证加密包并恢复到空 PostgreSQL 与空附件卷……" -ForegroundColor Cyan
    $restoreResult = Invoke-EamCompose -Context $context -Arguments @("--profile", "restore", "run", "--rm", "restore")
    $restoreResult.Output | ForEach-Object { Write-Host $_ }

    Write-Host "正在应用当前版本迁移并执行库存/保管重算核对……" -ForegroundColor Cyan
    Invoke-EamCompose -Context $context -Arguments @("--profile", "release", "run", "--rm", "release") | Out-Null
    Invoke-EamCompose -Context $context -Arguments @("--profile", "release", "run", "--rm", "release", "python", "manage.py", "fail_stale_eam_backups", "--restored-snapshot") | Out-Null
    Invoke-EamCompose -Context $context -Arguments @("--profile", "release", "run", "--rm", "release", "python", "manage.py", "reconcile_supply_balances") | Out-Null
    Invoke-EamCompose -Context $context -Arguments @("--profile", "release", "run", "--rm", "release", "python", "manage.py", "reconcile_supply_custodies") | Out-Null
    Invoke-EamCompose -Context $context -Arguments @("up", "--detach", "--wait", "app", "caddy") | Out-Null
    $version = Wait-EamHealth -Url $context.Url -ExpectedCommit $identity.Commit
    $login = Invoke-WebRequest -Uri ($context.Url + "/login/") -UseBasicParsing -TimeoutSec 5
    if ($login.StatusCode -ne 200) { throw "恢复后登录页健康检查失败。" }
    Save-EamReleaseMarker -State $state -Identity $identity
    Write-Host "恢复完成：数据库、附件、关键数量与哈希均已验证。" -ForegroundColor Green
    Write-Host "二维码 Token 未被改写；如曾使用固定 QR_BASE_URL，请另行核对主机名。" -ForegroundColor Yellow
    if (-not $NoBrowser) { Open-EamBrowser -Url $context.Url }
    exit 0
}
catch {
    Write-Host "恢复失败：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host "脚本没有覆盖其他实例，也没有删除任何卷。若恢复过程已开始，请保留当前目标以供诊断，并改用新的空实例重试。" -ForegroundColor Yellow
    exit 1
}
finally {
    if ($passphraseFile -and $state) {
        try { Remove-EamPassphraseFile -State $state -Path $passphraseFile } catch { Write-Warning $_.Exception.Message }
    }
    if ($state) {
        try {
            Set-EamTemporaryComposeValue -State $state -Name "EAM_PORTABLE_PASSPHRASE_FILE" -Value $state.PlaceholderPassphrase
            Set-EamTemporaryComposeValue -State $state -Name "EAM_PORTABLE_BACKUP_FILE" -Value $state.EmptyPackage
        }
        catch { }
    }
}
