#Requires -Version 7
<#
.SYNOPSIS
    把 dsh-mail-plugin 安装到指定的 DSH profile。

.DESCRIPTION
    * 校验 profile、仓库与插件入口；
    * 创建/修复 profile 的 node_modules\dsh-mail-plugin junction；
    * 在 profile 的 package.json 里补 dependencies 与 dsh.profile.bundles；
    * 在 profile 的 cordis.patch.yml 里幂等写入 dsh-mail-plugin 条目；
    * 创建数据目录与工作区目录。

    所有被修改的文件在写入前都会备份为 <文件名>.bak-<yyyyMMddHHmmss>。
    脚本可重复执行（幂等）。

.PARAMETER Profile
    profile 名称（默认 desktop），或 profile 目录的完整路径。
    也可用环境变量 DSH_PROFILES_ROOT 覆盖 profiles 根目录。

.PARAMETER RepoPath
    本仓库根目录（包含 dsh-mail-plugin 目录），默认是脚本所在目录的上一级。

.PARAMETER DataDir
    数据目录，默认 <RepoPath>\data\mail。
    显式传入时写入 patch；未传入时保留 profile 里已有的 dataDir，缺失才用默认值。

.PARAMETER WorkspaceDir
    邮件工作区目录，默认 <RepoPath>\data\mail\workspace。
    显式传入时写入 patch；未传入时保留已有值，缺失才用默认值。

.PARAMETER Force
    junction 已指向别处、或 package.json 依赖指向别处时，强制改为本仓库。

.EXAMPLE
    pwsh -File .\install.ps1
    pwsh -File .\install.ps1 -DataDir 'D:\MailData' -WorkspaceDir 'D:\MailWorkspace'
#>
param(
    [string]$Profile = 'desktop',
    [string]$RepoPath,
    [string]$DataDir,
    [string]$WorkspaceDir,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:LogPrefix = '[install]'

. (Join-Path $PSScriptRoot '_mail-plugin-common.ps1')

try {
    Initialize-ConsoleEncoding
    Assert-PowerShell7

    $profileDir = Resolve-ProfileDirectory $Profile
    $repoDir = Resolve-RepoDirectory -RepoValue $RepoPath -ScriptRoot $PSScriptRoot -RequirePlugin $true
    $pluginDir = Join-Path $repoDir 'dsh-mail-plugin'
    $patchPath = Join-Path $profileDir 'cordis.patch.yml'
    $packagePath = Join-Path $profileDir 'package.json'
    $linkPath = Join-Path (Join-Path $profileDir 'node_modules') $script:PluginId

    Write-Log "profile：$profileDir"
    Write-Log "仓库：$repoDir"

    if (-not (Test-Path -LiteralPath $packagePath -PathType Leaf)) {
        Fail "profile 的 package.json 不存在，无法登记插件依赖：$packagePath（请先确认 profile 路径正确）"
    }

    # 先读现有 patch，决定数据目录是沿用还是覆盖。
    $patch = Read-PatchFile $patchPath
    $existing = Get-PluginConfigValues $patch.Lines

    if ($PSBoundParameters.ContainsKey('DataDir')) {
        $dataDirValue = Get-FullPath $DataDir
    }
    elseif ($existing.Contains('dataDir') -and -not [string]::IsNullOrWhiteSpace([string]$existing['dataDir'])) {
        $dataDirValue = Get-FullPath ([string]$existing['dataDir'])
    }
    else {
        $dataDirValue = Get-FullPath (Join-Path $repoDir 'data\mail')
    }

    if ($PSBoundParameters.ContainsKey('WorkspaceDir')) {
        $workspaceValue = Get-FullPath $WorkspaceDir
    }
    elseif ($existing.Contains('mailWorkspace') -and -not [string]::IsNullOrWhiteSpace([string]$existing['mailWorkspace'])) {
        $workspaceValue = Get-FullPath ([string]$existing['mailWorkspace'])
    }
    else {
        $workspaceValue = Get-FullPath (Join-Path $repoDir 'data\mail\workspace')
    }

    if ($existing.Contains('mailWorkspaceId') -and -not [string]::IsNullOrWhiteSpace([string]$existing['mailWorkspaceId'])) {
        $workspaceId = [string]$existing['mailWorkspaceId']
    }
    else {
        $workspaceId = [guid]::NewGuid().ToString()
    }

    if ($existing.Contains('syncIntervalMs') -and -not [string]::IsNullOrWhiteSpace([string]$existing['syncIntervalMs'])) {
        $syncInterval = [string]$existing['syncIntervalMs']
    }
    else {
        $syncInterval = '300000'
    }

    Ensure-Directory $dataDirValue
    Ensure-Directory $workspaceValue

    # junction
    $nodeModules = Join-Path $profileDir 'node_modules'
    Ensure-Directory $nodeModules
    $null = Ensure-PluginJunction -LinkPath $linkPath -TargetPath $pluginDir -Force:([bool]$Force) -RepairHint 'install.ps1 -Force'

    # package.json：dependencies + dsh.profile.bundles
    $package = Read-PackageJson $packagePath
    $desiredSpec = Get-PluginDependencySpec $pluginDir

    $dependenciesProp = $package.Data.PSObject.Properties['dependencies']
    if ($null -eq $dependenciesProp -or $null -eq $dependenciesProp.Value) {
        Set-JsonProperty $package.Data 'dependencies' ([pscustomobject]@{})
        Write-Log 'package.json：已创建 dependencies 对象'
    }

    $dependency = $package.Data.dependencies.PSObject.Properties[$script:PluginId]
    if ($null -eq $dependency) {
        $package.Data.dependencies | Add-Member -NotePropertyName $script:PluginId -NotePropertyValue $desiredSpec
        Write-Log "package.json：已添加依赖 $($script:PluginId) = $desiredSpec"
    }
    elseif (Test-LinkSpecPointsTo ([string]$dependency.Value) $pluginDir) {
        Write-Log "package.json：依赖已指向本仓库，无需修改"
    }
    else {
        if (-not $Force) {
            Fail "package.json 中 $($script:PluginId) 依赖为 '$($dependency.Value)'，与本仓库 $desiredSpec 不一致；使用 -Force 覆盖。"
        }
        $dependency.Value = $desiredSpec
        Write-Log "package.json：已将依赖更新为 $desiredSpec"
    }

    $dshProp = $package.Data.PSObject.Properties['dsh']
    if ($null -eq $dshProp -or $null -eq $dshProp.Value) {
        Set-JsonProperty $package.Data 'dsh' ([pscustomobject]@{})
        Write-Log 'package.json：已创建 dsh 对象'
    }
    $dshData = $package.Data.dsh
    $profileProp = $dshData.PSObject.Properties['profile']
    if ($null -eq $profileProp -or $null -eq $profileProp.Value) {
        Set-JsonProperty $dshData 'profile' ([pscustomobject]@{})
        Write-Log 'package.json：已创建 dsh.profile 对象'
    }
    $profileData = $dshData.profile

    $bundlesProp = $profileData.PSObject.Properties['bundles']
    $bundles = [System.Collections.Generic.List[string]]::new()
    if ($null -ne $bundlesProp -and $null -ne $bundlesProp.Value) {
        foreach ($item in @($bundlesProp.Value)) {
            $bundles.Add([string]$item)
        }
    }
    $beforeBundles = $bundles -join "`0"
    $filtered = [System.Collections.Generic.List[string]]::new()
    foreach ($item in $bundles) {
        if ($item -ne $script:PluginId) { $filtered.Add($item) }
    }
    $filtered.Add($script:PluginId)
    $afterBundles = $filtered -join "`0"
    if ($beforeBundles -ne $afterBundles) {
        Set-JsonProperty $profileData 'bundles' ([string[]]$filtered.ToArray())
        Write-Log "package.json：dsh.profile.bundles 已包含 $($script:PluginId)"
    }
    else {
        Write-Log "package.json：dsh.profile.bundles 已包含 $($script:PluginId)，无需修改"
    }

    $null = Save-PackageJson $package

    # cordis.patch.yml：追加或更新 dsh-mail-plugin 条目
    $entry = Get-PluginEntryRange $patch.Lines
    if ($null -eq $entry) {
        $configValues = [ordered]@{
            syncIntervalMs  = $syncInterval
            dataDir         = $dataDirValue
            mailWorkspace   = $workspaceValue
            mailWorkspaceId = $workspaceId
        }
        Add-PluginEntry -Lines $patch.Lines -ConfigValues $configValues -Disabled $false
    }
    else {
        $configChanged = $false
        if (Set-PluginConfigValue $patch.Lines 'syncIntervalMs' $syncInterval) { $configChanged = $true }
        if (Set-PluginConfigValue $patch.Lines 'dataDir' $dataDirValue) { $configChanged = $true }
        if (Set-PluginConfigValue $patch.Lines 'mailWorkspace' $workspaceValue) { $configChanged = $true }
        if (Set-PluginConfigValue $patch.Lines 'mailWorkspaceId' $workspaceId) { $configChanged = $true }
        if (Set-PluginDisabled $patch.Lines $false) { $configChanged = $true }
        if (-not $configChanged) {
            Write-Log 'cordis.patch.yml：配置已是最新，无需修改'
        }
    }
    $null = Save-PatchFile $patch 1

    Write-Host ''
    Write-Log '安装完成。'
    Write-Host '下一步：重启 DSH（或重启后在「插件管理器」里把 dsh-mail-plugin 禁用再启用）使改动生效。'
    exit 0
}
catch {
    $host.UI.WriteErrorLine("安装失败：$($_.Exception.Message)")
    exit 1
}
