# intunewin-on-mac guest agent. Invoked over SSH:
#   powershell -NoProfile -File C:\iwm\agent.ps1 -Action run    -Job   C:\iwm\jobs\<id>.json
#   powershell -NoProfile -File C:\iwm\agent.ps1 -Action detect -Rules C:\iwm\jobs\<id>.rules.json
#   powershell -NoProfile -File C:\iwm\agent.ps1 -Action info
# Always prints a single JSON document on stdout.
param(
    [Parameter(Mandatory = $true)][ValidateSet('run', 'detect', 'info', 'apps')][string]$Action,
    [string]$Job,
    [string]$Rules
)
$ErrorActionPreference = 'Stop'
$root = 'C:\iwm'

function Out-Json($obj) { $obj | ConvertTo-Json -Depth 8 -Compress }
function Dbg($m) { if ($env:IWM_DEBUG) { Add-Content "$root\logs\agent.log" "$(Get-Date -Format s) $m" } }

# ---------------------------------------------------------------- run as SYSTEM (like the Intune Management Extension)
function Invoke-Job {
    param([string]$JobFile)
    $job = [IO.File]::ReadAllText($JobFile) | ConvertFrom-Json
    $id = [IO.Path]::GetFileNameWithoutExtension($JobFile)
    $dir = "$root\jobs"
    $cmdFile = "$dir\$id.cmd"; $outFile = "$dir\$id.out"; $rcFile = "$dir\$id.rc"
    Remove-Item $outFile, $rcFile -Force -ErrorAction SilentlyContinue
    $workdir = if ($job.workdir) { $job.workdir } else { $root }
    $timeout = if ($job.timeout) { [int]$job.timeout } else { 1800 }
    $account = if ($job.account) { $job.account } else { 'system' }
    @(
        '@echo off',
        "cd /d `"$workdir`"",
        "($($job.command)) > `"$outFile`" 2>&1",
        "echo %errorlevel% > `"$rcFile`""
    ) | Set-Content -Path $cmdFile -Encoding ASCII

    $taskName = "iwm-$id"
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    $action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$cmdFile`""
    if ($account -eq 'system') {
        $principal = New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    } else {
        # interactive user context (Intune "user" install behaviour): the auto-logged-on user
        $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
    }
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Seconds ($timeout + 60))
    Dbg "register $taskName"
    Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings | Out-Null
    $t0 = Get-Date
    Start-ScheduledTask -TaskName $taskName
    Dbg "started"
    while (-not (Test-Path $rcFile)) {
        Start-Sleep -Seconds 2
        if (((Get-Date) - $t0).TotalSeconds -gt $timeout) {
            Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
            break
        }
    }
    Dbg "rc file seen"
    Start-Sleep -Milliseconds 500
    # NB: use .NET readers, not Get-Content: it decorates strings with PSPath/PSDrive properties and
    # ConvertTo-Json then walks the whole provider object graph (looks like a hang).
    $rc = if (Test-Path $rcFile) { [int]([IO.File]::ReadAllText($rcFile).Trim()) } else { -1 }
    $out = if (Test-Path $outFile) { [IO.File]::ReadAllText($outFile) } else { '' }
    Dbg "rc=$rc outlen=$($out.Length)"
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Dbg "unregistered"
    return @{
        id = $id; command = $job.command; workdir = $workdir; account = $account
        rc = $rc; timed_out = (-not (Test-Path $rcFile))
        duration_s = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1)
        output = $out
    }
}

# ---------------------------------------------------------------- detection rules (Intune semantics: all must pass)
function Expand-Env([string]$s) { [Environment]::ExpandEnvironmentVariables($s) }

function Compare-Values([string]$op, $actual, $expected, [string]$kind) {
    if ($kind -eq 'version') {
        $a = [version]([regex]::Match([string]$actual, '\d+(\.\d+){0,3}').Value)
        $e = [version]([regex]::Match([string]$expected, '\d+(\.\d+){0,3}').Value)
    } elseif ($kind -eq 'integer') {
        $a = [int64]$actual; $e = [int64]$expected
    } else {
        $a = [string]$actual; $e = [string]$expected
    }
    switch ($op) {
        'equal'              { return $a -eq $e }
        'notEqual'           { return $a -ne $e }
        'greaterThan'        { return $a -gt $e }
        'greaterThanOrEqual' { return $a -ge $e }
        'lessThan'           { return $a -lt $e }
        'lessThanOrEqual'    { return $a -le $e }
        default              { return $a -eq $e }
    }
}

function Test-MsiRule($r) {
    $installer = New-Object -ComObject WindowsInstaller.Installer
    $state = $installer.GetType().InvokeMember('ProductState', 'GetProperty', $null, $installer, @($r.productCode))
    $detected = ($state -eq 5)
    $ver = $null
    if ($detected) {
        try { $ver = $installer.GetType().InvokeMember('ProductInfo', 'GetProperty', $null, $installer, @($r.productCode, 'VersionString')) } catch {}
        if ($r.productVersionOperator -and $r.productVersionOperator -ne 'notConfigured' -and $r.productVersion) {
            $detected = Compare-Values $r.productVersionOperator $ver $r.productVersion 'version'
        }
    }
    return @{ detected = $detected; state = $state; version = $ver }
}

function Test-FileRule($r) {
    $p = Join-Path (Expand-Env $r.path) $r.fileOrFolderName
    $exists = Test-Path -LiteralPath $p
    $detail = @{ path = $p; exists = $exists }
    switch ($r.operationType) {
        'exists'    { $detail.detected = $exists }
        'notExists' { $detail.detected = -not $exists }
        'version'   {
            $v = if ($exists) { (Get-Item -LiteralPath $p).VersionInfo.ProductVersion } else { $null }
            if (-not $v -and $exists) { $v = (Get-Item -LiteralPath $p).VersionInfo.FileVersion }
            $detail.version = $v
            $detail.detected = $exists -and (Compare-Values $r.operator $v $r.comparisonValue 'version')
        }
        'sizeInMB'  {
            $mb = if ($exists) { [math]::Round((Get-Item -LiteralPath $p).Length / 1MB) } else { 0 }
            $detail.sizeMB = $mb
            $detail.detected = $exists -and (Compare-Values $r.operator $mb $r.comparisonValue 'integer')
        }
        default     { $detail.detected = $exists; $detail.note = "operationType $($r.operationType) approximated as exists" }
    }
    return $detail
}

function Test-RegistryRule($r) {
    $kp = $r.keyPath -replace '^HKEY_LOCAL_MACHINE\\', '' -replace '^HKLM\\', ''
    $hive = [Microsoft.Win32.RegistryHive]::LocalMachine
    if ($r.keyPath -match '^(HKEY_CURRENT_USER|HKCU)\\') { $hive = [Microsoft.Win32.RegistryHive]::CurrentUser; $kp = $r.keyPath -replace '^(HKEY_CURRENT_USER|HKCU)\\', '' }
    elseif ($r.keyPath -match '^(HKEY_CLASSES_ROOT|HKCR)\\') { $hive = [Microsoft.Win32.RegistryHive]::ClassesRoot; $kp = $r.keyPath -replace '^(HKEY_CLASSES_ROOT|HKCR)\\', '' }
    elseif ($r.keyPath -match '^(HKEY_USERS|HKU)\\') { $hive = [Microsoft.Win32.RegistryHive]::Users; $kp = $r.keyPath -replace '^(HKEY_USERS|HKU)\\', '' }
    $view = if ($r.check32BitOn64System) { [Microsoft.Win32.RegistryView]::Registry32 } else { [Microsoft.Win32.RegistryView]::Registry64 }
    $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey($hive, $view)
    $key = $base.OpenSubKey($kp)
    $detail = @{ key = $r.keyPath; valueName = $r.valueName; keyExists = ($null -ne $key) }
    $val = $null
    if ($key) {
        $val = $key.GetValue($(if ($r.valueName) { $r.valueName } else { '' }))
        $detail.value = if ($null -ne $val) { [string]$val } else { $null }
    }
    switch ($r.operationType) {
        'exists'       { $detail.detected = if ($r.valueName) { $null -ne $val } else { $null -ne $key } }
        'doesNotExist' { $detail.detected = if ($r.valueName) { $null -eq $val } else { $null -eq $key } }
        'string'       { $detail.detected = ($null -ne $val) -and (Compare-Values $r.operator $val $r.comparisonValue 'string') }
        'integer'      { $detail.detected = ($null -ne $val) -and (Compare-Values $r.operator $val $r.comparisonValue 'integer') }
        'version'      { $detail.detected = ($null -ne $val) -and (Compare-Values $r.operator $val $r.comparisonValue 'version') }
        default        { $detail.detected = $null -ne $key }
    }
    return $detail
}

function Test-ScriptRule($r) {
    $f = "$root\jobs\detect-$([guid]::NewGuid().ToString('N')).ps1"
    Set-Content -Path $f -Value $r.scriptContent -Encoding UTF8
    $out = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $f 2>&1 | Out-String
    $rc = $LASTEXITCODE
    Remove-Item $f -Force -ErrorAction SilentlyContinue
    # Intune: detected when exit code is 0 AND something was written to STDOUT
    return @{ detected = ($rc -eq 0 -and $out.Trim().Length -gt 0); rc = $rc; output = $out.Trim() }
}

function Invoke-Detection {
    param([string]$RulesFile)
    $rules = [IO.File]::ReadAllText($RulesFile) | ConvertFrom-Json
    if ($rules -isnot [array]) { $rules = @($rules) }
    $results = @()
    foreach ($r in $rules) {
        $t = ($r.'@odata.type' -split '\.')[-1]
        try {
            $res = switch ($t) {
                'win32LobAppProductCodeRule'       { Test-MsiRule $r }
                'win32LobAppFileSystemRule'        { Test-FileRule $r }
                'win32LobAppRegistryRule'          { Test-RegistryRule $r }
                'win32LobAppPowerShellScriptRule'  { Test-ScriptRule $r }
                default                            { @{ detected = $false; error = "unknown rule type $t" } }
            }
        } catch {
            $res = @{ detected = $false; error = $_.Exception.Message }
        }
        $res.type = $t
        $results += $res
    }
    $all = ($results.Count -gt 0) -and (($results | Where-Object { -not $_.detected }).Count -eq 0)
    return @{ detected = $all; rules = $results }
}

# ---------------------------------------------------------------- misc
function Get-Info {
    $os = Get-CimInstance Win32_OperatingSystem
    $cs = Get-CimInstance Win32_ComputerSystem
    return @{
        computer = $env:COMPUTERNAME; user = $env:USERNAME
        os = $os.Caption; version = $os.Version; build = $os.BuildNumber
        arch = $env:PROCESSOR_ARCHITECTURE; ram_mb = [int]($cs.TotalPhysicalMemory / 1MB)
        uptime_s = [int]((Get-Date) - $os.LastBootUpTime).TotalSeconds
        ready = (Test-Path "$root\ready.txt")
    }
}

function Get-InstalledApps {
    $paths = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
             'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
             'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*'
    return @(Get-ItemProperty $paths -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName } |
        Select-Object DisplayName, DisplayVersion, Publisher, PSChildName, UninstallString |
        Sort-Object DisplayName)
}

switch ($Action) {
    'run'    { Out-Json (Invoke-Job -JobFile $Job) }
    'detect' { Out-Json (Invoke-Detection -RulesFile $Rules) }
    'info'   { Out-Json (Get-Info) }
    'apps'   { Out-Json (Get-InstalledApps) }
}
