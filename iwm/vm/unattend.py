"""Generate an autounattend.xml for a fully unattended Windows 10/11 install under QEMU.

What it does:
  windowsPE  : bypass TPM/SecureBoot/RAM checks, wipe disk 0 (GPT: EFI+MSR+OS), pick the edition,
               accept the EULA with a generic (non-activating) product key.
  specialize : name the machine, run iwm\\setup.ps1 from the config ISO as SYSTEM
               (installs virtio drivers + OpenSSH server, drops the guest agent).
  oobeSystem : skip every OOBE page, create a local admin, auto-logon, run setup.ps1 again
               as a fallback (it is idempotent).
"""
from __future__ import annotations

from xml.sax.saxutils import escape

# Generic keys published by Microsoft for unattended edition selection (they do not activate).
GENERIC_KEYS = {
    "Windows 11 Pro": "VK7JG-NPHTM-C97JM-9MPGT-3V66T",
    "Windows 10 Pro": "VK7JG-NPHTM-C97JM-9MPGT-3V66T",
    "Windows 11 Home": "YTMG3-N6DKC-DKB77-7M9GH-8HVX7",
    "Windows 11 Enterprise": "XGVPP-NMH47-7TTHJ-W3FW7-8HV2C",
    "Windows 11 Education": "YNMGQ-8RYV3-4PGQ3-C8XTP-7CFBY",
}

# Search all plausible drive letters for the config ISO and run the setup script.
FIND_AND_RUN = (
    'cmd.exe /c "for %d in (C D E F G H I J K) do @if exist %d:\\iwm\\setup.ps1 '
    'powershell.exe -NoProfile -ExecutionPolicy Bypass -File %d:\\iwm\\setup.ps1"'
)


def autounattend_xml(
    arch: str = "arm64",
    edition: str = "Windows 11 Pro",
    username: str = "iwm",
    password: str = "iwm",
    computer_name: str = "IWM-TEST",
    product_key: str | None = None,
    locale: str = "en-US",
    timezone: str = "UTC",
) -> str:
    parch = {"arm64": "arm64", "x64": "amd64", "amd64": "amd64", "x86": "x86"}[arch]
    key = product_key or GENERIC_KEYS.get(edition, GENERIC_KEYS["Windows 11 Pro"])
    comp = f'processorArchitecture="{parch}" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS"'
    xmlns = 'xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
    pw = escape(password)
    user = escape(username)

    def bypass(i: int, name: str) -> str:
        return f"""
        <RunSynchronousCommand wcm:action="add">
          <Order>{i}</Order>
          <Path>cmd.exe /c reg add HKLM\\SYSTEM\\Setup\\LabConfig /v {name} /t REG_DWORD /d 1 /f</Path>
        </RunSynchronousCommand>"""

    return f"""<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend">
  <settings pass="windowsPE">
    <component name="Microsoft-Windows-International-Core-WinPE" {comp} {xmlns}>
      <SetupUILanguage><UILanguage>{locale}</UILanguage></SetupUILanguage>
      <InputLocale>{locale}</InputLocale>
      <SystemLocale>{locale}</SystemLocale>
      <UILanguage>{locale}</UILanguage>
      <UserLocale>{locale}</UserLocale>
    </component>
    <component name="Microsoft-Windows-Setup" {comp} {xmlns}>
      <RunSynchronous>{bypass(1, "BypassTPMCheck")}{bypass(2, "BypassSecureBootCheck")}{bypass(3, "BypassRAMCheck")}{bypass(4, "BypassStorageCheck")}{bypass(5, "BypassCPUCheck")}
      </RunSynchronous>
      <DiskConfiguration>
        <WillShowUI>OnError</WillShowUI>
        <Disk wcm:action="add">
          <DiskID>0</DiskID>
          <WillWipeDisk>true</WillWipeDisk>
          <CreatePartitions>
            <CreatePartition wcm:action="add"><Order>1</Order><Type>EFI</Type><Size>300</Size></CreatePartition>
            <CreatePartition wcm:action="add"><Order>2</Order><Type>MSR</Type><Size>16</Size></CreatePartition>
            <CreatePartition wcm:action="add"><Order>3</Order><Type>Primary</Type><Extend>true</Extend></CreatePartition>
          </CreatePartitions>
          <ModifyPartitions>
            <ModifyPartition wcm:action="add"><Order>1</Order><PartitionID>1</PartitionID><Format>FAT32</Format><Label>System</Label></ModifyPartition>
            <ModifyPartition wcm:action="add"><Order>2</Order><PartitionID>2</PartitionID></ModifyPartition>
            <ModifyPartition wcm:action="add"><Order>3</Order><PartitionID>3</PartitionID><Format>NTFS</Format><Label>Windows</Label><Letter>C</Letter></ModifyPartition>
          </ModifyPartitions>
        </Disk>
      </DiskConfiguration>
      <ImageInstall>
        <OSImage>
          <InstallFrom>
            <MetaData wcm:action="add"><Key>/IMAGE/NAME</Key><Value>{escape(edition)}</Value></MetaData>
          </InstallFrom>
          <InstallTo><DiskID>0</DiskID><PartitionID>3</PartitionID></InstallTo>
          <WillShowUI>OnError</WillShowUI>
        </OSImage>
      </ImageInstall>
      <UserData>
        <ProductKey><Key>{key}</Key><WillShowUI>Never</WillShowUI></ProductKey>
        <AcceptEula>true</AcceptEula>
        <FullName>{user}</FullName>
        <Organization>intunewin-on-mac</Organization>
      </UserData>
    </component>
  </settings>
  <settings pass="specialize">
    <component name="Microsoft-Windows-Shell-Setup" {comp} {xmlns}>
      <ComputerName>{escape(computer_name)}</ComputerName>
      <TimeZone>{escape(timezone)}</TimeZone>
    </component>
    <component name="Microsoft-Windows-Deployment" {comp} {xmlns}>
      <RunSynchronous>
        <RunSynchronousCommand wcm:action="add">
          <Order>1</Order>
          <Path>cmd.exe /c reg add HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\OOBE /v BypassNRO /t REG_DWORD /d 1 /f</Path>
        </RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add">
          <Order>2</Order>
          <Path>{escape(FIND_AND_RUN)}</Path>
        </RunSynchronousCommand>
      </RunSynchronous>
    </component>
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-International-Core" {comp} {xmlns}>
      <InputLocale>{locale}</InputLocale>
      <SystemLocale>{locale}</SystemLocale>
      <UILanguage>{locale}</UILanguage>
      <UserLocale>{locale}</UserLocale>
    </component>
    <component name="Microsoft-Windows-Shell-Setup" {comp} {xmlns}>
      <OOBE>
        <HideEULAPage>true</HideEULAPage>
        <HideLocalAccountScreen>true</HideLocalAccountScreen>
        <HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
        <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
        <ProtectYourPC>3</ProtectYourPC>
        <SkipMachineOOBE>true</SkipMachineOOBE>
        <SkipUserOOBE>true</SkipUserOOBE>
      </OOBE>
      <UserAccounts>
        <LocalAccounts>
          <LocalAccount wcm:action="add">
            <Name>{user}</Name>
            <DisplayName>{user}</DisplayName>
            <Group>Administrators</Group>
            <Password><Value>{pw}</Value><PlainText>true</PlainText></Password>
          </LocalAccount>
        </LocalAccounts>
      </UserAccounts>
      <AutoLogon>
        <Enabled>true</Enabled>
        <Username>{user}</Username>
        <Password><Value>{pw}</Value><PlainText>true</PlainText></Password>
        <LogonCount>999</LogonCount>
      </AutoLogon>
      <FirstLogonCommands>
        <SynchronousCommand wcm:action="add">
          <Order>1</Order>
          <CommandLine>{escape(FIND_AND_RUN)}</CommandLine>
          <Description>intunewin-on-mac guest setup (fallback)</Description>
          <RequiresUserInput>false</RequiresUserInput>
        </SynchronousCommand>
      </FirstLogonCommands>
    </component>
  </settings>
</unattend>
"""
