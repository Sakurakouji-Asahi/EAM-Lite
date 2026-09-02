[CmdletBinding()]
param(
    [string]$ProductionRoot,
    [switch]$NoBrowser,
    [switch]$CheckOnly
)

. (Join-Path $PSScriptRoot "common.ps1")

function Get-EamPromotionGitText {
    param(
        [Parameter(Mandatory = $true)][string]$RepositoryRoot,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    return ((Invoke-EamGit -RepositoryRoot $RepositoryRoot -Arguments $Arguments).Output -join "`n").Trim()
}

function Assert-EamPromotionTreeClean {
    param(
        [Parameter(Mandatory = $true)][string]$RepositoryRoot,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $status = Get-EamPromotionGitText -RepositoryRoot $RepositoryRoot -Arguments @(
        "status",
        "--porcelain=v1",
        "--untracked-files=all"
    )
    if (-not [string]::IsNullOrWhiteSpace($status)) {
        throw "$Label 存在未提交文件，已拒绝同步。请先提交或移走这些文件。"
    }
}

function Get-EamPromotionCommonDirectory {
    param([Parameter(Mandatory = $true)][string]$RepositoryRoot)

    $path = Get-EamPromotionGitText -RepositoryRoot $RepositoryRoot -Arguments @(
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir"
    )
    return [System.IO.Path]::GetFullPath($path).TrimEnd("\")
}

function Invoke-EamPromotionPowerShell {
    param(
        [Parameter(Mandatory = $true)][string]$ScriptPath,
        [string[]]$Arguments = @()
    )

    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $ScriptPath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "执行失败：$ScriptPath"
    }
}

try {
    Assert-EamWindows
    $developmentRoot = [System.IO.Path]::GetFullPath((Get-EamRepositoryRoot)).TrimEnd("\")
    if ([string]::IsNullOrWhiteSpace($ProductionRoot)) {
        $ProductionRoot = Join-Path (Split-Path -Parent $developmentRoot) "资产管理-正式版"
    }
    $ProductionRoot = [System.IO.Path]::GetFullPath($ProductionRoot).TrimEnd("\")

    if ($developmentRoot -eq $ProductionRoot) {
        throw "开发版与正式版不能使用同一个目录。"
    }
    if (-not (Test-Path -LiteralPath $ProductionRoot -PathType Container)) {
        throw "未找到正式版目录：$ProductionRoot"
    }
    if (-not (Test-EamGitRepository -RepositoryRoot $developmentRoot)) {
        throw "开发版目录不是 Git 工作区：$developmentRoot"
    }
    if (-not (Test-EamGitRepository -RepositoryRoot $ProductionRoot)) {
        throw "正式版目录不是 Git 工作区：$ProductionRoot"
    }

    Assert-EamPromotionTreeClean -RepositoryRoot $developmentRoot -Label "开发版"
    Assert-EamPromotionTreeClean -RepositoryRoot $ProductionRoot -Label "正式版"

    $developmentCommon = Get-EamPromotionCommonDirectory -RepositoryRoot $developmentRoot
    $productionCommon = Get-EamPromotionCommonDirectory -RepositoryRoot $ProductionRoot
    if ($developmentCommon -ne $productionCommon) {
        throw "两个目录不属于同一源码仓库，已拒绝同步。"
    }

    $productionBranch = Get-EamPromotionGitText -RepositoryRoot $ProductionRoot -Arguments @(
        "branch",
        "--show-current"
    )
    if ($productionBranch -ne "codex/production") {
        throw "正式版必须位于 codex/production 分支，当前为：$productionBranch"
    }

    $developmentCommit = Get-EamPromotionGitText -RepositoryRoot $developmentRoot -Arguments @(
        "rev-parse",
        "HEAD"
    )
    $productionCommit = Get-EamPromotionGitText -RepositoryRoot $ProductionRoot -Arguments @(
        "rev-parse",
        "HEAD"
    )
    $version = (Get-Content -LiteralPath (Join-Path $developmentRoot "VERSION") -Raw -Encoding UTF8).Trim()
    if ($version -notmatch '^\d+\.\d+\.\d+([.-][0-9A-Za-z.-]+)?$') {
        throw "开发版 VERSION 格式非法：$version"
    }

    $formalTags = @(
        (Invoke-EamGit -RepositoryRoot $ProductionRoot -Arguments @(
            "tag",
            "--points-at",
            "HEAD",
            "--list",
            "v[0-9]*"
        )).Output | Where-Object { $_ -match '^v\d+\.\d+\.\d+([.-][0-9A-Za-z.-]+)?$' }
    )
    if ($formalTags.Count -eq 0) {
        throw "正式版当前提交缺少正式标签，无法证明运行来源。"
    }

    if ($developmentCommit -eq $productionCommit) {
        Write-Host "开发版与正式版已经同步：$($developmentCommit.Substring(0, 12))" -ForegroundColor Green
        exit 0
    }

    $ancestor = Invoke-EamGit -RepositoryRoot $developmentRoot -Arguments @(
        "merge-base",
        "--is-ancestor",
        $productionCommit,
        $developmentCommit
    ) -AllowFailure
    if ($ancestor.ExitCode -ne 0) {
        throw "开发历史不是正式版的直接后续，已拒绝自动合并。请先人工处理分支差异。"
    }

    Write-Host "同步检查通过：" -ForegroundColor Cyan
    Write-Host "  开发版：$developmentRoot"
    Write-Host "  正式版：$ProductionRoot"
    Write-Host "  当前正式：$($productionCommit.Substring(0, 12))"
    Write-Host "  待发布：$($developmentCommit.Substring(0, 12))"
    if ($CheckOnly) {
        Write-Host "仅检查模式完成，未修改正式版。" -ForegroundColor Green
        exit 0
    }

    Ensure-EamDockerReady | Out-Null
    $productionScripts = Join-Path $ProductionRoot "scripts\local"
    $startScript = Join-Path $productionScripts "start.ps1"
    $backupScript = Join-Path $productionScripts "backup.ps1"
    $stopScript = Join-Path $productionScripts "stop.ps1"

    Write-Host "第 1/4 步：确认当前正式版可用……" -ForegroundColor Cyan
    Invoke-EamPromotionPowerShell -ScriptPath $startScript -Arguments @("-NoBrowser")

    Write-Host "第 2/4 步：创建并验证更新前备份……" -ForegroundColor Cyan
    Invoke-EamPromotionPowerShell -ScriptPath $backupScript

    Write-Host "第 3/4 步：停止正式版并快进同步代码……" -ForegroundColor Cyan
    Invoke-EamPromotionPowerShell -ScriptPath $stopScript
    $tag = "v$version-local." + (Get-Date -Format "yyyyMMddHHmmss") + "." + $developmentCommit.Substring(0, 12)
    Invoke-EamGit -RepositoryRoot $developmentRoot -Arguments @("tag", $tag, $developmentCommit) | Out-Null
    Invoke-EamGit -RepositoryRoot $ProductionRoot -Arguments @(
        "merge",
        "--ff-only",
        $developmentCommit
    ) | Out-Null

    Write-Host "第 4/4 步：执行迁移、启动并检查正式版……" -ForegroundColor Cyan
    $startArguments = if ($NoBrowser) { @("-NoBrowser") } else { @() }
    Invoke-EamPromotionPowerShell -ScriptPath $startScript -Arguments $startArguments

    $state = Initialize-EamState -Mode local
    $backup = $null
    if (Test-Path -LiteralPath $state.LastPortableBackup -PathType Leaf) {
        $backup = Get-Content -LiteralPath $state.LastPortableBackup -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    $logPath = Join-Path $state.Logs ("development-promotion-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".json")
    Write-EamRestrictedText -Path $logPath -Value ([ordered]@{
        status = "completed"
        source_directory = $developmentRoot
        production_directory = $ProductionRoot
        old_commit = $productionCommit
        new_commit = $developmentCommit
        release_tag = $tag
        backup = $backup
        completed_at = (Get-Date).ToUniversalTime().ToString("o")
    } | ConvertTo-Json -Depth 10)

    Write-Host "同步完成：$($productionCommit.Substring(0, 12)) → $($developmentCommit.Substring(0, 12))" -ForegroundColor Green
    Write-Host "正式数据、附件、密钥和备份始终与开发环境分离。" -ForegroundColor Green
    Write-Host "同步记录：$logPath"
    exit 0
}
catch {
    Write-Host "同步失败：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host "脚本未删除正式数据库卷、附件卷、密钥或已生成的备份。" -ForegroundColor Yellow
    exit 1
}
