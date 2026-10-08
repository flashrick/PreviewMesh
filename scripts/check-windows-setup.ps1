# Offline Windows setup checks. Run selected source AST nodes only with fixtures.
$ErrorActionPreference = 'Stop'
$sourcePath = Join-Path $PSScriptRoot '..\ops\wsl\setup-lan.ps1'
$source = Get-Content -LiteralPath $sourcePath -Raw
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($source, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -gt 0) {
    throw ('setup-lan.ps1 parser errors: ' + (($parseErrors | ForEach-Object Message) -join '; '))
}
foreach ($functionAst in $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
    . ([scriptblock]::Create($functionAst.Extent.Text))
}
$installAst = $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.IfStatementAst] }, $true) |
    Where-Object { $_.Extent.Text -match '\$Mode\s+-eq\s+[''\"]Install' } |
    Select-Object -First 1
if ($null -eq $installAst) { throw 'Could not extract the real Install branch from setup-lan.ps1.' }
$installBranch = [scriptblock]::Create($installAst.Extent.Text)
$installBranchNoExit = [scriptblock]::Create(($installAst.Extent.Text -replace '(?i)\bexit\s+0\b', 'return'))
$scheduleTaskAst = $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.IfStatementAst] }, $true) |
    Where-Object { $_.Extent.Text -match '(?i)\bRegister-ScheduledTask\b' } |
    Where-Object { $_.Extent.Text -match '(?i)\bDisable-ScheduledTask\b|\bStop-ScheduledTask\b' } |
    Sort-Object { $_.Extent.Text.Length } |
    Select-Object -First 1
if ($null -eq $scheduleTaskAst) {
    # Keep the pre-fix fixture useful while the scheduler branch has no legacy-task cleanup yet.
    $scheduleTaskAst = $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.IfStatementAst] }, $true) |
        Where-Object { $_.Extent.Text -match '(?i)\bRegister-ScheduledTask\b' } |
        Sort-Object { $_.Extent.Text.Length } |
        Select-Object -First 1
}
if ($null -eq $scheduleTaskAst) { throw 'Could not extract the isolated scheduled-task branch from setup-lan.ps1.' }
if ($scheduleTaskAst.Extent.Text -match '(?i)New-NetFirewallRule|New-NetFirewallHyperVRule|netsh\.exe') {
    throw 'The scheduled-task test seam unexpectedly includes the firewall/portproxy Apply branch.'
}
$scheduleTaskBranch = [scriptblock]::Create($scheduleTaskAst.Extent.Text)
$testEntryAst = $ast.FindAll({ param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Test-Entry'
    }, $true) | Select-Object -First 1
if ($null -eq $testEntryAst) { throw 'Could not extract Test-Entry from setup-lan.ps1.' }
$testEntrySource = $testEntryAst.Extent.Text
$networkModeAst = $ast.FindAll({ param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-WslNetworkMode'
    }, $true) | Select-Object -First 1
if ($null -eq $networkModeAst) { throw 'Could not extract Get-WslNetworkMode from setup-lan.ps1.' }
$mirroredHostAccessAst = $ast.FindAll({ param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Assert-MirroredHostAccess'
    }, $true) | Select-Object -First 1
if ($null -eq $mirroredHostAccessAst) { throw 'Could not extract Assert-MirroredHostAccess from setup-lan.ps1.' }
$administratorFunctionAst = $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true) |
    Where-Object { $_.Name -eq 'Invoke-AdministratorSetup' } |
    Select-Object -First 1
if ($null -eq $administratorFunctionAst) { throw 'Could not extract Invoke-AdministratorSetup from setup-lan.ps1.' }
$encodingAssignmentAst = $ast.FindAll({ param($node)
        $node -is [System.Management.Automation.Language.AssignmentStatementAst] -and
        $node.Extent.Text -match '\$OutputEncoding\s*='
    }, $true) | Select-Object -First 1
if ($null -eq $encodingAssignmentAst) { throw 'Could not extract the setup-lan.ps1 UTF-8 output assignment.' }
$encodingAssignment = $encodingAssignmentAst.Extent.Text

$script:Failures = @()
$script:StartProcessCalls = 0
$script:StartProcessArgs = @()
$script:MockInterface = $null
$script:MockProfile = $null
$script:MockExitCode = 1
$script:ChildResultMode = 'none'
$script:ResultPath = $null
$Distro = 'Ubuntu-Test'
$LanIP = '192.168.1.20'
$AllowedSubnet = '192.168.1.0/24'
$expectedUnicode = ([char]0x4e2d).ToString() + ([char]0x6587).ToString() +
    ([char]0x7ba1).ToString() + ([char]0x7406).ToString() + ([char]0x5458).ToString() + ' ' + ([char]0x2713).ToString()

function Add-Failure([string]$Message) {
    $script:Failures += $Message
}

function Assert-Condition([bool]$Condition, [string]$Message) {
    if (-not $Condition) { Add-Failure $Message }
}

function Expect-ThrowContains([string]$Name, [scriptblock]$Action, [string]$Pattern) {
    $caught = $null
    try { & $Action } catch { $caught = $_.Exception }
    if ($null -eq $caught) {
        Add-Failure ($Name + ': expected an error containing /' + $Pattern + '/.')
    } elseif ($caught.Message -notmatch $Pattern) {
        Add-Failure ($Name + ': error was ' + $caught.Message)
    }
}

$testEntryInvocation = [scriptblock]::Create($testEntrySource + "`nTest-Entry")
$script:TestEntryLanIP = '192.168.1.20'
$script:TestEntryNetworkMode = 'nat'
$script:TestEntryCurlMode = $null
$script:TestEntryCurlCalls = 0
$script:TestEntryCurlArgs = @()
$script:TestEntryCurlExit = $null
$script:TestEntryCurlStderr = @()

function Resolve-EntryAddresses([string]$Name) {
    return [System.Net.IPAddress]::Parse($script:TestEntryLanIP)
}

function Get-WslNetworkMode {
    if ($script:TestEntryNetworkMode -eq 'probe-failure') { throw 'synthetic wslinfo failure' }
    return $script:TestEntryNetworkMode
}

function curl.exe {
    $script:TestEntryCurlCalls++
    $script:TestEntryCurlArgs = @($args)
    for ($index = 0; $index -lt $script:TestEntryCurlArgs.Count - 1; $index++) {
        if ([string]$script:TestEntryCurlArgs[$index] -eq '--stderr') {
            $stderrPath = [string]$script:TestEntryCurlArgs[$index + 1]
            [System.IO.File]::WriteAllText($stderrPath, 'synthetic curl transport detail')
            $script:TestEntryCurlStderr += $stderrPath
        }
    }
    switch ($script:TestEntryCurlMode) {
        'native-exit-7' {
            $script:TestEntryCurlExit = 7
            '000'
            $global:LASTEXITCODE = 7
        }
        'native-exit-28' {
            $script:TestEntryCurlExit = 28
            '000'
            $global:LASTEXITCODE = 28
        }
        'http-000' {
            $script:TestEntryCurlExit = 0
            '000'
            $global:LASTEXITCODE = 0
        }
        'http-500' {
            $script:TestEntryCurlExit = 0
            '500'
            $global:LASTEXITCODE = 0
        }
        'success' {
            $script:TestEntryCurlExit = 0
            '404'
            $global:LASTEXITCODE = 0
        }
        default { throw ('Unknown Test-Entry curl mode: ' + $script:TestEntryCurlMode) }
    }
}

function Invoke-TestEntryDiagnosticCase([string]$Name, [string]$Mode, [bool]$ExpectSuccess,
                                         [string]$StatusPattern, [string]$ExitPattern,
                                         [string]$NetworkMode = 'nat', [string]$HintPattern = '') {
    $script:TestEntryNetworkMode = $NetworkMode
    $script:TestEntryCurlMode = $Mode
    $script:TestEntryCurlCalls = 0
    $script:TestEntryCurlArgs = @()
    $script:TestEntryCurlExit = $null
    $script:TestEntryCurlStderr = @()
    $caught = $null
    try { & $testEntryInvocation } catch { $caught = $_.Exception }
    $message = if ($null -ne $caught) { [string]$caught.Message } else { '' }
    if ($ExpectSuccess) {
        Assert-Condition ($null -eq $caught) ($Name + ': strict HTTP 404 should pass, got ' + $message)
    } else {
        Assert-Condition ($null -ne $caught) ($Name + ': expected Test-Entry to fail')
        if ($null -ne $caught) {
            Assert-Condition ($message -match $StatusPattern) ($Name + ': diagnostic omitted status/address: ' + $message)
            Assert-Condition ($message -match $ExitPattern) ($Name + ': diagnostic omitted curl exit code: ' + $message)
            Assert-Condition ($message -match [regex]::Escape($script:TestEntryLanIP + ':18080')) ($Name + ': diagnostic omitted target address: ' + $message)
            Assert-Condition ($message -match '(?i)synthetic curl transport detail') ($Name + ': diagnostic omitted curl stderr detail: ' + $message)
            if ($HintPattern) { Assert-Condition ($message -match $HintPattern) ($Name + ': diagnostic omitted expected hint: ' + $message) }
        }
    }
    Assert-Condition ($script:TestEntryCurlCalls -eq 1) ($Name + ': expected one curl invocation')
    Assert-Condition ($script:TestEntryCurlArgs -contains '--stderr') ($Name + ': curl invocation omitted --stderr capture')
    foreach ($stderrPath in $script:TestEntryCurlStderr) {
        $stderrExistedBeforeCleanup = Test-Path -LiteralPath $stderrPath
        Remove-Item -LiteralPath $stderrPath -Force -ErrorAction SilentlyContinue
        Assert-Condition (-not $stderrExistedBeforeCleanup) ($Name + ': curl stderr fixture was left behind by Test-Entry')
    }
}

$networkModeInvocation = [scriptblock]::Create($networkModeAst.Extent.Text + "`nGet-WslNetworkMode")
$script:MockWslMode = 'nat'
$script:MockWslCalls = @()
function Invoke-WSL {
    param([string[]]$Arguments)
    $script:MockWslCalls += ,@($Arguments)
    return $script:MockWslMode
}
function Invoke-NetworkModeCase([string]$Name, [string]$Mode, [bool]$ExpectSuccess) {
    $script:MockWslMode = $Mode
    $script:MockWslCalls = @()
    $caught = $null
    $value = $null
    try { $value = & $networkModeInvocation } catch { $caught = $_.Exception }
    if ($ExpectSuccess) {
        Assert-Condition ($null -eq $caught -and $value -eq $Mode) ($Name + ': unexpected network mode result')
    } else {
        Assert-Condition ($null -ne $caught -and $caught.Message -match '(?i)(unsupported|NAT|mirrored)') ($Name + ': invalid mode was accepted')
    }
    Assert-Condition ($script:MockWslCalls.Count -eq 1) ($Name + ': expected one WSL mode probe')
    if ($script:MockWslCalls.Count -eq 1) {
        Assert-Condition (($script:MockWslCalls[0] -join ' ') -eq 'wslinfo --networking-mode') ($Name + ': wrong WSL mode probe arguments')
    }
}
Invoke-NetworkModeCase 'network-mode-nat' 'nat' $true
Invoke-NetworkModeCase 'network-mode-mirrored' 'mirrored' $true
Invoke-NetworkModeCase 'network-mode-invalid' 'bridge' $false

function Get-NetIPAddress {
    param([string]$AddressFamily, [string]$IPAddress, [string]$ErrorAction)
    return $script:MockInterface
}

function Get-NetConnectionProfile {
    param([int]$InterfaceIndex, [string]$ErrorAction)
    return $script:MockProfile
}

function New-SetupResultPath {
    return $script:ResultPath
}

function Write-MockResult {
    if ($script:ChildResultMode -eq 'none') { return }
    $payload = switch ($script:ChildResultMode) {
        'success' { '{"ok":true,"error":""}' }
        'failure' { '{"ok":false,"error":"synthetic child failure"}' }
        'malformed' { '{not-json' }
        default { throw ('Unknown mock result mode: ' + $script:ChildResultMode) }
    }
    [System.IO.File]::WriteAllText($script:ResultPath, $payload)
}

function Get-EncodedChildCommand {
    $flat = @()
    foreach ($item in $script:StartProcessArgs) {
        if ($item -is [array]) { $flat += $item } else { $flat += [string]$item }
    }
    $argument = $null
    for ($index = 0; $index -lt $flat.Count; $index++) {
        if ([string]$flat[$index] -eq '-ArgumentList' -and $index + 1 -lt $flat.Count) {
            $argument = [string]$flat[$index + 1]
            break
        }
    }
    if ($null -eq $argument) {
        foreach ($item in $flat) {
            if ([string]$item -match '-EncodedCommand\s+') { $argument = [string]$item; break }
        }
    }
    if ($null -eq $argument) { return $null }
    $encoded = ($argument -split '-EncodedCommand\s+', 2)[1].Trim()
    return [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($encoded))
}

function Start-Process {
    $script:StartProcessCalls++
    $script:StartProcessArgs = @($args)
    if ($script:ChildResultMode -like 'fixture-*') {
        $childCommand = Get-EncodedChildCommand
        if ($null -eq $childCommand) { throw 'fixture child did not receive an encoded command' }
        # Execute only the synthetic fixture through a normal child process; omit RunAs deliberately.
        & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand (
            [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($childCommand))) *> $null
        return [pscustomobject]@{ ExitCode = $LASTEXITCODE }
    }
    Write-MockResult
    if ($null -eq $script:MockExitCode) { return $null }
    return [pscustomobject]@{ ExitCode = $script:MockExitCode }
}

function Assert-EncodedHandoff {
    $childCommand = $null
    try {
        $childCommand = Get-EncodedChildCommand
    } catch {
        Add-Failure ('RunAs handoff command was not valid UTF-16 Base64: ' + $_.Exception.Message)
        return
    }
    Assert-Condition ($null -ne $childCommand) 'RunAs handoff did not provide an encoded command.'
    if ($null -eq $childCommand) { return }
    Assert-Condition ($childCommand -match '(?i)-Mode\s+Apply') 'RunAs handoff omitted Apply mode.'
    $quotedPath = Quote-Argument $script:ResultPath
    Assert-Condition ($childCommand -match '(?i)Set-Content\s+-LiteralPath') 'RunAs handoff omitted the literal result path write.'
    Assert-Condition ($childCommand -match [regex]::Escape($quotedPath)) 'RunAs handoff omitted the expected quoted result path.'
    Assert-Condition ($childCommand -match '(?i)try') 'RunAs handoff omitted the child try wrapper.'
    Assert-Condition ($childCommand -match '(?i)catch') 'RunAs handoff omitted the child catch wrapper.'
    Assert-Condition ($childCommand -match '(?i)ConvertTo-Json') 'RunAs handoff omitted the JSON result write.'
}

# Address checks must happen before any administrator handoff. Execute the real
# Install branch, but replace its administrator function with a call counter.
$script:MockInterface = $null
$script:MockProfile = $null
$script:StartProcessCalls = 0
$script:AdministratorCalls = 0
function Invoke-AdministratorSetup { $script:AdministratorCalls++ }
$Mode = 'Install'
Expect-ThrowContains 'missing Windows LAN address' { & $installBranch } '(?i)(setup\.sh init|WSL|Windows.*IPv4)'
Assert-Condition ($script:StartProcessCalls -eq 0) 'Missing Windows LAN address attempted Start-Process.'
Assert-Condition ($script:AdministratorCalls -eq 0) 'Missing Windows LAN address reached administrator setup.'

$script:MockInterface = [pscustomobject]@{ InterfaceIndex = 17; PrefixLength = 24 }
$script:MockProfile = [pscustomobject]@{ NetworkCategory = 'Public' }
$script:StartProcessCalls = 0
$script:AdministratorCalls = 0
Expect-ThrowContains 'public Windows profile' { & $installBranch } '(?i)(Private|Settings|trusted LAN)'
Assert-Condition ($script:StartProcessCalls -eq 0) 'Public Windows profile attempted Start-Process.'
Assert-Condition ($script:AdministratorCalls -eq 0) 'Public Windows profile reached administrator setup.'

# The real Install branch must reject a mirrored host-access prerequisite before
# UAC, then reach the administrator handoff only after the prerequisite passes.
$script:InstallEvents = @()
$script:InstallNetworkMode = 'mirrored'
$script:InstallHostAccessFailure = $false
function Get-LanInterface {
    $script:InstallEvents += 'interface'
    return [pscustomobject]@{ InterfaceIndex = 17; PrefixLength = 24 }
}
function Get-WslNetworkMode {
    $script:InstallEvents += 'mode'
    return $script:InstallNetworkMode
}
function Assert-MirroredHostAccess {
    param([string]$NetworkMode)
    $script:InstallEvents += 'host-access'
    if ($script:InstallHostAccessFailure) {
        throw 'WSL mirrored networking requires hostAddressLoopback=true; run wsl --shutdown before retrying.'
    }
}
function Invoke-AdministratorSetup { $script:InstallEvents += 'administrator'; $script:AdministratorCalls++ }
$script:MockProfile = [pscustomobject]@{ NetworkCategory = 'Private' }
$script:AdministratorCalls = 0
$script:InstallEvents = @()
$script:InstallHostAccessFailure = $true
Expect-ThrowContains 'mirrored host access before UAC' { & $installBranchNoExit } '(?i)(hostAddressLoopback|wsl --shutdown)'
Assert-Condition ($script:AdministratorCalls -eq 0) 'Mirrored host-access failure reached administrator setup.'
Assert-Condition (($script:InstallEvents -join ',') -eq 'interface,mode,host-access') 'Mirrored host-access failure did not stop after the prerequisite.'

$script:AdministratorCalls = 0
$script:InstallEvents = @()
$script:InstallHostAccessFailure = $false
& $installBranchNoExit
Assert-Condition ($script:AdministratorCalls -eq 1) 'Mirrored host-access success did not reach administrator setup.'
Assert-Condition (($script:InstallEvents -join ',') -eq 'interface,mode,host-access,administrator') 'Mirrored install order changed.'

$script:InstallNetworkMode = 'nat'
$script:AdministratorCalls = 0
$script:InstallEvents = @()
& $installBranchNoExit
Assert-Condition ($script:AdministratorCalls -eq 1) 'NAT install did not reach administrator setup.'
Assert-Condition (($script:InstallEvents -join ',') -eq 'interface,mode,host-access,administrator') 'NAT install path changed unexpectedly.'

# Exercise only the scheduler If branch. These command shims keep the test
# offline: no task, firewall rule, port proxy, or interactive window is used.
$script:ScheduleActionCalls = @()
$script:ScheduleTriggerCalls = @()
$script:SchedulePrincipalCalls = @()
$script:ScheduleRegisterCalls = @()
$script:ScheduleTaskLookupCalls = @()
$script:ScheduleOperationCalls = @()
$script:ScheduleExpectedTaskName = 'PreviewMesh LAN forwarding'
$script:ScheduleExistingTask = $null
$script:ScheduleUnrelatedTask = [pscustomobject]@{ TaskName = 'Unrelated scheduled task' }
$script:ScheduleInstalledScript = 'C:\ProgramData\PreviewMesh\setup-lan.ps1'

function Reset-ScheduleMocks([object]$ExistingTask) {
    $script:ScheduleExistingTask = $ExistingTask
    $script:ScheduleActionCalls = @()
    $script:ScheduleTriggerCalls = @()
    $script:SchedulePrincipalCalls = @()
    $script:ScheduleRegisterCalls = @()
    $script:ScheduleTaskLookupCalls = @()
    $script:ScheduleOperationCalls = @()
}

function New-ScheduledTaskAction {
    param([string]$Execute, [string]$Argument)
    $action = [pscustomobject]@{ Execute = $Execute; Argument = $Argument }
    $script:ScheduleActionCalls += $action
    return $action
}

function New-ScheduledTaskTrigger {
    param(
        [switch]$AtLogOn,
        [string]$User,
        [switch]$Once,
        [datetime]$At,
        [timespan]$RepetitionInterval
    )
    $trigger = [pscustomobject]@{
        AtLogOn = [bool]$AtLogOn
        User = $User
        Once = [bool]$Once
        At = $At
        RepetitionInterval = $RepetitionInterval
    }
    $script:ScheduleTriggerCalls += $trigger
    return $trigger
}

function New-ScheduledTaskPrincipal {
    param([string]$UserId, [string]$LogonType, [string]$RunLevel)
    $principal = [pscustomobject]@{ UserId = $UserId; LogonType = $LogonType; RunLevel = $RunLevel }
    $script:SchedulePrincipalCalls += $principal
    return $principal
}

function Get-ScheduledTask {
    param([string]$TaskName, [string]$TaskPath, [string]$ErrorAction)
    $script:ScheduleTaskLookupCalls += [pscustomobject]@{ TaskName = $TaskName; TaskPath = $TaskPath }
    # A missing or broad lookup receives the unrelated fixture, so the test
    # catches implementations that stop an arbitrary scheduled task.
    if ($TaskName -eq $script:ScheduleExpectedTaskName -and
        ($null -eq $TaskPath -or $TaskPath -eq '\')) {
        return $script:ScheduleExistingTask
    }
    return $script:ScheduleUnrelatedTask
}

function Disable-ScheduledTask {
    param([object]$InputObject)
    $script:ScheduleOperationCalls += [pscustomobject]@{ Operation = 'Disable'; InputObject = $InputObject }
}

function Stop-ScheduledTask {
    param([object]$InputObject)
    $script:ScheduleOperationCalls += [pscustomobject]@{ Operation = 'Stop'; InputObject = $InputObject }
}

function Register-ScheduledTask {
    param(
        [string]$TaskName,
        [string]$TaskPath,
        [object]$Action,
        [object[]]$Trigger,
        [object]$Principal,
        [string]$Description,
        [switch]$Force
    )
    $registration = [pscustomobject]@{
        TaskName = $TaskName
        TaskPath = $TaskPath
        Action = $Action
        Trigger = @($Trigger)
        Principal = $Principal
        Description = $Description
        Force = [bool]$Force
    }
    $script:ScheduleRegisterCalls += $registration
    return $registration
}

function Invoke-ScheduleBranchCase([string]$CaseName, [string]$ModeValue, [string]$NetworkModeValue,
                                   [object]$ExistingTask) {
    Reset-ScheduleMocks $ExistingTask
    $Mode = $ModeValue
    $networkMode = $NetworkModeValue
    $taskName = $script:ScheduleExpectedTaskName
    $installedScript = $script:ScheduleInstalledScript
    $caught = $null
    try { & $scheduleTaskBranch } catch { $caught = $_.Exception }
    Assert-Condition ($null -eq $caught) ($CaseName + ': isolated scheduler branch threw ' + $(if ($null -ne $caught) { $caught.Message } else { '' }))
}

$expectedScheduleUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$natTask = [pscustomobject]@{ TaskName = $script:ScheduleExpectedTaskName; Marker = 'nat' }
Invoke-ScheduleBranchCase 'apply-nat' 'Apply' 'nat' $natTask
Assert-Condition ($script:ScheduleRegisterCalls.Count -eq 1) 'Apply NAT did not register exactly one refresh task.'
Assert-Condition ($script:ScheduleTaskLookupCalls.Count -eq 0) 'Apply NAT looked up a legacy task.'
Assert-Condition ($script:ScheduleOperationCalls.Count -eq 0) 'Apply NAT disabled or stopped a task.'
if ($script:ScheduleActionCalls.Count -eq 1) {
    $natAction = $script:ScheduleActionCalls[0]
    $natArguments = [string]$natAction.Argument
    Assert-Condition ($natAction.Execute -eq 'powershell.exe') 'Apply NAT changed the scheduled-task executable.'
    Assert-Condition ($natArguments -match '(?i)(^|\s)-NoProfile(\s|$)') 'Apply NAT refresh action omitted -NoProfile.'
    Assert-Condition ($natArguments -match '(?i)(^|\s)-NonInteractive(\s|$)') 'Apply NAT refresh action omitted -NonInteractive.'
    Assert-Condition ($natArguments -match '(?i)(^|\s)-WindowStyle\s+Hidden(\s|$)') 'Apply NAT refresh action omitted -WindowStyle Hidden.'
    Assert-Condition ($natArguments -match '(?i)(^|\s)-ExecutionPolicy\s+Bypass(\s|$)') 'Apply NAT refresh action omitted -ExecutionPolicy Bypass.'
    Assert-Condition ($natArguments.Contains('-File "' + $script:ScheduleInstalledScript + '" -Mode Refresh')) 'Apply NAT refresh action changed the installed script path or Refresh mode.'
} else {
    Add-Failure 'Apply NAT did not produce an action fixture.'
}
if ($script:SchedulePrincipalCalls.Count -eq 1) {
    $natPrincipal = $script:SchedulePrincipalCalls[0]
    Assert-Condition ($natPrincipal.UserId -eq $expectedScheduleUser) 'Apply NAT changed the scheduled-task user.'
    Assert-Condition ($natPrincipal.LogonType -eq 'Interactive') 'Apply NAT changed the scheduled-task logon type.'
    Assert-Condition ($natPrincipal.RunLevel -eq 'Highest') 'Apply NAT changed the scheduled-task run level.'
} else {
    Add-Failure 'Apply NAT did not produce one task principal fixture.'
}
if ($script:ScheduleRegisterCalls.Count -eq 1) {
    $natRegistration = $script:ScheduleRegisterCalls[0]
    Assert-Condition ($natRegistration.TaskName -eq $script:ScheduleExpectedTaskName) 'Apply NAT changed the task name.'
    Assert-Condition ([string]::IsNullOrEmpty([string]$natRegistration.TaskPath) -or $natRegistration.TaskPath -eq '\') 'Apply NAT changed the task root path.'
    Assert-Condition ([object]::ReferenceEquals($natRegistration.Action, $script:ScheduleActionCalls[0])) 'Apply NAT registered a different action fixture.'
    Assert-Condition (@($natRegistration.Trigger).Count -eq 2) 'Apply NAT changed the logon/refresh trigger count.'
    Assert-Condition ([object]::ReferenceEquals($natRegistration.Principal, $script:SchedulePrincipalCalls[0])) 'Apply NAT registered a different principal fixture.'
    Assert-Condition ($natRegistration.Force) 'Apply NAT omitted task replacement with -Force.'
}
Assert-Condition ($script:ScheduleTriggerCalls.Count -eq 2) 'Apply NAT changed the logon/refresh trigger count.'
if ($script:ScheduleTriggerCalls.Count -ge 1) {
    Assert-Condition ($script:ScheduleTriggerCalls[0].AtLogOn) 'Apply NAT omitted the logon trigger.'
    Assert-Condition ($script:ScheduleTriggerCalls[0].User -eq $expectedScheduleUser) 'Apply NAT changed the logon trigger user.'
}
if ($script:ScheduleTriggerCalls.Count -ge 2) {
    Assert-Condition ($script:ScheduleTriggerCalls[1].Once) 'Apply NAT omitted the one-time refresh trigger.'
    Assert-Condition ($script:ScheduleTriggerCalls[1].RepetitionInterval -eq (New-TimeSpan -Minutes 1)) 'Apply NAT changed the refresh interval.'
}

$oldTask = [pscustomobject]@{ TaskName = $script:ScheduleExpectedTaskName; Marker = 'existing' }
$script:ScheduleUnrelatedTask = [pscustomobject]@{ TaskName = 'Unrelated scheduled task'; Marker = 'must-not-stop' }
Invoke-ScheduleBranchCase 'apply-mirrored-existing-task' 'Apply' 'mirrored' $oldTask
Assert-Condition ($script:ScheduleRegisterCalls.Count -eq 0) 'Apply mirrored registered a refresh task.'
Assert-Condition ($script:ScheduleActionCalls.Count -eq 0 -and $script:ScheduleTriggerCalls.Count -eq 0 -and $script:SchedulePrincipalCalls.Count -eq 0) 'Apply mirrored constructed refresh-task objects.'
Assert-Condition ($script:ScheduleTaskLookupCalls.Count -eq 1) 'Apply mirrored did not perform one legacy-task lookup.'
if ($script:ScheduleTaskLookupCalls.Count -eq 1) {
    Assert-Condition ($script:ScheduleTaskLookupCalls[0].TaskName -eq $script:ScheduleExpectedTaskName) 'Apply mirrored looked up an unrelated task name.'
    Assert-Condition ([string]::IsNullOrEmpty([string]$script:ScheduleTaskLookupCalls[0].TaskPath) -or $script:ScheduleTaskLookupCalls[0].TaskPath -eq '\') 'Apply mirrored looked outside the task root.'
}
Assert-Condition ($script:ScheduleOperationCalls.Count -eq 2) 'Apply mirrored did not perform exactly disable and stop operations.'
if ($script:ScheduleOperationCalls.Count -eq 2) {
    Assert-Condition ($script:ScheduleOperationCalls[0].Operation -eq 'Disable') 'Apply mirrored did not disable the legacy task first.'
    Assert-Condition ($script:ScheduleOperationCalls[1].Operation -eq 'Stop') 'Apply mirrored did not stop the legacy task second.'
    Assert-Condition ([object]::ReferenceEquals($script:ScheduleOperationCalls[0].InputObject, $oldTask)) 'Apply mirrored disabled an unrelated task.'
    Assert-Condition ([object]::ReferenceEquals($script:ScheduleOperationCalls[1].InputObject, $oldTask)) 'Apply mirrored stopped an unrelated task.'
    Assert-Condition (-not ($script:ScheduleOperationCalls | Where-Object { [object]::ReferenceEquals($_.InputObject, $script:ScheduleUnrelatedTask) })) 'Apply mirrored stopped the unrelated task fixture.'
}

$script:ScheduleUnrelatedTask = [pscustomobject]@{ TaskName = 'Unrelated scheduled task'; Marker = 'must-not-stop' }
Invoke-ScheduleBranchCase 'apply-mirrored-no-existing-task' 'Apply' 'mirrored' $null
Assert-Condition ($script:ScheduleRegisterCalls.Count -eq 0) 'Apply mirrored without a legacy task registered a refresh task.'
Assert-Condition ($script:ScheduleOperationCalls.Count -eq 0) 'Apply mirrored without a legacy task performed task operations.'
Assert-Condition ($script:ScheduleTaskLookupCalls.Count -eq 1) 'Apply mirrored without a legacy task did not perform one lookup.'

foreach ($nonApplyMode in @('Refresh', 'Check')) {
    Invoke-ScheduleBranchCase ('mode-' + $nonApplyMode + '-does-not-touch-task') $nonApplyMode 'mirrored' $oldTask
    Assert-Condition ($script:ScheduleRegisterCalls.Count -eq 0) ($nonApplyMode + ' registered a refresh task.')
    Assert-Condition ($script:ScheduleTaskLookupCalls.Count -eq 0) ($nonApplyMode + ' looked up a scheduled task.')
    Assert-Condition ($script:ScheduleOperationCalls.Count -eq 0) ($nonApplyMode + ' disabled or stopped a scheduled task.')
}

# Restore the real handoff function for result protocol tests.
. ([scriptblock]::Create($administratorFunctionAst.Extent.Text))

# Keep all result files in a portable, GUID-specific directory below this test.
$resultRoot = Join-Path $PSScriptRoot ('windows-setup-check-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $resultRoot -Force | Out-Null
$script:MockInterface = [pscustomobject]@{ InterfaceIndex = 17; PrefixLength = 24 }
$script:MockProfile = [pscustomobject]@{ NetworkCategory = 'Private' }
$mirroredAccessInvocation = [scriptblock]::Create($mirroredHostAccessAst.Extent.Text + "`nAssert-MirroredHostAccess -NetworkMode `$script:AssertNetworkMode")
$hostFixtureRoot = Join-Path $resultRoot 'wsl-config-fixtures'
New-Item -ItemType Directory -Path $hostFixtureRoot -Force | Out-Null
$previousUserProfile = $env:USERPROFILE

function Invoke-MirroredHostAccessCase([string]$Name, [string]$NetworkMode, [string]$Content, [bool]$ExpectSuccess) {
    $caseRoot = Join-Path $hostFixtureRoot $Name
    New-Item -ItemType Directory -Path $caseRoot -Force | Out-Null
    $configPath = Join-Path $caseRoot '.wslconfig'
    if ($null -ne $Content) { [IO.File]::WriteAllText($configPath, $Content) }
    $env:USERPROFILE = $caseRoot
    $script:AssertNetworkMode = $NetworkMode
    $caught = $null
    try { & $mirroredAccessInvocation } catch { $caught = $_.Exception }
    if ($ExpectSuccess) {
        Assert-Condition ($null -eq $caught) ($Name + ': unexpected host-access rejection: ' + $(if ($null -ne $caught) { $caught.Message } else { '' }))
    } else {
        Assert-Condition ($null -ne $caught) ($Name + ': expected host-access rejection')
        if ($null -ne $caught) {
            Assert-Condition ($caught.Message -match '(?i)hostAddressLoopback') ($Name + ': rejection omitted hostAddressLoopback guidance')
            Assert-Condition ($caught.Message -match '(?i)wsl\s+--shutdown') ($Name + ': rejection omitted wsl --shutdown guidance')
        }
    }
}

# Restore the Test-Entry probe mock after the Install-order fixture overrides it.
function Get-WslNetworkMode {
    if ($script:TestEntryNetworkMode -eq 'probe-failure') { throw 'synthetic wslinfo failure' }
    return $script:TestEntryNetworkMode
}

# Test-Entry diagnostics must identify transport failure, HTTP status, target,
# and captured native curl detail while retaining strict 404 success.
Invoke-TestEntryDiagnosticCase 'curl-exit-7' 'native-exit-7' $false '(?i)(?:HTTP|status)[^0-9]*000|000.*(?:HTTP|status)' '(?i)(?:curl|exit)[^0-9]*7|7.*(?:curl|exit)'
Invoke-TestEntryDiagnosticCase 'curl-timeout-28' 'native-exit-28' $false '(?i)(?:HTTP|status)[^0-9]*000|000.*(?:HTTP|status)' '(?i)(?:curl|exit)[^0-9]*28|28.*(?:curl|exit)'
Invoke-TestEntryDiagnosticCase 'http-000-exit-0' 'http-000' $false '(?i)(?:HTTP|status)[^0-9]*000|000.*(?:HTTP|status)' '(?i)(?:curl|exit)[^0-9]*0|0.*(?:curl|exit)'
Invoke-TestEntryDiagnosticCase 'http-500-exit-0' 'http-500' $false '(?i)(?:HTTP|status)[^0-9]*500|500.*(?:HTTP|status)' '(?i)(?:curl|exit)[^0-9]*0|0.*(?:curl|exit)'
Invoke-TestEntryDiagnosticCase 'http-500-mirrored-hint' 'http-500' $false '(?i)(?:HTTP|status)[^0-9]*500|500.*(?:HTTP|status)' '(?i)(?:curl|exit)[^0-9]*0|0.*(?:curl|exit)' 'mirrored' '(?i)hostAddressLoopback.*wsl\s+--shutdown'
Invoke-TestEntryDiagnosticCase 'http-500-probe-failure' 'http-500' $false '(?i)(?:HTTP|status)[^0-9]*500|500.*(?:HTTP|status)' '(?i)(?:curl|exit)[^0-9]*0|0.*(?:curl|exit)' 'probe-failure'
Invoke-TestEntryDiagnosticCase 'http-404-success' 'success' $true '' ''

function Invoke-AdministratorCase([string]$Name, [string]$Mode, [object]$ExitCode,
                                   [string]$Pattern, [bool]$ExpectSuccess, [string]$ChildPath = $null) {
    $script:ResultPath = Join-Path $resultRoot ('previewmesh-windows-setup-' + $Name + '.json')
    Remove-Item -LiteralPath $script:ResultPath -Force -ErrorAction SilentlyContinue
    $script:ChildResultMode = $Mode
    $script:MockExitCode = $ExitCode
    $script:StartProcessCalls = 0
    $script:StartProcessArgs = @()
    $caught = $null
    $scriptPath = if ($ChildPath) { $ChildPath } else { $sourcePath }
    try { Invoke-AdministratorSetup -ScriptPath $scriptPath } catch { $caught = $_.Exception }
    $script:LastAdministratorError = if ($null -ne $caught) { [string]$caught.Message } else { $null }
    if ($ExpectSuccess) {
        if ($null -ne $caught) { Add-Failure ($Name + ': unexpected error ' + $caught.Message) }
    } elseif ($null -eq $caught) {
        Add-Failure ($Name + ': expected an administrator setup error.')
    } elseif ($caught.Message -notmatch $Pattern) {
        Add-Failure ($Name + ': error was ' + $caught.Message)
    }
    Assert-Condition ($script:StartProcessCalls -eq 1) ($Name + ': expected one mocked RunAs launch.')
    if ($Name -eq 'child-failure') { Assert-EncodedHandoff }
    Assert-Condition (-not (Test-Path -LiteralPath $script:ResultPath)) ($Name + ': result file was not cleaned up.')
}

try {
    Invoke-MirroredHostAccessCase 'nat-does-not-read-config' 'nat' $null $true
    Invoke-MirroredHostAccessCase 'mirrored-missing-setting' 'mirrored' $null $false
    Invoke-MirroredHostAccessCase 'mirrored-wrong-section' 'mirrored' "[wsl2]`nHOSTADDRESSLOOPBACK=true`n" $false
    Invoke-MirroredHostAccessCase 'mirrored-false' 'mirrored' "[EXPERIMENTAL]`nHOSTADDRESSLOOPBACK=false`n" $false
    Invoke-MirroredHostAccessCase 'mirrored-last-value-wins' 'mirrored' "[experimental]`nHOSTADDRESSLOOPBACK=true`nHOSTADDRESSLOOPBACK=false ; final value`n" $false
    Invoke-MirroredHostAccessCase 'mirrored-true-with-comments' 'mirrored' "`n# ignored comment`n[EXPERIMENTAL] ; section comment`n  ; another comment`nHOSTADDRESSLOOPBACK = TRUE # inline comment`n" $true

    Invoke-AdministratorCase 'child-failure' 'failure' 7 'synthetic child failure' $false
    Invoke-AdministratorCase 'ok-false-exit-zero' 'failure' 0 'synthetic child failure' $false
    Invoke-AdministratorCase 'malformed-result' 'malformed' 0 '(?i)(invalid|JSON|result)' $false
    Invoke-AdministratorCase 'cancelled-no-result' 'none' 1223 '(?i)(1223|exit|result|administrator)' $false
    Invoke-AdministratorCase 'success-null-process' 'success' $null '' $true
    Invoke-AdministratorCase 'success-nonzero-exit' 'success' 7 '(?i)(success|code|exit)' $false

    $failureFixture = Join-Path $resultRoot 'synthetic-child-failure.ps1'
    $successFixture = Join-Path $resultRoot 'synthetic-child-success.ps1'
    $failureContent = @(
        '[CmdletBinding()]'
        'param([string]$Mode, [string]$Distro, [string]$LanIP, [string]$AllowedSubnet)'
        '$ErrorActionPreference = ''Stop'''
        $encodingAssignment
        '$u=[char]0x4e2d+[char]0x6587+[char]0x7ba1+[char]0x7406+[char]0x5458+'' ''+[char]0x2713'
        'throw (''synthetic child failure: '' + $u)'
    ) -join "`r`n"
    $successContent = @(
        '[CmdletBinding()]'
        'param([string]$Mode, [string]$Distro, [string]$LanIP, [string]$AllowedSubnet)'
        '$ErrorActionPreference = ''Stop'''
        $encodingAssignment
        'exit 0'
    ) -join "`r`n"
    Set-Content -LiteralPath $failureFixture -Encoding UTF8 -Value $failureContent
    Set-Content -LiteralPath $successFixture -Encoding UTF8 -Value $successContent
    Invoke-AdministratorCase 'fixture-child-failure' 'fixture-failure' 0 ('synthetic child failure.*' + [regex]::Escape($expectedUnicode)) $false $failureFixture
    Assert-Condition ($null -ne $script:LastAdministratorError -and $script:LastAdministratorError.Contains($expectedUnicode)) 'Synthetic child Unicode error was not preserved.'
    Assert-Condition ($null -eq $script:LastAdministratorError -or $script:LastAdministratorError.IndexOf([char]0xfffd) -lt 0) 'Synthetic child Unicode error was replaced.'
    Invoke-AdministratorCase 'fixture-child-success' 'fixture-success' 0 '' $true $successFixture
} finally {
    if ($null -eq $previousUserProfile) {
        Remove-Item Env:USERPROFILE -ErrorAction SilentlyContinue
    } else {
        $env:USERPROFILE = $previousUserProfile
    }
    Remove-Item -LiteralPath $resultRoot -Recurse -Force -ErrorAction SilentlyContinue
}

if ($script:Failures.Count -gt 0) {
    Write-Output ('RED: ' + $script:Failures.Count + ' Windows setup mock checks failed')
    $script:Failures | ForEach-Object { Write-Output (' - ' + $_) }
    exit 1
}
Write-Output 'Windows setup mock checks passed.'
exit 0
