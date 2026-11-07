# dsh-mail-plugin 交付脚本的共用函数（由 install.ps1 / uninstall.ps1 / upgrade.ps1 dot-source）
# 仅使用 PowerShell 7 内置能力，不依赖第三方模块。
#
# 设计要点：
# * 所有文件修改都“先备份、再写、写后校验”，校验失败立即回滚并报错；
# * 只做最小文本编辑，不重排用户文件里的其它内容；
# * 任何失败都抛异常，由入口脚本统一打印原因并 exit 1。

# 这些值由入口脚本在 dot-source 前设置；这里只给默认值兜底。
if (-not (Get-Variable -Name LogPrefix -Scope Script -ErrorAction SilentlyContinue)) {
    $script:LogPrefix = '[mail-plugin]'
}

$script:PluginId = 'dsh-mail-plugin'
$script:EntryLineRegex = '^[ \t]*-[ \t]+id:[ \t]*["'']?' + [regex]::Escape($script:PluginId) + '["'']?[ \t]*(#.*)?$'

function Write-Log([string]$Message) {
    Write-Host "$($script:LogPrefix) $Message"
}

function Fail([string]$Message) {
    throw [System.InvalidOperationException]::new($Message)
}

function Initialize-ConsoleEncoding {
    # 中文 Windows 上管道输出默认是 GBK；统一成 UTF-8，便于写入日志或被其它进程读取。
    # 只影响重定向/管道，不影响 Windows Terminal 里的中文显示。
    try {
        $utf8 = [System.Text.UTF8Encoding]::new($false)
        [Console]::OutputEncoding = $utf8
        $OutputEncoding = $utf8
    }
    catch {
        # 没有控制台的环境（极少数）忽略即可。
    }
}

function Assert-PowerShell7 {
    if ($PSVersionTable.PSVersion.Major -lt 7) {
        Fail '需要 PowerShell 7（pwsh）或更高版本。'
    }
    $pwsh = Get-Command pwsh -ErrorAction SilentlyContinue
    $localPwsh = Join-Path $PSHOME 'pwsh.exe'
    if ($null -eq $pwsh -and -not (Test-Path -LiteralPath $localPwsh -PathType Leaf)) {
        Fail '未找到 PowerShell 7 可执行文件 pwsh。'
    }
}

# ---------------------------------------------------------------- 路径 -----

function Get-FullPath([string]$Path) {
    return [System.IO.Path]::GetFullPath($Path)
}

function Test-SamePath([string]$Left, [string]$Right) {
    if ([string]::IsNullOrWhiteSpace($Left) -or [string]::IsNullOrWhiteSpace($Right)) {
        return $false
    }
    try {
        $a = [System.IO.Path]::GetFullPath($Left).TrimEnd('\', '/')
        $b = [System.IO.Path]::GetFullPath($Right).TrimEnd('\', '/')
    }
    catch {
        return $false
    }
    return [string]::Equals($a, $b, [System.StringComparison]::OrdinalIgnoreCase)
}

function Resolve-ProfileDirectory([string]$ProfileValue) {
    if ([string]::IsNullOrWhiteSpace($ProfileValue)) {
        Fail '-Profile 不能为空（默认 desktop）。'
    }
    $looksLikePath = ($ProfileValue -match '[\\/]') -or [System.IO.Path]::IsPathRooted($ProfileValue)
    if ($looksLikePath) {
        $candidate = Get-FullPath $ProfileValue
        if (-not (Test-Path -LiteralPath $candidate -PathType Container)) {
            Fail "profile 目录不存在：$candidate"
        }
        return $candidate
    }

    $root = $env:DSH_PROFILES_ROOT
    if ([string]::IsNullOrWhiteSpace($root)) {
        if ($env:DSH_PROFILE -eq $ProfileValue -and -not [string]::IsNullOrWhiteSpace($env:DSH_PROFILE_DIR)) {
            $root = Split-Path -Parent $env:DSH_PROFILE_DIR
        }
        else {
            $root = Join-Path $env:USERPROFILE '.dsh\profiles'
        }
    }
    $candidate = Join-Path $root $ProfileValue
    if (-not (Test-Path -LiteralPath $candidate -PathType Container)) {
        Fail "profile 目录不存在：$candidate（可用 -Profile 传入完整路径，或设置 DSH_PROFILES_ROOT）"
    }
    return (Get-FullPath $candidate)
}

function Resolve-RepoDirectory([string]$RepoValue, [string]$ScriptRoot, [bool]$RequirePlugin = $true) {
    if ([string]::IsNullOrWhiteSpace($RepoValue)) {
        $RepoValue = Split-Path -Parent $ScriptRoot
    }
    if (-not (Test-Path -LiteralPath $RepoValue -PathType Container)) {
        Fail "仓库目录不存在：$RepoValue"
    }
    $repoDir = Get-FullPath $RepoValue
    if ($RequirePlugin) {
        $entry = Join-Path (Join-Path $repoDir 'packages\dsh-mail-plugin') 'lib\index.js'
        if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) {
            Fail "未找到插件入口：$entry（请确认 -RepoPath 指向仓库根目录）"
        }
    }
    return $repoDir
}

function Ensure-Directory([string]$Path) {
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        Fail "路径已被文件占用，无法创建目录：$Path"
    }
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
        Write-Log "已创建目录 $Path"
    }
}

# ---------------------------------------------------------------- 文本 -----

function Read-TextFile([string]$Path) {
    return [System.IO.File]::ReadAllText($Path, [System.Text.UTF8Encoding]::new($false))
}

function Write-TextFile([string]$Path, [string]$Text) {
    [System.IO.File]::WriteAllText($Path, $Text, [System.Text.UTF8Encoding]::new($false))
}

function Get-FileEol([string]$Text) {
    if ($Text.Contains("`r`n")) { return "`r`n" }
    return "`n"
}

function Backup-File([string]$Path) {
    $stamp = Get-Date -Format 'yyyyMMddHHmmss'
    $backup = "$Path.bak-$stamp"
    $index = 2
    while (Test-Path -LiteralPath $backup) {
        $backup = "$Path.bak-$stamp-$index"
        $index++
    }
    Copy-Item -LiteralPath $Path -Destination $backup -Force
    Write-Host "已备份到 $backup"
    return $backup
}

# ---------------------------------------------------------------- JSON -----

function Read-PackageJson([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        Fail "package.json 不存在：$Path"
    }
    $raw = Read-TextFile $Path
    $data = $null
    try {
        $data = $raw | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        Fail "package.json 不是合法 JSON：$($_.Exception.Message)"
    }
    if ($null -eq $data -or $data -isnot [System.Management.Automation.PSCustomObject]) {
        Fail 'package.json 顶层必须是 JSON 对象。'
    }
    return [pscustomobject]@{
        Path      = $Path
        Data      = $data
        Raw       = $raw
        Canonical = ($data | ConvertTo-Json -Depth 100 -Compress)
        Eol       = (Get-FileEol $raw)
    }
}

function Save-PackageJson($Package) {
    $newCanonical = $Package.Data | ConvertTo-Json -Depth 100 -Compress
    if ($newCanonical -eq $Package.Canonical) {
        return $false
    }

    $text = $Package.Data | ConvertTo-Json -Depth 100
    $text = ($text -replace "`r`n", "`n") -replace "`n", $Package.Eol
    if (-not $text.EndsWith($Package.Eol)) {
        $text += $Package.Eol
    }

    $backup = Backup-File $Package.Path
    try {
        Write-TextFile $Package.Path $text
        # 写回后立刻重新解析，非法则回滚到备份并失败。
        $verified = Read-TextFile $Package.Path | ConvertFrom-Json -ErrorAction Stop
        if ($null -eq $verified) {
            throw '写回后 JSON 解析结果为空'
        }
    }
    catch {
        Copy-Item -LiteralPath $backup -Destination $Package.Path -Force
        Fail "package.json 写回校验失败，已回滚到备份 $backup：$($_.Exception.Message)"
    }
    Write-Log "已写入 $($Package.Path)（2 空格缩进，保持原有键顺序）"
    return $true
}

function Set-JsonProperty($Object, [string]$Name, $Value) {
    $property = $Object.PSObject.Properties[$Name]
    if ($null -ne $property) {
        $property.Value = $Value
    }
    else {
        $Object | Add-Member -NotePropertyName $Name -NotePropertyValue $Value
    }
}

function Remove-JsonProperty($Object, [string]$Name) {
    if ($null -ne $Object.PSObject.Properties[$Name]) {
        $Object.PSObject.Properties.Remove($Name)
    }
}

function Get-PluginDependencySpec([string]$PluginDir) {
    return 'link:' + ($PluginDir -replace '\\', '/')
}

function Test-LinkSpecPointsTo([string]$Spec, [string]$PluginDir) {
    if ([string]::IsNullOrWhiteSpace($Spec)) { return $false }
    if ($Spec -notmatch '^link:(?<path>.+)$') { return $false }
    return (Test-SamePath $Matches['path'] $PluginDir)
}

# ---------------------------------------------------------- cordis.patch ----

function Read-PatchFile([string]$Path) {
    $patch = [pscustomobject]@{
        Path  = $Path
        Lines = [System.Collections.Generic.List[string]]::new()
        Eol   = "`n"
        Raw   = ''
    }
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $patch
    }
    $text = Read-TextFile $Path
    $patch.Raw = $text
    if ([string]::IsNullOrEmpty($text)) {
        return $patch
    }
    $patch.Eol = Get-FileEol $text
    $lines = @($text -split "`r?`n")
    if ($lines.Count -gt 0 -and $lines[$lines.Count - 1] -eq '') {
        $lines = @($lines[0..($lines.Count - 2)])
    }
    foreach ($line in $lines) {
        $patch.Lines.Add($line)
    }
    return $patch
}

function Get-IndentWidth([string]$Line) {
    return ([regex]::Match($Line, '^[ \t]*')).Length
}

function Find-PluginEntryIndex([System.Collections.Generic.List[string]]$Lines) {
    for ($i = 0; $i -lt $Lines.Count; $i++) {
        if ($Lines[$i] -match $script:EntryLineRegex) {
            return $i
        }
    }
    return -1
}

function Get-PluginEntryRange([System.Collections.Generic.List[string]]$Lines) {
    $start = Find-PluginEntryIndex $Lines
    if ($start -lt 0) { return $null }
    $indent = Get-IndentWidth $Lines[$start]
    $end = $Lines.Count
    for ($i = $start + 1; $i -lt $Lines.Count; $i++) {
        $line = $Lines[$i]
        if ($line -match '^\s*$') { continue }
        if ((Get-IndentWidth $line) -le $indent) {
            $end = $i
            break
        }
    }
    return [pscustomobject]@{ Start = $start; End = $end; Indent = $indent }
}

function Get-EntryInsertIndex([System.Collections.Generic.List[string]]$Lines, $Entry) {
    $index = $Entry.End
    for ($i = $Entry.End - 1; $i -gt $Entry.Start; $i--) {
        if ($Lines[$i] -match '^\s*$') {
            $index = $i
        }
        else {
            break
        }
    }
    return $index
}

function Get-PluginConfigInfo([System.Collections.Generic.List[string]]$Lines, $Entry) {
    for ($i = $Entry.Start + 1; $i -lt $Entry.End; $i++) {
        if ((Get-IndentWidth $Lines[$i]) -le $Entry.Indent) { continue }
        if ($Lines[$i] -match '^(?<indent>[ \t]*)config:[ \t]*(?<rest>.*)$') {
            $configIndent = $Matches['indent'].Length
            if ($configIndent -le $Entry.Indent) { continue }
            $rest = $Matches['rest'].Trim()
            if ($rest -ne '' -and $rest -ne '{}') {
                Fail "cordis.patch.yml 中 dsh-mail-plugin 的 config 使用了内联写法，请改为块写法后重试：config: $rest"
            }
            $blockEnd = $i + 1
            while ($blockEnd -lt $Entry.End) {
                $line = $Lines[$blockEnd]
                if ($line -match '^\s*$') {
                    $blockEnd++
                    continue
                }
                if ((Get-IndentWidth $line) -gt $configIndent) {
                    $blockEnd++
                    continue
                }
                break
            }
            return [pscustomobject]@{
                Start       = $i
                End         = $blockEnd
                Indent      = $configIndent
                InlineEmpty = ($rest -eq '{}')
            }
        }
    }
    return $null
}

function Format-YamlScalar([string]$Value) {
    if ($null -eq $Value) { return "''" }
    if ($Value -match '^[A-Za-z0-9._/-]+$') { return $Value }
    return "'" + $Value.Replace("'", "''") + "'"
}

function ConvertFrom-YamlScalar([string]$Value) {
    $trimmed = $Value.Trim()
    if ($trimmed.Length -ge 2 -and $trimmed.StartsWith("'") -and $trimmed.EndsWith("'")) {
        return $trimmed.Substring(1, $trimmed.Length - 2).Replace("''", "'")
    }
    if ($trimmed.Length -ge 2 -and $trimmed.StartsWith('"') -and $trimmed.EndsWith('"')) {
        return $trimmed.Substring(1, $trimmed.Length - 2)
    }
    return $trimmed
}

function Get-PluginConfigValues([System.Collections.Generic.List[string]]$Lines) {
    $values = [ordered]@{}
    $entry = Get-PluginEntryRange $Lines
    if ($null -eq $entry) { return $values }
    $config = Get-PluginConfigInfo $Lines $entry
    if ($null -eq $config) { return $values }
    for ($i = $config.Start + 1; $i -lt $config.End; $i++) {
        if ((Get-IndentWidth $Lines[$i]) -le $config.Indent) { continue }
        if ($Lines[$i] -match '^(?<indent>[ \t]+)(?<key>[A-Za-z0-9_.-]+):[ \t]*(?<value>.*)$') {
            $values[$Matches['key']] = ConvertFrom-YamlScalar $Matches['value']
        }
    }
    return $values
}

function Set-PluginConfigValue([System.Collections.Generic.List[string]]$Lines, [string]$Key, [string]$Value) {
    $entry = Get-PluginEntryRange $Lines
    if ($null -eq $entry) { Fail 'cordis.patch.yml 中找不到 dsh-mail-plugin 条目。' }
    $config = Get-PluginConfigInfo $Lines $entry

    if ($null -eq $config) {
        $insertAt = Get-EntryInsertIndex $Lines $entry
        $childIndent = ' ' * ($entry.Indent + 2)
        $Lines.Insert($insertAt, $childIndent + 'config:')
        $Lines.Insert($insertAt + 1, $childIndent + '  ' + $Key + ': ' + (Format-YamlScalar $Value))
        Write-Log "cordis.patch.yml：已创建 config 并写入 $Key"
        return $true
    }

    if ($config.InlineEmpty) {
        $prefix = ' ' * $config.Indent
        $Lines[$config.Start] = $prefix + 'config:'
        $Lines.Insert($config.Start + 1, (' ' * ($config.Indent + 2)) + $Key + ': ' + (Format-YamlScalar $Value))
        Write-Log "cordis.patch.yml：已写入 $Key"
        return $true
    }

    $keyRegex = '^(?<indent>[ \t]+)' + [regex]::Escape($Key) + ':(?<rest>.*)$'
    for ($i = $config.Start + 1; $i -lt $config.End; $i++) {
        if ($Lines[$i] -match $keyRegex) {
            $newLine = $Matches['indent'] + $Key + ': ' + (Format-YamlScalar $Value)
            if ($Lines[$i] -eq $newLine) {
                return $false
            }
            $Lines[$i] = $newLine
            Write-Log "cordis.patch.yml：已更新 $Key"
            return $true
        }
    }

    $insertAt = $config.End
    $childIndentWidth = $config.Indent + 2
    if ($config.End -gt $config.Start + 1) {
        $firstChild = $Lines[$config.Start + 1]
        if ($firstChild -notmatch '^\s*$' -and (Get-IndentWidth $firstChild) -gt $config.Indent) {
            $childIndentWidth = Get-IndentWidth $firstChild
        }
    }
    for ($i = $config.End - 1; $i -gt $config.Start; $i--) {
        if ($Lines[$i] -match '^\s*$') {
            $insertAt = $i
        }
        else {
            break
        }
    }
    $Lines.Insert($insertAt, (' ' * $childIndentWidth) + $Key + ': ' + (Format-YamlScalar $Value))
    Write-Log "cordis.patch.yml：已补充 $Key"
    return $true
}

function Set-PluginDisabled([System.Collections.Generic.List[string]]$Lines, [bool]$Disabled) {
    $entry = Get-PluginEntryRange $Lines
    if ($null -eq $entry) { Fail 'cordis.patch.yml 中找不到 dsh-mail-plugin 条目。' }
    $config = Get-PluginConfigInfo $Lines $entry
    $value = if ($Disabled) { 'true' } else { 'false' }

    for ($i = $entry.Start + 1; $i -lt $entry.End; $i++) {
        if ($null -ne $config -and $i -ge $config.Start -and $i -lt $config.End) { continue }
        if ($Lines[$i] -match '^(?<indent>[ \t]+)disabled:') {
            $newLine = $Matches['indent'] + 'disabled: ' + $value
            if ($Lines[$i] -eq $newLine) {
                return $false
            }
            $Lines[$i] = $newLine
            Write-Log "cordis.patch.yml：已更新 disabled = $value"
            return $true
        }
    }
    $insertAt = Get-EntryInsertIndex $Lines $entry
    $Lines.Insert($insertAt, (' ' * ($entry.Indent + 2)) + 'disabled: ' + $value)
    Write-Log "cordis.patch.yml：已补充 disabled = $value"
    return $true
}

function Add-PluginEntry([System.Collections.Generic.List[string]]$Lines, $ConfigValues, [bool]$Disabled) {
    $Lines.Add('- id: ' + $script:PluginId)
    $Lines.Add('  name: ' + $script:PluginId)
    $Lines.Add('  config:')
    foreach ($key in $ConfigValues.Keys) {
        $Lines.Add('    ' + $key + ': ' + (Format-YamlScalar ([string]$ConfigValues[$key])))
    }
    $disabledText = if ($Disabled) { 'true' } else { 'false' }
    $Lines.Add('  disabled: ' + $disabledText)
    Write-Log 'cordis.patch.yml：已追加 dsh-mail-plugin 条目'
}

function Remove-PluginEntry([System.Collections.Generic.List[string]]$Lines) {
    $entry = Get-PluginEntryRange $Lines
    if ($null -eq $entry) { return $false }
    $Lines.RemoveRange($entry.Start, $entry.End - $entry.Start)
    while ($Lines.Count -gt 0 -and $Lines[$Lines.Count - 1] -match '^\s*$') {
        $Lines.RemoveAt($Lines.Count - 1)
    }
    $hasItems = $false
    foreach ($line in $Lines) {
        if ($line -match '^[ \t]*-[ \t]+\S') {
            $hasItems = $true
            break
        }
    }
    if (-not $hasItems) {
        $Lines.Add('[]')
    }
    Write-Log 'cordis.patch.yml：已移除 dsh-mail-plugin 条目'
    return $true
}

function Save-PatchFile($Patch, [int]$ExpectedEntries) {
    $text = $Patch.Lines -join $Patch.Eol
    if ($text.Length -gt 0 -and -not $text.EndsWith($Patch.Eol)) {
        $text += $Patch.Eol
    }
    if ($Patch.Raw -eq $text) {
        return $false
    }

    $exists = Test-Path -LiteralPath $Patch.Path -PathType Leaf
    $backup = $null
    if ($exists) {
        $backup = Backup-File $Patch.Path
    }
    Write-TextFile $Patch.Path $text

    try {
        $reloaded = Read-TextFile $Patch.Path
        if ([string]::IsNullOrWhiteSpace($reloaded)) {
            throw '写回后文件为空'
        }
        $entryCount = ([regex]::Matches($reloaded, $script:EntryLineRegex, 'Multiline')).Count
        if ($entryCount -ne $ExpectedEntries) {
            throw "写回后 dsh-mail-plugin 条目数为 $entryCount，期望 $ExpectedEntries"
        }
    }
    catch {
        if ($exists -and $null -ne $backup) {
            Copy-Item -LiteralPath $backup -Destination $Patch.Path -Force
        }
        else {
            Remove-Item -LiteralPath $Patch.Path -Force -ErrorAction SilentlyContinue
        }
        Fail "cordis.patch.yml 写回校验失败，已回滚：$($_.Exception.Message)"
    }
    Write-Log "已写入 $($Patch.Path)"
    return $true
}

# ------------------------------------------------------------- junction -----

function Get-LinkState([string]$LinkPath) {
    $item = Get-Item -LiteralPath $LinkPath -Force -ErrorAction SilentlyContinue
    if ($null -eq $item) {
        return [pscustomobject]@{ Exists = $false; IsLink = $false; LinkType = ''; Target = $null }
    }
    $linkType = [string]$item.LinkType
    $isLink = @('Junction', 'SymbolicLink') -contains $linkType
    $target = $null
    if ($isLink) {
        $targets = @($item.Target)
        if ($targets.Count -gt 0) {
            $target = [string]$targets[0]
            if (-not [System.IO.Path]::IsPathRooted($target)) {
                $target = Join-Path (Split-Path -Parent $LinkPath) $target
            }
        }
    }
    return [pscustomobject]@{ Exists = $true; IsLink = $isLink; LinkType = $linkType; Target = $target }
}

function Remove-JunctionPath([string]$LinkPath) {
    try {
        [System.IO.Directory]::Delete($LinkPath, $false)
        return
    }
    catch {
        # 回退到 Remove-Item（只删链接本身，不加 -Recurse 以免波及目标内容）
    }
    Remove-Item -LiteralPath $LinkPath -Force
}

function Ensure-PluginJunction([string]$LinkPath, [string]$TargetPath, [bool]$Force, [string]$RepairHint = 'install.ps1 -Force') {
    $state = Get-LinkState $LinkPath
    if ($state.Exists -and -not $state.IsLink) {
        Fail "路径已存在但不是 junction/symlink：$LinkPath。为避免误删真实目录，请手工删除后重试。"
    }
    if ($state.Exists -and $state.IsLink -and (Test-SamePath $state.Target $TargetPath)) {
        Write-Log "junction 已指向本仓库，无需修改：$LinkPath"
        return 'unchanged'
    }
    if ($state.Exists -and $state.IsLink) {
        if (-not $Force) {
            Fail "junction 已存在但指向 $($state.Target)，与本仓库 $TargetPath 不一致；使用 $RepairHint 重建。"
        }
        Remove-JunctionPath $LinkPath
        New-Item -ItemType Junction -Path $LinkPath -Target $TargetPath | Out-Null
        Write-Log "已重建 junction：$LinkPath -> $TargetPath"
        return 'replaced'
    }
    New-Item -ItemType Junction -Path $LinkPath -Target $TargetPath | Out-Null
    Write-Log "已创建 junction：$LinkPath -> $TargetPath"
    return 'created'
}
