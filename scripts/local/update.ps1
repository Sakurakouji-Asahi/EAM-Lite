[CmdletBinding()]
param([switch]$NoBrowser)

. (Join-Path $PSScriptRoot "common.ps1")

function Invoke-PreUpdateBackup {
    param([Parameter(Mandatory = $true)][string]$RepositoryRoot)

    $backupScript = Join-Path $RepositoryRoot "scripts\local\backup.ps1"
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $backupScript
    if ($LASTEXITCODE -ne 0) {
        throw "更新前便携备份失败，已取消更新。"
    }
}

function Save-UpdateLog {
    param(
        [Parameter(Mandatory = $true)]$State,
        [Parameter(Mandatory = $true)]$Payload
    )

    $name = "update-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".json"
    $path = Join-Path $State.Logs $name
    Write-EamRestrictedText -Path $path -Value ($Payload | ConvertTo-Json -Depth 12)
    return $path
}

try {
    Assert-EamWindows
    $repositoryRoot = Get-EamRepositoryRoot
    $forward = if ($NoBrowser) { @("-NoBrowser") } else { @() }
    if (Invoke-EamCurrentReleaseDelegation -RepositoryRoot $repositoryRoot -ScriptName "update.ps1" -ForwardArguments $forward) {
        exit $script:EamDelegatedExitCode
    }
    Ensure-EamDockerReady | Out-Null
    $identityBefore = Get-EamStableIdentity -RepositoryRoot $repositoryRoot
    $state = Initialize-EamState -Mode local
    Write-EamComposeEnvironment -State $state -Identity $identityBefore
    $context = Get-EamComposeContext -Mode local -State $state -RepositoryRoot $repositoryRoot

    Invoke-PreUpdateBackup -RepositoryRoot $repositoryRoot
    $backupSummary = if (Test-Path -LiteralPath $state.LastPortableBackup -PathType Leaf) {
        Get-Content -LiteralPath $state.LastPortableBackup -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    else { $null }
    $migrationBefore = Invoke-EamCompose -Context $context -Arguments @("--profile", "release", "run", "--rm", "release", "python", "manage.py", "showmigrations", "--plan") -AllowFailure

    if ($identityBefore.Kind -eq "git") {
        $branch = ((Invoke-EamGit -RepositoryRoot $repositoryRoot -Arguments @("branch", "--show-current")).Output -join "").Trim()
        if ($branch -ne "main") {
            throw "Git clone 更新只允许在干净的 main 分支执行。"
        }
        Write-Host "正在获取远端更新（只允许 fast-forward）……" -ForegroundColor Cyan
        Invoke-EamGit -RepositoryRoot $repositoryRoot -Arguments @("fetch", "--all", "--prune", "--tags") | Out-Null
        $newCommit = ((Invoke-EamGit -RepositoryRoot $repositoryRoot -Arguments @("rev-parse", "origin/main")).Output -join "").Trim()
        if ($newCommit -eq $identityBefore.Commit) {
            Write-Host "当前已经是 origin/main 最新版本；更新前备份仍已保留。" -ForegroundColor Green
            if (-not $NoBrowser) { Open-EamBrowser -Url $context.Url }
            exit 0
        }
        $ancestor = Invoke-EamGit -RepositoryRoot $repositoryRoot -Arguments @("merge-base", "--is-ancestor", $identityBefore.Commit, $newCommit) -AllowFailure
        if ($ancestor.ExitCode -ne 0) {
            throw "origin/main 不能从当前版本 fast-forward，已拒绝自动更新。"
        }
        Invoke-EamGit -RepositoryRoot $repositoryRoot -Arguments @("pull", "--ff-only", "origin", "main") | Out-Null
        $startScript = Join-Path $repositoryRoot "scripts\local\start.ps1"
        & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $startScript -NoBrowser
        if ($LASTEXITCODE -ne 0) {
            $failedLog = Save-UpdateLog -State $state -Payload ([ordered]@{
                status = "failed"
                source = "git"
                old_commit = $identityBefore.Commit
                new_commit = $newCommit
                backup = $backupSummary
                migration_status_before = $migrationBefore.Output
                message = "新版本启动或健康检查失败；数据库、备份和旧镜像均已保留。"
            })
            throw "新版本启动失败。恢复信息已保存：$failedLog"
        }
        $identityAfter = Get-EamStableIdentity -RepositoryRoot $repositoryRoot
        $state = Initialize-EamState -Mode local
        Write-EamComposeEnvironment -State $state -Identity $identityAfter
        $context = Get-EamComposeContext -Mode local -State $state -RepositoryRoot $repositoryRoot
        $migrationAfter = Invoke-EamCompose -Context $context -Arguments @("--profile", "release", "run", "--rm", "release", "python", "manage.py", "showmigrations", "--plan") -AllowFailure
        $version = Wait-EamHealth -Url $context.Url -ExpectedCommit $identityAfter.Commit
        $logPath = Save-UpdateLog -State $state -Payload ([ordered]@{
            status = "completed"
            source = "git"
            old_commit = $identityBefore.Commit
            new_commit = $identityAfter.Commit
            backup = $backupSummary
            migration_status_before = $migrationBefore.Output
            migration_status_after = $migrationAfter.Output
            completed_at = (Get-Date).ToUniversalTime().ToString("o")
        })
        Write-Host "更新完成：$($identityBefore.Commit.Substring(0, 12)) → $($identityAfter.Commit.Substring(0, 12))" -ForegroundColor Green
        Write-Host "更新记录：$logPath"
        if (-not $NoBrowser) { Open-EamBrowser -Url $context.Url }
        exit 0
    }

    if ($identityBefore.Repository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') {
        throw "Release 清单 repository 格式非法。"
    }
    Write-Host "正在检查 GitHub 最新正式 Release……" -ForegroundColor Cyan
    $headers = @{ "User-Agent" = "EAM-Lite-Windows-Updater"; "Accept" = "application/vnd.github+json" }
    $latest = Invoke-RestMethod -Uri ("https://api.github.com/repos/" + $identityBefore.Repository + "/releases/latest") -Headers $headers -TimeoutSec 20
    if ($latest.draft -or $latest.prerelease) {
        throw "GitHub latest 不是正式 Release，已拒绝更新。"
    }
    $latestVersion = ([string]$latest.tag_name).TrimStart("v")
    if ($latestVersion -eq $identityBefore.Version) {
        Write-Host "当前已是最新正式 Release；更新前备份仍已保留。" -ForegroundColor Green
        if (-not $NoBrowser) { Open-EamBrowser -Url $context.Url }
        exit 0
    }
    $zipName = "EAM-Lite-v$latestVersion-Windows.zip"
    $hashName = $zipName + ".sha256"
    $zipAsset = $latest.assets | Where-Object { $_.name -eq $zipName } | Select-Object -First 1
    $hashAsset = $latest.assets | Where-Object { $_.name -eq $hashName } | Select-Object -First 1
    if (-not $zipAsset -or -not $hashAsset) {
        throw "最新 Release 缺少 Windows ZIP 或 SHA-256 文件。"
    }
    $downloadToken = [guid]::NewGuid().ToString("N")
    $zipPath = Join-Path $state.Temporary ("update-" + $downloadToken + ".zip")
    $hashPath = Join-Path $state.Temporary ("update-" + $downloadToken + ".sha256")
    Invoke-WebRequest -Uri $zipAsset.browser_download_url -OutFile $zipPath -Headers $headers -UseBasicParsing -TimeoutSec 300
    Invoke-WebRequest -Uri $hashAsset.browser_download_url -OutFile $hashPath -Headers $headers -UseBasicParsing -TimeoutSec 30
    $expectedHash = ((Get-Content -LiteralPath $hashPath -Raw -Encoding UTF8) -split '\s+')[0].ToLowerInvariant()
    $actualHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($expectedHash -notmatch '^[0-9a-f]{64}$' -or $actualHash -ne $expectedHash) {
        throw "Windows Release ZIP 的 SHA-256 校验失败。"
    }
    if ($zipAsset.PSObject.Properties.Name -contains "digest" -and $zipAsset.digest) {
        if ([string]$zipAsset.digest -ne ("sha256:" + $actualHash)) {
            throw "GitHub 资产 digest 与下载文件不一致。"
        }
    }
    $releaseRoot = Join-Path (Split-Path -Parent $state.Root) "releases"
    New-Item -ItemType Directory -Path $releaseRoot -Force | Out-Null
    $extractPath = Join-Path $releaseRoot ("v" + $latestVersion + "-" + $downloadToken.Substring(0, 8))
    if (Test-Path -LiteralPath $extractPath) { throw "目标版本目录已存在，拒绝覆盖。" }
    Expand-Archive -LiteralPath $zipPath -DestinationPath $extractPath
    $identityAfter = Get-EamStableIdentity -RepositoryRoot $extractPath
    if ($identityAfter.Version -ne $latestVersion) {
        throw "下载包内版本与 GitHub Release 标签不一致。"
    }
    $newStart = Join-Path $extractPath "scripts\local\start.ps1"
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $newStart -NoBrowser
    if ($LASTEXITCODE -ne 0) {
        $failedLog = Save-UpdateLog -State $state -Payload ([ordered]@{
            status = "failed"
            source = "release"
            old_commit = $identityBefore.Commit
            new_commit = $identityAfter.Commit
            backup = $backupSummary
            retained_new_directory = $extractPath
            message = "新 Release 启动失败；旧目录、旧镜像、数据库和备份均已保留。"
        })
        throw "新 Release 启动失败。恢复信息已保存：$failedLog"
    }
    Write-EamRestrictedText -Path $state.CurrentReleasePointer -Value $extractPath
    $version = Wait-EamHealth -Url $context.Url -ExpectedCommit $identityAfter.Commit
    $logPath = Save-UpdateLog -State $state -Payload ([ordered]@{
        status = "completed"
        source = "release"
        old_commit = $identityBefore.Commit
        new_commit = $identityAfter.Commit
        old_version = $identityBefore.Version
        new_version = $identityAfter.Version
        backup = $backupSummary
        old_directory = $repositoryRoot
        new_directory = $extractPath
        zip_sha256 = $actualHash
        completed_at = (Get-Date).ToUniversalTime().ToString("o")
    })
    Remove-Item -LiteralPath $zipPath, $hashPath -Force
    Write-Host "Release 更新完成；旧版本目录已保留为代码回滚点。" -ForegroundColor Green
    Write-Host "当前版本目录：$extractPath"
    Write-Host "更新记录：$logPath"
    if (-not $NoBrowser) { Open-EamBrowser -Url $context.Url }
    exit 0
}
catch {
    Write-Host "更新失败：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host "脚本未删除旧镜像、数据库卷、附件卷或更新前备份。" -ForegroundColor Yellow
    exit 1
}
