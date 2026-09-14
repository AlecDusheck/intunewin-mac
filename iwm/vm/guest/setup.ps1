# intunewin-on-mac guest setup. Runs from the config ISO (X:\iwm\setup.ps1) during the
# specialize pass (as SYSTEM) and again at first logon as a fallback. Idempotent.
$ErrorActionPreference = 'Continue'
$root = 'C:\iwm'
$src = Split-Path -Parent $MyInvocation.MyCommand.Path
foreach ($d in @($root, "$root\jobs", "$root\pkg", "$root\logs", "$root\tmp")) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
}
if (Test-Path "$root\ready.txt") { exit 0 }
Start-Transcript -Path "$root\logs\setup.log" -Append | Out-Null
Write-Host "=== iwm guest setup $(Get-Date -Format s) from $src as $env:USERNAME"

# 1. Drivers (virtio-net etc.). pnputil /subdirs needs Windows 10 1903+.
if (Test-Path "$src\drivers") {
    Write-Host "--- installing drivers"
    & pnputil.exe /add-driver "$src\drivers\*.inf" /subdirs /install 2>&1 | Write-Host
}

# 2. OpenSSH server (Win32-OpenSSH portable zip; works offline, no Feature-on-Demand needed).
$sshDir = "$env:ProgramFiles\OpenSSH"
if (-not (Test-Path "$sshDir\sshd.exe")) {
    Write-Host "--- installing OpenSSH"
    $zip = Get-ChildItem "$src\OpenSSH*.zip" | Select-Object -First 1
    Remove-Item "$root\tmp\ssh" -Recurse -Force -ErrorAction SilentlyContinue
    Expand-Archive -Path $zip.FullName -DestinationPath "$root\tmp\ssh" -Force
    $inner = Get-ChildItem "$root\tmp\ssh" -Directory | Select-Object -First 1
    New-Item -ItemType Directory -Force -Path $sshDir | Out-Null
    Copy-Item "$($inner.FullName)\*" $sshDir -Recurse -Force
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$sshDir\install-sshd.ps1" 2>&1 | Write-Host
}
# firewall + service + default shell
& netsh.exe advfirewall firewall add rule name="OpenSSH Server (sshd)" dir=in action=allow protocol=TCP localport=22 2>&1 | Write-Host
Set-Service -Name sshd -StartupType Automatic -ErrorAction SilentlyContinue
New-Item -Path 'HKLM:\SOFTWARE\OpenSSH' -Force | Out-Null
New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell -PropertyType String -Force `
    -Value "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" | Out-Null
$machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
if ($machinePath -notlike "*$sshDir*") {
    [Environment]::SetEnvironmentVariable('Path', "$machinePath;$sshDir", 'Machine')
}
# authorized keys for administrators
$sshData = "$env:ProgramData\ssh"
New-Item -ItemType Directory -Force -Path $sshData | Out-Null
if (Test-Path "$src\authorized_keys") {
    $ak = "$sshData\administrators_authorized_keys"
    Copy-Item "$src\authorized_keys" $ak -Force
    & icacls.exe $ak /inheritance:r /grant "SYSTEM:(F)" /grant "BUILTIN\Administrators:(F)" 2>&1 | Write-Host
}
# sshd_config: make sure key auth + admin keys file are active (defaults do this, but be explicit)
$cfg = "$sshData\sshd_config"
if (-not (Test-Path $cfg) -and (Test-Path "$sshDir\sshd_config_default")) { Copy-Item "$sshDir\sshd_config_default" $cfg }
if (Test-Path $cfg) {
    $c = Get-Content $cfg -Raw
    if ($c -notmatch '(?m)^\s*PubkeyAuthentication\s+yes') { Add-Content $cfg "`nPubkeyAuthentication yes" }
    if ($c -notmatch '(?m)^\s*PasswordAuthentication\s+yes') { Add-Content $cfg "`nPasswordAuthentication yes" }
}
Start-Service sshd -ErrorAction SilentlyContinue

# 3. Make the VM boring and deterministic: no sleep, no auto-updates, no lock screen.
& powercfg.exe /change standby-timeout-ac 0 | Out-Null
& powercfg.exe /change monitor-timeout-ac 0 | Out-Null
& powercfg.exe /change hibernate-timeout-ac 0 | Out-Null
& powercfg.exe /h off | Out-Null
& reg.exe add "HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" /v NoAutoUpdate /t REG_DWORD /d 1 /f | Out-Null
& reg.exe add "HKLM\SOFTWARE\Policies\Microsoft\Windows\Personalization" /v NoLockScreen /t REG_DWORD /d 1 /f | Out-Null
& reg.exe add "HKLM\SOFTWARE\Policies\Microsoft\Windows\CloudContent" /v DisableWindowsConsumerFeatures /t REG_DWORD /d 1 /f | Out-Null
# Time sync (QEMU RTC may be off); harmless if it fails
& w32tm.exe /resync 2>&1 | Out-Null

# 4. UEFI fallback loader: copy bootmgfw.efi to \EFI\Boot\boot<arch>.efi on the EFI system partition
#    so the firmware boots this disk even if its NVRAM boot entries are lost (QEMU/edk2 quirk).
try {
    $letter = 'S:'
    & mountvol.exe $letter /S | Out-Null
    $bootmgfw = "$letter\EFI\Microsoft\Boot\bootmgfw.efi"
    if (Test-Path $bootmgfw) {
        $fallback = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'bootaa64.efi' } else { 'bootx64.efi' }
        New-Item -ItemType Directory -Force -Path "$letter\EFI\Boot" | Out-Null
        Copy-Item $bootmgfw "$letter\EFI\Boot\$fallback" -Force
        Write-Host "--- installed UEFI fallback loader $fallback"
    }
    & mountvol.exe $letter /D | Out-Null
} catch { Write-Host "fallback loader: $_" }

# 5. Guest agent + marker
Copy-Item "$src\agent.ps1" "$root\agent.ps1" -Force
Set-ItemProperty "$root\agent.ps1" -Name IsReadOnly -Value $false   # files copied off the ISO are read-only
Set-Content -Path "$root\ready.txt" -Value (Get-Date -Format s)
Write-Host "=== iwm guest setup done"
Stop-Transcript | Out-Null
