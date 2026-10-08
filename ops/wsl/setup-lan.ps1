# Run from setup.sh. No secrets are passed to Windows.
[CmdletBinding()]
param(
    [ValidateSet('Install', 'Apply', 'Refresh', 'Check')][string]$Mode = 'Check',
    [string]$Distro,
    [string]$LanIP,
    [string]$AllowedSubnet = 'auto'
)
$ErrorActionPreference = 'Stop'
# WSL captures diagnostics as UTF-8; Windows PowerShell otherwise uses its local code page.
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$directory = Join-Path $env:ProgramData 'PreviewMesh'
$configPath = Join-Path $directory 'lan.json'
$installedScript = Join-Path $directory 'setup-lan.ps1'
$ruleName = 'PreviewMesh-LAN-18080'
$hypervName = 'PreviewMesh-WSL-18080'
$taskName = 'PreviewMesh LAN forwarding'
function Quote-Argument([string]$value) { return "'" + $value.Replace("'", "''") + "'" }
function Get-LanInterface([string]$Address) {
    $interface = Get-NetIPAddress -AddressFamily IPv4 -IPAddress $Address -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $interface) {
        throw "Configured LAN IP $Address is not assigned to Windows. Run setup.sh init with the same config, choose WSL and select the current Windows LAN IPv4 address, then rerun install."
    }
    $profile = Get-NetConnectionProfile -InterfaceIndex $interface.InterfaceIndex -ErrorAction SilentlyContinue
    if (-not $profile) { throw 'No Windows connection profile was found. Select the address of the active Windows LAN connection in setup.sh init.' }
    if ($profile.NetworkCategory -eq 'Public') {
        throw 'This Windows connection is Public. Only for a trusted home or office LAN, set this connection to Private in Settings > Network & Internet > connection properties, then retry install. PreviewMesh requires a Private or Domain network.'
    }
    return $interface
}
function New-SetupResultPath { return [IO.Path]::GetTempFileName() }
function Invoke-AdministratorSetup([string]$ScriptPath) {
    $resultPath = New-SetupResultPath
    try {
        $apply = '& ' + (Quote-Argument $ScriptPath) + ' -Mode Apply -Distro ' + (Quote-Argument $Distro) +
            ' -LanIP ' + (Quote-Argument $LanIP) + ' -AllowedSubnet ' + (Quote-Argument $AllowedSubnet)
        $quotedResult = Quote-Argument $resultPath
        # RunAs cannot redirect standard streams. Return the child error through a caller-readable file.
        $command = @"
`$ErrorActionPreference = 'Stop'
try {
    $apply
    @{ ok = `$true; error = '' } | ConvertTo-Json -Compress | Set-Content -LiteralPath $quotedResult -Encoding UTF8
    exit 0
} catch {
    @{ ok = `$false; error = (`$_ | Out-String).Trim() } | ConvertTo-Json -Compress | Set-Content -LiteralPath $quotedResult -Encoding UTF8
    exit 1
}
"@
        $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
        Write-Output 'Windows administrator access is needed to configure the preview LAN entry.'
        $process = Start-Process powershell.exe -Verb RunAs -Wait -PassThru -ArgumentList "-NoProfile -ExecutionPolicy Bypass -EncodedCommand $encoded"
        $raw = Get-Content -LiteralPath $resultPath -Raw -ErrorAction SilentlyContinue
        if ([string]::IsNullOrWhiteSpace($raw)) {
            $exitCode = if ($null -ne $process) { $process.ExitCode } else { 'unavailable' }
            throw "Windows administrator setup did not return a result (exit code: $exitCode). Check the administrator prompt or child window, then rerun install."
        }
        try { $result = $raw | ConvertFrom-Json } catch { throw 'Windows administrator setup returned invalid JSON; rerun install.' }
        if ($null -eq $result -or $result.ok -isnot [bool]) { throw 'Windows administrator setup returned an invalid result; rerun install.' }
        if (-not $result.ok) {
            $detail = if ([string]::IsNullOrWhiteSpace([string]$result.error)) { 'The administrator process reported failure without details.' } else { [string]$result.error }
            throw "Windows administrator setup failed:`n$detail"
        }
        if ($null -ne $process -and $null -ne $process.ExitCode -and $process.ExitCode -ne 0) {
            throw "Windows administrator setup reported success but exited with code $($process.ExitCode); rerun install."
        }
        Write-Output 'PASS: Windows administrator setup completed.'
    } finally {
        Remove-Item -LiteralPath $resultPath -Force -ErrorAction SilentlyContinue
    }
}
function Invoke-WSL([string[]]$Arguments) {
    $result = & wsl.exe -d $Distro -- @Arguments
    if ($LASTEXITCODE -ne 0) { throw 'WSL command failed. Run wsl --update in Windows, reopen Ubuntu and retry setup.' }
    return ($result -join [Environment]::NewLine)
}
function Get-WslNetworkMode {
    $networkMode = (Invoke-WSL @('wslinfo', '--networking-mode')).Trim()
    if ($networkMode -notin @('nat', 'mirrored')) {
        throw "Unsupported WSL networking mode: $networkMode. Use NAT or mirrored networking."
    }
    return $networkMode
}
function Assert-MirroredHostAccess([string]$NetworkMode) {
    if ($NetworkMode -ne 'mirrored') { return }
    $settingsPath = Join-Path $env:USERPROFILE '.wslconfig'
    $enabled = $false
    $section = ''
    # This setting lets Windows verify WSL through the same LAN address used by clients.
    if (Test-Path -LiteralPath $settingsPath) {
        foreach ($line in [IO.File]::ReadAllLines($settingsPath)) {
            if ($line -match '^\s*\[([^\]]+)\]\s*(?:[#;].*)?$') {
                $section = $Matches[1].Trim()
            } elseif ($section -eq 'experimental' -and $line -match '^\s*hostAddressLoopback\s*=\s*([^#;]*)') {
                $enabled = $Matches[1].Trim() -eq 'true'
            }
        }
    }
    if (-not $enabled) {
        throw "WSL mirrored networking requires hostAddressLoopback=true for Windows to reach the preview through its LAN IPv4. In $settingsPath, add hostAddressLoopback=true under [experimental], preserving existing settings. Save work in all WSL distributions, run wsl --shutdown in Windows PowerShell (stops all distributions), reopen $Distro, then rerun the same setup.sh install command."
    }
}
function Resolve-EntryAddresses([string]$Name) {
    return @([System.Net.Dns]::GetHostAddresses($Name) | Where-Object AddressFamily -eq InterNetwork | ForEach-Object IPAddressToString | Select-Object -Unique)
}
function Test-Entry {
    $name = "previewmesh-check.$LanIP.sslip.io"
    $addresses = @(Resolve-EntryAddresses -Name $name)
    if ($addresses.Count -ne 1 -or $addresses[0] -ne $LanIP) {
        throw "Windows DNS does not resolve $name to $LanIP. Ask your network administrator about private-IP DNS filtering."
    }
    $errorPath = [IO.Path]::GetTempFileName()
    try {
        # A native stderr stream can throw before Windows PowerShell inspects curl's exit code.
        $status = & curl.exe --noproxy '*' --max-time 10 -sS --stderr $errorPath -o NUL -w '%{http_code}' -H 'Host: previewmesh-ingress-check.invalid' "http://$($LanIP):18080/"
        $exitCode = $LASTEXITCODE
        if ($exitCode -ne 0 -or $status -ne '404') {
            $detail = [IO.File]::ReadAllText($errorPath).Trim()
            $hint = 'Check the network profile, firewall and WSL forwarding.'
            # Keep the primary HTTP failure if this secondary diagnostic probe fails.
            try { $probeMode = Get-WslNetworkMode } catch { $probeMode = 'unknown' }
            if ($probeMode -eq 'mirrored') {
                $hint = 'For mirrored networking, ensure [experimental] hostAddressLoopback=true in %UserProfile%\.wslconfig and restart WSL after saving all WSL work (Windows PowerShell: wsl --shutdown). Also check the allowed subnet and Windows/Hyper-V firewall rules.'
            }
            throw "Cannot reach Traefik at $($LanIP):18080 (expected HTTP 404; actual HTTP $status; curl exit $exitCode). $detail $hint"
        }
    } finally {
        Remove-Item -LiteralPath $errorPath -Force -ErrorAction SilentlyContinue
    }
    Write-Output 'PASS: Windows DNS and LAN entry. Ask a colleague to check from a second LAN machine too.'
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
if ($Mode -eq 'Install') {
    # Check LAN and mirrored prerequisites before UAC; recheck after elevation.
    [void](Get-LanInterface -Address $LanIP)
    Assert-MirroredHostAccess -NetworkMode (Get-WslNetworkMode)
    Invoke-AdministratorSetup -ScriptPath $PSCommandPath
    exit 0
}
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run Windows setup as administrator.' }
$interface = Get-LanInterface -Address $LanIP
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
$networkMode = Get-WslNetworkMode
Assert-MirroredHostAccess -NetworkMode $networkMode
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
if ($Mode -eq 'Apply' -and $networkMode -eq 'nat') {
    $arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $installedScript + '" -Mode Refresh'
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $triggers = @((New-ScheduledTaskTrigger -AtLogOn -User $user), (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)))
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers -Principal $taskPrincipal -Description 'Keep WSL preview LAN forwarding current while this user distro is running.' -Force | Out-Null
} elseif ($Mode -eq 'Apply') {
    # Mirrored networking uses persistent firewall rules and needs no address refresh.
    $existingTask = Get-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction SilentlyContinue
    if ($existingTask) {
        Disable-ScheduledTask -InputObject $existingTask | Out-Null
        Stop-ScheduledTask -InputObject $existingTask
    }
}
if ($Mode -eq 'Apply' -and $previousIP) {
    & netsh.exe interface portproxy delete v4tov4 "listenaddress=$previousIP" listenport=18080 | Out-Null
    @{ distro = $Distro; lan_ip = $LanIP; allowed_subnet = $AllowedSubnet; network_mode = $networkMode } | ConvertTo-Json | Set-Content -Encoding UTF8 $configPath
}
Test-Entry
