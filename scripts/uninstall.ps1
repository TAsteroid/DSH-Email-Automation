#Requires -Version 7
<#
.SYNOPSIS
    从指定的 DSH profile 卸载 dsh-mail-plugin。

.DESCRIPTION
    * 移除 cordis.patch.yml 里的 dsh-mail-plugin 条目；
    * 删除 profile 的 node_modules\dsh-mail-plugin junction（只删链接，不删仓库）；
    * 从 package.json 删除 dsh-mail-plugin 依赖与 dsh.profile.bundles 条目；
    * 可选 -PurgeData：交互确认后删除数据目录。

    脚本幂等：没有可删内容时也正常退出（exit 0）。
    修改文件前会备份为 <文件名>.bak-<yyyyMMddHHmmss>。

.PARAMETER Profile
    profile 名称（默认 desktop），或 profile 目录的完整路径。

.PARAMETER PurgeData
    同时删除数据目录（默认取 patch 里配置的 dataDir，缺失时用 <RepoPath>\data\mail）。
    会先打印路径并要求输入 yes 确认。

.EXAMPLE
    pwsh -File .\uninstall.ps1
    pwsh -File .\uninstall.ps1 -PurgeData
#>
param(
    [string]$Profile = 'desktop',
    [switch]$PurgeData
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:LogPrefix = '[uninstall]'

. (Join-Path $PSScriptRoot '_mail-plugin-common.ps1')

function Assert-SafePurgeTarget([string]$Path, [string]$RepoDir, [string]$ProfileDir) {
    $full = Get-FullPath $Path
    $trimmed = $full.TrimEnd('\', '/')
    if ([string]::IsNullOrWhiteSpace($trimmed)) {
        Fail '数据目录为空，拒绝删除。'
    }
    $root = [System.IO.Path]::GetPathRoot($full)
    if (Test-SamePath $trimmed $root) {
        Fail "拒绝删除盘符根目录：$full"
    }
    if (Test-SamePath $trimmed $RepoDir) {
        Fail "拒绝删除仓库目录：$full"
    }
    if (Test-SamePath $trimmed $ProfileDir) {
        Fail "拒绝删除 profile 目录：$full"
    }
    if (Test-SamePath $trimmed $env:USERPROFILE) {
        Fail "拒绝删除用户主目录：$full"
    }
    if ($trimmed.Length -lt ($root.Length + 2)) {
        Fail "路径过短，拒绝删除：$full"
    }
}

try {
    Initialize-ConsoleEncoding
    Assert-PowerShell7

    $profileDir = Resolve-ProfileDirectory $Profile
    $repoDir = Resolve-RepoDirectory -RepoValue $null -ScriptRoot $PSScriptRoot -RequirePlugin $false
    $patchPath = Join-Path $profileDir 'cordis.patch.yml'
    $packagePath = Join-Path $profileDir 'package.json'
    $linkPath = Join-Path (Join-Path $profileDir 'node_modules') $script:PluginId

    Write-Log "profile：$profileDir"

    $removedSomething = $false

    # 1. patch 条目（先读出 dataDir 供 -PurgeData 使用）
    $patch = Read-PatchFile $patchPath
    $existing = Get-PluginConfigValues $patch.Lines
    $configuredDataDir = $null
    if ($existing.Contains('dataDir') -and -not [string]::IsNullOrWhiteSpace([string]$existing['dataDir'])) {
        $configuredDataDir = [string]$existing['dataDir']
    }

    if ($null -ne (Get-PluginEntryRange $patch.Lines)) {
        $null = Remove-PluginEntry $patch.Lines
        $null = Save-PatchFile $patch 0
        $removedSomething = $true
    }
    else {
        Write-Log 'cordis.patch.yml：没有 dsh-mail-plugin 条目，跳过'
    }

    # 2. junction
    $state = Get-LinkState $linkPath
    if (-not $state.Exists) {
        Write-Log 'junction 不存在，跳过'
    }
    elseif ($state.IsLink) {
        Remove-JunctionPath $linkPath
        Write-Log "已删除 junction：$linkPath"
        $removedSomething = $true
    }
    else {
        Write-Warning "路径不是 junction/symlink，跳过删除以免误删真实目录：$linkPath"
    }

    # 3. package.json：依赖与 bundles
    if (Test-Path -LiteralPath $packagePath -PathType Leaf) {
        $package = Read-PackageJson $packagePath
        $dependenciesProp = $package.Data.PSObject.Properties['dependencies']
        if ($null -ne $dependenciesProp -and $null -ne $dependenciesProp.Value) {
            if ($null -ne $package.Data.dependencies.PSObject.Properties[$script:PluginId]) {
                Remove-JsonProperty $package.Data.dependencies $script:PluginId
                Write-Log "package.json：已删除依赖 $($script:PluginId)"
            }
        }
        $dshProp = $package.Data.PSObject.Properties['dsh']
        if ($null -ne $dshProp -and $null -ne $dshProp.Value) {
            $profileProp = $package.Data.dsh.PSObject.Properties['profile']
            if ($null -ne $profileProp -and $null -ne $profileProp.Value) {
                $bundlesProp = $package.Data.dsh.profile.PSObject.Properties['bundles']
                if ($null -ne $bundlesProp -and $null -ne $bundlesProp.Value) {
                    $original = @($bundlesProp.Value)
                    $kept = [System.Collections.Generic.List[string]]::new()
                    foreach ($item in $original) {
                        if ([string]$item -ne $script:PluginId) { $kept.Add([string]$item) }
                    }
                    if ($kept.Count -ne $original.Count) {
                        Set-JsonProperty $package.Data.dsh.profile 'bundles' ([string[]]$kept.ToArray())
                        Write-Log "package.json：已从 dsh.profile.bundles 移除 $($script:PluginId)"
                    }
                }
            }
        }
        if (Save-PackageJson $package) {
            $removedSomething = $true
        }
    }
    else {
        Write-Log 'package.json 不存在，跳过'
    }

    # 4. 可选：删除数据目录
    if ($PurgeData) {
        if (-not [string]::IsNullOrWhiteSpace($configuredDataDir)) {
            $purgeDir = Get-FullPath $configuredDataDir
        }
        else {
            $purgeDir = Get-FullPath (Join-Path $repoDir 'data\mail')
        }
        Write-Log "数据目录：$purgeDir"
        if (-not (Test-Path -LiteralPath $purgeDir -PathType Container)) {
            Write-Log '数据目录不存在，无需删除'
        }
        else {
            Assert-SafePurgeTarget $purgeDir $repoDir $profileDir
            Write-Host "即将删除数据目录（邮件缓存与附件，不可恢复）：$purgeDir"
            Write-Host '建议先退出 DSH 再执行 -PurgeData。'
            $answer = ''
            try {
                $answer = Read-Host '输入 yes 确认删除（其它输入取消）'
            }
            catch {
                $answer = ''
            }
            if ($answer -eq 'yes') {
                Remove-Item -LiteralPath $purgeDir -Recurse -Force
                Write-Log "已删除数据目录 $purgeDir"
            }
            else {
                Write-Log '未确认，保留数据目录'
            }
        }
    }

    Write-Host ''
    if ($removedSomething) {
        Write-Log '卸载完成。'
        Write-Host '下一步：重启 DSH 使插件从组合中移除。'
    }
    else {
        Write-Log '没有发现已安装的 dsh-mail-plugin 内容，已是卸载状态。'
    }
    exit 0
}
catch {
    $host.UI.WriteErrorLine("卸载失败：$($_.Exception.Message)")
    exit 1
}
