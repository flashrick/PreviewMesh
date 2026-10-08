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

# Restore the real handoff function for result protocol tests.
. ([scriptblock]::Create($administratorFunctionAst.Extent.Text))

# Keep all result files in a portable, GUID-specific directory below this test.
$resultRoot = Join-Path $PSScriptRoot ('windows-setup-check-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $resultRoot -Force | Out-Null
$script:MockInterface = [pscustomobject]@{ InterfaceIndex = 17; PrefixLength = 24 }
$script:MockProfile = [pscustomobject]@{ NetworkCategory = 'Private' }

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
    Remove-Item -LiteralPath $resultRoot -Recurse -Force -ErrorAction SilentlyContinue
}

if ($script:Failures.Count -gt 0) {
    Write-Output ('RED: ' + $script:Failures.Count + ' Windows setup mock checks failed')
    $script:Failures | ForEach-Object { Write-Output (' - ' + $_) }
    exit 1
}
Write-Output 'Windows setup mock checks passed.'
exit 0
