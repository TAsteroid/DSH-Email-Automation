#Requires -Version 7
<#
.SYNOPSIS
    升级已安装的 dsh-mail-plugin。

.DESCRIPTION
    * 可选从 origin 拉取最新代码（推荐；可用 -SkipPull 跳过）；
    * 确保 profile 的 junction、package.json 依赖/bundles、cordis.patch.yml 条目都存在；
    * 只补充 patch config 里缺失的键，绝不覆盖用户已有的
      dataDir / mailWorkspace / syncIntervalMs 值；
    * 不直接复制 SQLite 数据库（WAL 模式下复制可能丢数据）。
      数据库迁移与迁移前自动备份由 Mail Core 在下次启动时完成。

    修改文件前会备份为 <文件名>.bak-<yyyyMMddHHmmss>。

.PARAMETER Profile
    profile 名称（默认 desktop），或 profile 目录的完整路径。

.PARAMETER SkipPull
    跳过 git pull，只用当前仓库内容做升级/修复。

.EXAMPLE
    pwsh -File .\upgrade.ps1
    pwsh -File .\upgrade.ps1 -SkipPull
#>
param(
    [string]$Profile = 'desktop',
    [switch]$SkipPull
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:LogPrefix = '[upgrade]'

. (Join-Path $PSScriptRoot '_mail-plugin-common.ps1')

try {
    Initialize-ConsoleEncoding
    Assert-PowerShell7

    $repoDir = Resolve-RepoDirectory -RepoValue $null -ScriptRoot $PSScriptRoot -RequirePlugin $true
    $profileDir = Resolve-ProfileDirectory $Profile
    $pluginDir = Join-Path $repoDir 'packages\dsh-mail-plugin'
    $patchPath = Join-Path $profileDir 'cordis.patch.yml'
    $packagePath = Join-Path $profileDir 'package.json'
    $linkPath = Join-Path (Join-Path $profileDir 'node_modules') $script:PluginId

    Write-Log "仓库：$repoDir"
    Write-Log "profile：$profileDir"

    if (-not (Test-Path -LiteralPath $packagePath -PathType Leaf)) {
        Fail "profile 的 package.json 不存在，无法登记插件依赖：$packagePath（请先确认 profile 路径正确）"
    }

    # 1. 拉取最新代码
    if ($SkipPull) {
        Write-Log '已跳过 git pull（-SkipPull）'
    }
    else {
        $git = Get-Command git -ErrorAction SilentlyContinue
        if ($null -eq $git) {
            Fail '未找到 git 命令，无法拉取更新。请安装 Git 并加入 PATH，或使用 -SkipPull 跳过拉取。'
        }
        Write-Log "git -C $repoDir pull --ff-only"
        $previousPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $pullOutput = & git -C $repoDir pull --ff-only 2>&1
            $pullCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousPreference
        }
        foreach ($line in @($pullOutput)) {
            Write-Host ([string]$line)
        }
        if ($pullCode -ne 0) {
            Fail "git pull 失败（退出码 $pullCode）。请手动处理后重试，或使用 -SkipPull 跳过拉取。"
        }
    }

    # 2. package.json：依赖 + bundles
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
        Write-Log "package.json：已补充依赖 $($script:PluginId) = $desiredSpec"
    }
    elseif (Test-LinkSpecPointsTo ([string]$dependency.Value) $pluginDir) {
        Write-Log 'package.json：依赖已指向本仓库，无需修改'
    }
    else {
        Fail "package.json 中 $($script:PluginId) 依赖为 '$($dependency.Value)'，与本仓库 $desiredSpec 不一致；请先运行 install.ps1 -Force 修正。"
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
    if (($filtered -join "`0") -ne $beforeBundles) {
        Set-JsonProperty $profileData 'bundles' ([string[]]$filtered.ToArray())
        Write-Log "package.json：dsh.profile.bundles 已确保包含 $($script:PluginId)"
    }
    else {
        Write-Log 'package.json：dsh.profile.bundles 已包含条目标识，无需修改'
    }
    $null = Save-PackageJson $package

    # 3. junction
    Ensure-Directory (Join-Path $profileDir 'node_modules')
    $null = Ensure-PluginJunction -LinkPath $linkPath -TargetPath $pluginDir -Force:$false -RepairHint 'install.ps1 -Force'

    # 4. cordis.patch.yml：缺条目则补，缺键则补；绝不覆盖已有值
    $patch = Read-PatchFile $patchPath
    $existing = Get-PluginConfigValues $patch.Lines
    $defaultDataDir = Get-FullPath (Join-Path $repoDir 'data\mail')
    $defaultWorkspace = Get-FullPath (Join-Path $repoDir 'data\mail\workspace')

    if ($null -eq (Get-PluginEntryRange $patch.Lines)) {
        $configValues = [ordered]@{
            syncIntervalMs  = '300000'
            dataDir         = $defaultDataDir
            mailWorkspace   = $defaultWorkspace
            mailWorkspaceId = [guid]::NewGuid().ToString()
        }
        Add-PluginEntry -Lines $patch.Lines -ConfigValues $configValues -Disabled $false
    }
    else {
        if (-not ($existing.Contains('syncIntervalMs') -and -not [string]::IsNullOrWhiteSpace([string]$existing['syncIntervalMs']))) {
            $null = Set-PluginConfigValue $patch.Lines 'syncIntervalMs' '300000'
        }
        if (-not ($existing.Contains('dataDir') -and -not [string]::IsNullOrWhiteSpace([string]$existing['dataDir']))) {
            $null = Set-PluginConfigValue $patch.Lines 'dataDir' $defaultDataDir
        }
        if (-not ($existing.Contains('mailWorkspace') -and -not [string]::IsNullOrWhiteSpace([string]$existing['mailWorkspace']))) {
            $null = Set-PluginConfigValue $patch.Lines 'mailWorkspace' $defaultWorkspace
        }
        if (-not ($existing.Contains('mailWorkspaceId') -and -not [string]::IsNullOrWhiteSpace([string]$existing['mailWorkspaceId']))) {
            $null = Set-PluginConfigValue $patch.Lines 'mailWorkspaceId' ([guid]::NewGuid().ToString())
        }
    }
    $null = Save-PatchFile $patch 1

    $valuesAfter = Get-PluginConfigValues $patch.Lines
    $dataDirForMessage = $defaultDataDir
    if ($valuesAfter.Contains('dataDir') -and -not [string]::IsNullOrWhiteSpace([string]$valuesAfter['dataDir'])) {
        $dataDirForMessage = Get-FullPath ([string]$valuesAfter['dataDir'])
    }

    Write-Host ''
    Write-Log '升级准备完成。'
    Write-Host '数据库备份：没有直接复制 mail.db（WAL 模式下复制可能丢数据）。'
    Write-Host '下次启动 DSH 时，Mail Core 若检测到 schema 需要升级，会先把整库自动备份到 backups 目录，再执行迁移。'
    Write-Host "当前 dataDir：$dataDirForMessage"
    Write-Host "备份目录：$([System.IO.Path]::Combine($dataDirForMessage, 'backups'))"
    Write-Host '下一步：关闭并重新启动 DSH，然后在插件面板确认收件箱与同步状态。'
    exit 0
}
catch {
    $host.UI.WriteErrorLine("升级失败：$($_.Exception.Message)")
    exit 1
}
