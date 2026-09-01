[CmdletBinding()]
param([string]$OutputDirectory)

. (Join-Path $PSScriptRoot "common.ps1")

$passphraseFile = $null
$state = $null
try {
    Assert-EamWindows
    $repositoryRoot = Get-EamRepositoryRoot
    $forward = if ($OutputDirectory) { @("-OutputDirectory", $OutputDirectory) } else { @() }
    if (Invoke-EamCurrentReleaseDelegation -RepositoryRoot $repositoryRoot -ScriptName "backup.ps1" -ForwardArguments $forward) {
        exit $script:EamDelegatedExitCode
    }
    Ensure-EamDockerReady | Out-Null
    $identity = Get-EamStableIdentity -RepositoryRoot $repositoryRoot
    $state = Initialize-EamState -Mode local
    if ($OutputDirectory) {
        $state.BackupOutput = [System.IO.Path]::GetFullPath($OutputDirectory)
        New-Item -ItemType Directory -Path $state.BackupOutput -Force | Out-Null
    }
    Write-EamComposeEnvironment -State $state -Identity $identity
    $context = Get-EamComposeContext -Mode local -State $state -RepositoryRoot $repositoryRoot
    $running = Get-EamVersionPayload -Url $context.Url
    if (-not (Test-EamHealth -Url $context.Url) -or -not $running -or $running.commit -ne $identity.Commit) {
        throw "稳定使用版尚未以当前精确 commit 健康运行，请先执行【启动EAM-Lite.cmd】。"
    }

    Write-Host "请输入用于跨电脑迁移的密码（至少 12 个字符）。丢失后无法恢复。" -ForegroundColor Yellow
    $first = Read-Host "迁移密码" -AsSecureString
    $second = Read-Host "再次输入迁移密码" -AsSecureString
    if (-not (Test-EamSecureStringsEqual -First $first -Second $second)) {
        throw "两次迁移密码不一致，或密码少于 12 个字符。"
    }
    $passphraseFile = New-EamPassphraseFile -State $state -Passphrase $first
    Set-EamTemporaryComposeValue -State $state -Name "EAM_PORTABLE_PASSPHRASE_FILE" -Value $passphraseFile
    Set-EamTemporaryComposeValue -State $state -Name "EAM_PORTABLE_OUTPUT_DIR" -Value $state.BackupOutput

    Write-Host "正在生成并验证数据库、附件一体化加密便携包……" -ForegroundColor Cyan
    $result = Invoke-EamCompose -Context $context -Arguments @("--profile", "portable", "run", "--rm", "portable")
    $jsonLine = $result.Output | Where-Object { $_ -match 'PORTABLE_BACKUP_JSON=' } | Select-Object -Last 1
    if (-not $jsonLine) {
        throw "备份命令未返回可验证的便携包结果。"
    }
    $summary = (($jsonLine -split 'PORTABLE_BACKUP_JSON=', 2)[1] | ConvertFrom-Json)
    Write-EamRestrictedText -Path $state.LastPortableBackup -Value ($summary | ConvertTo-Json -Depth 8)
    Write-Host "便携备份已完成。" -ForegroundColor Green
    Write-Host "  文件：$($summary.path)"
    Write-Host "  大小：$($summary.size) 字节"
    Write-Host "  SHA-256：$($summary.sha256)"
    Write-Host "  版本：$($summary.version) · commit $($summary.commit)"
    exit 0
}
catch {
    Write-Host "备份失败：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
finally {
    if ($passphraseFile) {
        try { Remove-EamPassphraseFile -State $state -Path $passphraseFile } catch { Write-Warning $_.Exception.Message }
    }
    if ($state) {
        try {
            Set-EamTemporaryComposeValue -State $state -Name "EAM_PORTABLE_PASSPHRASE_FILE" -Value $state.PlaceholderPassphrase
        }
        catch { }
    }
}
