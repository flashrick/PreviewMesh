# Run from setup.sh. No secrets are passed to Windows.
[CmdletBinding()]
param(
    [ValidateSet('Install', 'Apply', 'Refresh', 'Check')][string]$Mode = 'Check',
    [string]$Distro,
    [string]$LanIP,
    [string]$AllowedSubnet = 'auto'
)
$ErrorActionPreference = 'Stop'
$directory = Join-Path $env:ProgramData 'PreviewMesh'
$configPath = Join-Path $directory 'lan.json'
$installedScript = Join-Path $directory 'setup-lan.ps1'
$ruleName = 'PreviewMesh-LAN-18080'
$hypervName = 'PreviewMesh-WSL-18080'
$taskName = 'PreviewMesh LAN forwarding'
function Quote-Argument([string]$value) { return "'" + $value.Replace("'", "''") + "'" }
function Invoke-WSL([string[]]$Arguments) {
    $result = & wsl.exe -d $Distro -- @Arguments
    if ($LASTEXITCODE -ne 0) { throw 'WSL command failed. Run wsl --update in Windows, reopen Ubuntu and retry setup.' }
    return ($result -join [Environment]::NewLine)
}
function Test-Entry {
    $name = "previewmesh-check.$LanIP.sslip.io"
    $addresses = @([System.Net.Dns]::GetHostAddresses($name) | Where-Object AddressFamily -eq InterNetwork | ForEach-Object IPAddressToString | Select-Object -Unique)
    if ($addresses.Count -ne 1 -or $addresses[0] -ne $LanIP) {
        throw "Windows DNS does not resolve $name to $LanIP. Ask your network administrator about private-IP DNS filtering."
    }
    $status = & curl.exe --noproxy '*' --max-time 10 -sS -o NUL -w '%{http_code}' -H 'Host: previewmesh-ingress-check.invalid' "http://$($LanIP):18080/"
    if ($LASTEXITCODE -ne 0 -or $status -ne '404') {
        throw "Cannot reach Traefik at $($LanIP):18080 (expected 404). Check the network profile, firewall and WSL forwarding."
    }
    Write-Output 'PASS: Windows DNS and LAN entry. Ask a colleague to check from a second LAN machine too.'
}
if ($Mode -eq 'Install') {
    $command = '& ' + (Quote-Argument $PSCommandPath) + ' -Mode Apply -Distro ' + (Quote-Argument $Distro) +
        ' -LanIP ' + (Quote-Argument $LanIP) + ' -AllowedSubnet ' + (Quote-Argument $AllowedSubnet)
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
    Write-Output 'Windows administrator access is needed to configure the preview LAN entry.'
    $process = Start-Process powershell.exe -Verb RunAs -Wait -PassThru -ArgumentList "-NoProfile -ExecutionPolicy Bypass -EncodedCommand $encoded"
    if ($process.ExitCode -ne 0) { throw 'Windows setup did not complete. Rerun setup.sh install after resolving the displayed error.' }
    exit 0
}
if ($Mode -eq 'Refresh') {
    $saved = Get-Content -Raw $configPath | ConvertFrom-Json
    $Distro = $saved.distro; $LanIP = $saved.lan_ip; $AllowedSubnet = $saved.allowed_subnet
    # Do not start a stopped distro just to refresh a port mapping.
    $running = ((& wsl.exe --list --running --quiet) -join [Environment]::NewLine).Replace([string][char]0, '')
    if (-not ($running -split "\r?\n" | Where-Object { $_.Trim() -eq $Distro })) { exit 0 }
}
if ($Distro -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]*$') { throw 'Unsupported WSL distribution name.' }
$address = [System.Net.IPAddress]::Parse($LanIP)
if ($address.AddressFamily -ne 'InterNetwork') { throw 'A LAN IPv4 address is required.' }
$bytes = $address.GetAddressBytes()
if (-not ($bytes[0] -eq 10 -or ($bytes[0] -eq 172 -and $bytes[1] -ge 16 -and $bytes[1] -le 31) -or ($bytes[0] -eq 192 -and $bytes[1] -eq 168))) {
    throw 'Use a private LAN IPv4, not a public or loopback address.'
}
if ($Mode -eq 'Check') { Test-Entry; exit 0 }
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run Windows setup as administrator.' }
$interface = Get-NetIPAddress -AddressFamily IPv4 -IPAddress $LanIP | Select-Object -First 1
if (-not $interface) { throw 'lan_ip must be the Windows LAN address, not the WSL NAT address.' }
$profile = Get-NetConnectionProfile -InterfaceIndex $interface.InterfaceIndex
if ($profile.NetworkCategory -eq 'Public') {
    throw 'This Windows connection is Public. For a trusted LAN, change its network profile to Private in Settings > Network & Internet, then retry. No firewall protection was disabled.'
}
$subnet = $AllowedSubnet
if ($subnet -eq 'auto') {
    $networkBytes = $address.GetAddressBytes()
    for ($index = 0; $index -lt 4; $index++) {
        $bits = [Math]::Min(8, [Math]::Max(0, [int]$interface.PrefixLength - 8 * $index))
        $networkBytes[$index] = $networkBytes[$index] -band [int](256 - [Math]::Pow(2, 8 - $bits))
    }
    $subnet = ([System.Net.IPAddress]::new($networkBytes)).ToString() + '/' + $interface.PrefixLength
}
elseif ($subnet -notmatch '^\d{1,3}(\.\d{1,3}){3}/\d{1,2}$') { throw 'Invalid allowed_subnet.' }
$networkMode = (Invoke-WSL @('wslinfo', '--networking-mode')).Trim()
if ($networkMode -notin @('nat', 'mirrored')) { throw "Unsupported WSL networking mode: $networkMode. Use NAT or mirrored networking." }
if (Test-Path $configPath) {
    $saved = Get-Content -Raw $configPath | ConvertFrom-Json
    if ($saved.distro -ne $Distro) {
        throw 'An existing PreviewMesh Windows installation uses a different distro. Review its task, portproxy and firewall rule before changing ownership.'
    }
} else {
    $listeners = @(Get-NetTCPConnection -LocalPort 18080 -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalAddress -in @($LanIP, '0.0.0.0', '::') })
    if ($listeners.Count -gt 0 -and $networkMode -eq 'nat') { throw 'Windows LAN port 18080 is already occupied.' }
    if (Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue) { throw 'A firewall rule with the PreviewMesh name already exists. Review it first.' }
    if ((Get-Command Get-NetFirewallHyperVRule -ErrorAction SilentlyContinue) -and
        (Get-NetFirewallHyperVRule -Name $hypervName -ErrorAction SilentlyContinue)) {
        throw 'A Hyper-V firewall rule with the PreviewMesh name already exists. Review it first.'
    }
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) { throw 'A task with the PreviewMesh name already exists. Review it first.' }
}
if ($Mode -eq 'Apply') {
    if (-not (Test-Path $directory)) { New-Item -ItemType Directory -Path $directory | Out-Null }
    & icacls.exe $directory /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Cannot protect the Windows helper directory.' }
    if ([IO.Path]::GetFullPath($PSCommandPath) -ne [IO.Path]::GetFullPath($installedScript)) {
        Copy-Item $PSCommandPath $installedScript -Force
    }
    $previousIP = if ($saved -and $saved.lan_ip -ne $LanIP) { $saved.lan_ip } elseif ($saved) { $saved.previous_lan_ip } else { $null }
    if ($previousIP) {
        $busy = @(Get-NetTCPConnection -LocalPort 18080 -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalAddress -eq $LanIP })
        if ($busy.Count -gt 0 -and $saved.lan_ip -ne $LanIP) { throw 'The new LAN entry port is already occupied.' }
    }
    @{ distro = $Distro; lan_ip = $LanIP; previous_lan_ip = $previousIP; allowed_subnet = $AllowedSubnet; network_mode = $networkMode } | ConvertTo-Json | Set-Content -Encoding UTF8 $configPath
}
if ($networkMode -eq 'nat') {
    $interfaces = (Invoke-WSL @('ip', '-j', '-4', 'address', 'show', 'dev', 'eth0')) | ConvertFrom-Json
    $wslAddress = @($interfaces.addr_info | Where-Object scope -eq 'global' | ForEach-Object local)[0]
    if (-not $wslAddress) { throw 'Cannot determine the WSL NAT address.' }
    [void][System.Net.IPAddress]::Parse($wslAddress)
    if ($saved -and (Get-Command Get-NetFirewallHyperVRule -ErrorAction SilentlyContinue)) {
        if (Get-NetFirewallHyperVRule -Name $hypervName -ErrorAction SilentlyContinue) { Remove-NetFirewallHyperVRule -Name $hypervName }
    }
    Start-Service iphlpsvc
    & netsh.exe interface portproxy add v4tov4 "listenaddress=$LanIP" listenport=18080 "connectaddress=$wslAddress" connectport=18080 protocol=tcp | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Cannot update Windows port forwarding.' }
} else {
    if (-not (Get-Command New-NetFirewallHyperVRule -ErrorAction SilentlyContinue)) {
        throw 'Mirrored WSL requires Hyper-V firewall support. Update Windows/WSL, then retry.'
    }
    if ($saved -and $saved.network_mode -eq 'nat') {
        & netsh.exe interface portproxy delete v4tov4 "listenaddress=$LanIP" listenport=18080 | Out-Null
    }
    if (Get-NetFirewallHyperVRule -Name $hypervName -ErrorAction SilentlyContinue) { Remove-NetFirewallHyperVRule -Name $hypervName }
    $hyperv = @{ Name = $hypervName; DisplayName = 'PreviewMesh WSL LAN entry'; Direction = 'Inbound'; VMCreatorId = '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}'; Protocol = 'TCP'; LocalPorts = 18080; LocalAddresses = $LanIP; RemoteAddresses = $subnet; Action = 'Allow' }
    New-NetFirewallHyperVRule @hyperv | Out-Null
}
if (Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue) { Remove-NetFirewallRule -Name $ruleName }
New-NetFirewallRule -Name $ruleName -DisplayName 'PreviewMesh LAN entry' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 18080 -LocalAddress $LanIP -RemoteAddress $subnet -Profile Private,Domain | Out-Null
if ($Mode -eq 'Apply') {
    $arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $installedScript + '" -Mode Refresh'
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $triggers = @((New-ScheduledTaskTrigger -AtLogOn -User $user), (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)))
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers -Principal $taskPrincipal -Description 'Keep WSL preview LAN forwarding current while this user distro is running.' -Force | Out-Null
}
if ($Mode -eq 'Apply' -and $previousIP) {
    & netsh.exe interface portproxy delete v4tov4 "listenaddress=$previousIP" listenport=18080 | Out-Null
    @{ distro = $Distro; lan_ip = $LanIP; allowed_subnet = $AllowedSubnet; network_mode = $networkMode } | ConvertTo-Json | Set-Content -Encoding UTF8 $configPath
}
Test-Entry
