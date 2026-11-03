[CmdletBinding()]
param([switch]$SmokeTest, [switch]$BackendSmokeTest, [switch]$Terminal, [switch]$Plain, [switch]$NoAnimation, [switch]$Logo)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Get-AutomaticHost([string[]]$Usable) {
    if ($Usable.Count -eq 1) { return $Usable[0] }
    if ($Usable -contains "hermes") { return "hermes" }
    if ($Usable.Count -gt 0) { return $Usable[0] }
    return $null
}

function Test-SafePackagePath([string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value.StartsWith("/") -or $Value.Contains("\") -or $Value.Contains([char]0)) { return $false }
    foreach ($part in $Value.Split('/')) {
        if ([string]::IsNullOrEmpty($part) -or $part -eq "." -or $part -eq "..") { return $false }
    }
    return $true
}

function Assert-ExactProperties($Value, [string[]]$Names, [string]$Message) {
    $actual = @($Value.PSObject.Properties.Name | Sort-Object)
    $expected = @($Names | Sort-Object)
    if (($actual -join "`n") -ne ($expected -join "`n")) { throw $Message }
}

function Test-CompanionPackage([string]$Root) {
    $rootItem = Get-Item -LiteralPath $Root -Force
    if (-not $rootItem.PSIsContainer -or ($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "The companion package root is unsafe."
    }
    $manifestPath = Join-Path $Root "companion-manifest.json"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw "The companion package manifest is missing." }
    $manifestItem = Get-Item -LiteralPath $manifestPath -Force
    if ($manifestItem.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "The companion package manifest is unsafe." }
    try { $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { throw "The companion package manifest is invalid." }
    Assert-ExactProperties $manifest @("schema", "files") "The companion package manifest shape is invalid."
    if ($manifest.schema -ne "daemon.companion-package.v1") { throw "The companion package manifest is unsupported." }
    $descriptors = @($manifest.files.PSObject.Properties)
    if ($descriptors.Count -eq 0 -or $descriptors.Count -gt 5000) { throw "The companion package manifest is empty or too large." }
    $expected = @{}
    foreach ($entry in $descriptors) {
        $relative = [string]$entry.Name
        if (-not (Test-SafePackagePath $relative)) { throw "The companion package manifest contains an unsafe path." }
        Assert-ExactProperties $entry.Value @("sha256", "size", "executable") "The companion package manifest contains an invalid entry."
        $digest = [string]$entry.Value.sha256
        $size = [long]$entry.Value.size
        if ($digest -cnotmatch "^[0-9a-f]{64}$" -or $size -lt 0 -or $size -gt 134217728 -or $entry.Value.executable -isnot [bool]) {
            throw "The companion package manifest contains an invalid entry."
        }
        if ($expected.ContainsKey($relative)) { throw "The companion package manifest contains a duplicate path." }
        $expected[$relative] = $entry.Value
    }

    $actual = @{}
    foreach ($item in Get-ChildItem -LiteralPath $Root -Recurse -Force) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "The companion package contains a symbolic link." }
        if ($item.PSIsContainer) { continue }
        $relative = $item.FullName.Substring($rootItem.FullName.TrimEnd('\', '/').Length).TrimStart('\', '/').Replace('\', '/')
        if ($relative -eq "companion-manifest.json") { continue }
        if (-not (Test-SafePackagePath $relative) -or $actual.ContainsKey($relative)) { throw "The companion package contains a duplicate or unsafe path." }
        $actual[$relative] = $item
    }
    if ($actual.Count -ne $expected.Count) { throw "Companion package integrity verification failed." }
    foreach ($relative in $expected.Keys) {
        if (-not $actual.ContainsKey($relative)) { throw "Companion package integrity verification failed." }
        $item = $actual[$relative]
        $descriptor = $expected[$relative]
        $executable = [IO.Path]::GetExtension($item.Name).ToLowerInvariant() -eq ".exe"
        $hash = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($item.Length -ne [long]$descriptor.size -or $hash -cne [string]$descriptor.sha256 -or $executable -ne [bool]$descriptor.executable) {
            throw "Companion package integrity verification failed."
        }
    }
    return $manifest
}

function Invoke-BoundedProcess([string]$Executable, [string[]]$Arguments, [int]$TimeoutMilliseconds, [hashtable]$EnvironmentOverrides = @{}) {
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $Executable
    $start.Arguments = (($Arguments | ForEach-Object { '"' + $_.Replace('"', '\"') + '"' }) -join " ")
    $start.UseShellExecute = $false
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.CreateNoWindow = $true
    foreach ($key in @($start.EnvironmentVariables.Keys)) {
        if ($key.StartsWith("UV_") -or $key.StartsWith("PYTHON") -or $key -eq "VIRTUAL_ENV" -or $key -eq "CONDA_PREFIX") {
            [void]$start.EnvironmentVariables.Remove($key)
        }
    }
    foreach ($key in $EnvironmentOverrides.Keys) { $start.EnvironmentVariables[$key] = [string]$EnvironmentOverrides[$key] }
    $process = [Diagnostics.Process]::new(); $process.StartInfo = $start
    if (-not $process.Start()) { throw "A private runtime operation could not start." }
    if (-not $process.WaitForExit($TimeoutMilliseconds)) { $process.Kill(); throw "A private runtime operation timed out." }
    return [pscustomobject]@{ ExitCode = $process.ExitCode; Stdout = $process.StandardOutput.ReadToEnd(); Stderr = $process.StandardError.ReadToEnd() }
}

function Find-PrivatePython([string]$InstallRoot) {
    if (-not (Test-Path -LiteralPath $InstallRoot -PathType Container)) { return $null }
    foreach ($candidate in Get-ChildItem -LiteralPath $InstallRoot -Filter python.exe -File -Recurse -Force | Sort-Object FullName) {
        if ($candidate.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
        $result = Invoke-BoundedProcess $candidate.FullName @("--version") 12000
        $version = ($result.Stdout + $result.Stderr).Trim()
        if ($result.ExitCode -eq 0 -and $version.StartsWith("Python 3.12.")) { return $candidate.FullName }
    }
    return $null
}

function Get-PrivatePython([string]$Uv) {
    $stateRoot = $env:DAEMON_HARNESS_COMPANION_STATE_ROOT
    if ([string]::IsNullOrWhiteSpace($stateRoot)) {
        $stateRoot = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "DaemonHarness\Companion\bootstrap"
    }
    $installRoot = Join-Path $stateRoot "python"
    $cacheRoot = Join-Path $stateRoot "cache"
    New-Item -ItemType Directory -Path $installRoot, $cacheRoot -Force | Out-Null
    $uvEnvironment = @{ UV_CACHE_DIR = $cacheRoot; UV_NO_CONFIG = "1" }
    $version = Invoke-BoundedProcess $Uv @("--version") 12000 $uvEnvironment
    if ($version.ExitCode -ne 0 -or -not $version.Stdout.Trim().StartsWith("uv 0.12.16")) { throw "The verified companion requires uv 0.12.16." }
    $python = Find-PrivatePython $installRoot
    if (-not [string]::IsNullOrWhiteSpace($python)) { return $python }
    $arguments = @("python", "install", "3.12", "--install-dir", $installRoot, "--managed-python", "--no-bin", "--no-registry", "--no-config", "--no-progress")
    $result = Invoke-BoundedProcess $Uv $arguments 180000 $uvEnvironment
    $python = Find-PrivatePython $installRoot
    if ($result.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($python)) {
        $detail = ($result.Stderr + $result.Stdout).Trim()
        if ([string]::IsNullOrWhiteSpace($detail)) { $detail = "Private Python 3.12 could not be installed." }
        throw $detail
    }
    return $python
}

$script:BackendRuntime = $null
function Resolve-BackendRuntime {
    if ($null -ne $script:BackendRuntime) { return $script:BackendRuntime }
    $manifest = Join-Path $PSScriptRoot "companion-manifest.json"
    $uv = Join-Path $PSScriptRoot "bin\uv.exe"
    $payload = Join-Path $PSScriptRoot "payload"
    $packageDetected = (Test-Path -LiteralPath $manifest) -or (Test-Path -LiteralPath $uv) -or (Test-Path -LiteralPath $payload)
    if ($packageDetected) {
        $package = Test-CompanionPackage $PSScriptRoot
        $uvDescriptor = $package.files.PSObject.Properties["bin/uv.exe"].Value
        if ($null -eq $uvDescriptor) { throw "The companion package does not contain verified uv metadata." }
        $installHome = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "DaemonHarness"
        $preparation = @{
            DAEMON_HARNESS_COMPANION_PACKAGE_ROOT = $PSScriptRoot
            DAEMON_HARNESS_COMPANION_INSTALL_HOME = $installHome
            DAEMON_HARNESS_COMPANION_BUNDLE_ROOT = $payload
            DAEMON_HARNESS_COMPANION_UV_EXECUTABLE = $uv
            DAEMON_HARNESS_COMPANION_UV_SHA256 = [string]$uvDescriptor.sha256
            DAEMON_HARNESS_COMPANION_TARGET_OS = "windows"
            DAEMON_HARNESS_COMPANION_OPENCLAW_API_PORT = "8000"
            DAEMON_HARNESS_COMPANION_OPENCLAW_GATEWAY_PORT = "18789"
        }
        $script:BackendRuntime = [pscustomobject]@{ Python = Get-PrivatePython $uv; SourceRoot = Join-Path $payload "src"; PreparationEnvironment = $preparation }
        return $script:BackendRuntime
    }
    if ([string]::IsNullOrWhiteSpace($env:DAEMON_HARNESS_COMPANION_PYTHON) -or [string]::IsNullOrWhiteSpace($env:DAEMON_HARNESS_COMPANION_ROOT)) {
        throw "No verified companion package or explicit development runtime was found."
    }
    $script:BackendRuntime = [pscustomobject]@{ Python = $env:DAEMON_HARNESS_COMPANION_PYTHON; SourceRoot = Join-Path $env:DAEMON_HARNESS_COMPANION_ROOT "src"; PreparationEnvironment = @{} }
    return $script:BackendRuntime
}

if ($SmokeTest) {
    if ((Get-AutomaticHost @("hermes")) -ne "hermes" -or
        (Get-AutomaticHost @("openclaw")) -ne "openclaw" -or
        (Get-AutomaticHost @("hermes", "openclaw")) -ne "hermes" -or
        $null -ne (Get-AutomaticHost @())) {
        throw "COMPANION_WINDOWS_SMOKE_FAILED"
    }
    Write-Output "COMPANION_WINDOWS_SMOKE_OK host_detection=automatic consent=required install=locked"
    exit 0
}

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw "Daemon Harness Companion requires Windows."
}

if ($Terminal) {
    Write-Host "Preparing DAEMONS. A verified package may download its private Python on first launch."
    $runtime = Resolve-BackendRuntime
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $runtime.Python
    $terminalArgs = @("-m", "daemons_companion.desktop.terminal")
    if ($Plain) { $terminalArgs += "--plain" }
    if ($NoAnimation) { $terminalArgs += "--no-animation" }
    if ($Logo) { $terminalArgs += "--logo" }
    $start.Arguments = $terminalArgs -join " "
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $false
    [void]$start.EnvironmentVariables.Remove("PYTHONHOME")
    $start.EnvironmentVariables["PYTHONUTF8"] = "1"
    $start.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"] = "1"
    $start.EnvironmentVariables["PYTHONPATH"] = $runtime.SourceRoot
    foreach ($key in $runtime.PreparationEnvironment.Keys) { $start.EnvironmentVariables[$key] = [string]$runtime.PreparationEnvironment[$key] }
    $process = [Diagnostics.Process]::new(); $process.StartInfo = $start
    if (-not $process.Start()) { throw "DAEMONS could not start." }
    $process.WaitForExit()
    exit $process.ExitCode
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

function Invoke-CompanionBackend([hashtable]$Request) {
    $runtime = Resolve-BackendRuntime
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $runtime.Python
    $start.Arguments = "-m daemons_companion.desktop.app request"
    $start.UseShellExecute = $false
    $start.RedirectStandardInput = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.CreateNoWindow = $true
    $start.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"] = "1"
    $start.EnvironmentVariables["PYTHONPATH"] = $runtime.SourceRoot
    foreach ($key in $runtime.PreparationEnvironment.Keys) { $start.EnvironmentVariables[$key] = [string]$runtime.PreparationEnvironment[$key] }
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $start
    if (-not $process.Start()) { throw "The local companion engine could not start." }
    $json = $Request | ConvertTo-Json -Depth 8 -Compress
    $process.StandardInput.Write($json)
    $process.StandardInput.Close()
    if (-not $process.WaitForExit(12000)) {
        $process.Kill()
        throw "The local companion engine timed out."
    }
    $stdout = $process.StandardOutput.ReadToEnd()
    $stderr = $process.StandardError.ReadToEnd()
    try { $response = $stdout | ConvertFrom-Json }
    catch { throw "The local companion engine returned an invalid response. $stderr" }
    if ($response.schema -ne "daemon.companion-response.v1") {
        throw "The local companion engine returned an unsupported response."
    }
    if (-not $response.ok) { throw [string]$response.error.message }
    if ($process.ExitCode -ne 0) { throw "The local companion engine failed." }
    return $response
}

if ($BackendSmokeTest) {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw "Daemon Harness Companion requires Windows." }
    $response = Invoke-CompanionBackend @{schema = "daemon.companion-request.v1"; action = "inspect"}
    if ($null -eq $response.inspection) { throw "COMPANION_WINDOWS_PACKAGED_BACKEND_FAILED" }
    foreach ($selected in @($response.inspection.usable_hosts)) {
        $codexAvailable = [bool]$response.inspection.codex_cli_available
        $providerKind = if ($codexAvailable) { "codex_cli" } else { "openai_api" }
        $plan = Invoke-CompanionBackend @{
            schema = "daemon.companion-request.v1"; action = "prepare"; host = $selected
            daemon_name = "Avery"; daemon_gender = "feminine"
            provider = @{
                kind = $providerKind
                model = "gpt-5.6-terra"; base_url = ""; credential_available = -not $codexAvailable
            }
        }
        $expectedSteps = if ($selected -eq "hermes") { 7 } else { 8 }
        if ($null -eq $plan.reviewed_plan -or @($plan.plan.steps).Count -ne $expectedSteps -or [string]::IsNullOrWhiteSpace([string]$plan.confirmation_digest)) {
            throw "COMPANION_WINDOWS_PACKAGED_BACKEND_FAILED"
        }
    }
    Write-Output "COMPANION_WINDOWS_PACKAGED_BACKEND_OK"
    exit 0
}

function New-Label([string]$Text, [int]$X, [int]$Y, [int]$Width, [int]$Height, [float]$Size = 9, [switch]$Bold) {
    $label = [Windows.Forms.Label]::new()
    $label.Text = $Text
    $label.SetBounds($X, $Y, $Width, $Height)
    $style = if ($Bold) { [Drawing.FontStyle]::Bold } else { [Drawing.FontStyle]::Regular }
    $label.Font = [Drawing.Font]::new("Segoe UI", $Size, $style)
    return $label
}

$form = [Windows.Forms.Form]::new()
$form.Text = "Daemon Harness Companion"
$form.ClientSize = [Drawing.Size]::new(780, 690)
$form.StartPosition = "CenterScreen"
$form.AutoScaleMode = [Windows.Forms.AutoScaleMode]::Dpi
$form.FormBorderStyle = [Windows.Forms.FormBorderStyle]::FixedDialog
$form.MaximizeBox = $false

$form.Controls.Add((New-Label "Set up Daemon Harness" 24 18 720 40 20 -Bold))
$form.Controls.Add((New-Label "Use an existing host or review a qualified version to install. The host remains in charge of channels, tools and permissions." 26 60 720 45 10))

$hostChoice = [Windows.Forms.ComboBox]::new(); $hostChoice.SetBounds(185, 118, 315, 28); $hostChoice.DropDownStyle = "DropDownList"; $hostChoice.Enabled = $false
$nameField = [Windows.Forms.TextBox]::new(); $nameField.SetBounds(185, 160, 315, 28)
$genderChoice = [Windows.Forms.ComboBox]::new(); $genderChoice.SetBounds(185, 202, 315, 28); $genderChoice.DropDownStyle = "DropDownList"; [void]$genderChoice.Items.AddRange(@("Feminine", "Masculine")); $genderChoice.SelectedIndex = 0
$providerChoice = [Windows.Forms.ComboBox]::new(); $providerChoice.SetBounds(185, 244, 315, 28); $providerChoice.DropDownStyle = "DropDownList"; [void]$providerChoice.Items.AddRange(@("ChatGPT with Codex CLI (recommended)", "OpenAI API key", "OpenAI-compatible API")); $providerChoice.SelectedIndex = 0
$modelField = [Windows.Forms.TextBox]::new(); $modelField.SetBounds(185, 286, 315, 28); $modelField.Text = "gpt-5.6-terra"
$baseURLField = [Windows.Forms.TextBox]::new(); $baseURLField.SetBounds(185, 328, 315, 28); $baseURLField.Enabled = $false
$credentialCheck = [Windows.Forms.CheckBox]::new(); $credentialCheck.SetBounds(185, 365, 540, 28); $credentialCheck.Text = "I have the required API key; Windows will protect it during installation."; $credentialCheck.Visible = $false

@(
    (New-Label "Host" 26 121 145 24 10 -Bold),
    (New-Label "Daemon name" 26 163 145 24 10 -Bold),
    (New-Label "Grammar" 26 205 145 24 10 -Bold),
    (New-Label "Model access" 26 247 145 24 10 -Bold),
    (New-Label "Model" 26 289 145 24 10 -Bold),
    (New-Label "Provider URL" 26 331 145 24 10 -Bold),
    $hostChoice, $nameField, $genderChoice, $providerChoice, $modelField,
    $baseURLField, $credentialCheck
) | ForEach-Object { $form.Controls.Add($_) }

$reviewButton = [Windows.Forms.Button]::new(); $reviewButton.Text = "Review exact changes"; $reviewButton.SetBounds(535, 326, 190, 32); $reviewButton.Enabled = $false; $form.Controls.Add($reviewButton)
$hostSetupButton = [Windows.Forms.Button]::new(); $hostSetupButton.Text = "Review host setup"; $hostSetupButton.SetBounds(535, 116, 190, 32); $hostSetupButton.Enabled = $false; $form.Controls.Add($hostSetupButton)
$planView = [Windows.Forms.TextBox]::new(); $planView.SetBounds(26, 408, 728, 165); $planView.Multiline = $true; $planView.ReadOnly = $true; $planView.ScrollBars = "Vertical"; $planView.Font = [Drawing.Font]::new("Segoe UI", 9); $form.Controls.Add($planView)
$consentCheck = [Windows.Forms.CheckBox]::new(); $consentCheck.SetBounds(26, 585, 470, 28); $consentCheck.Text = "I have reviewed and accept these exact changes."; $consentCheck.Enabled = $false; $form.Controls.Add($consentCheck)
$installButton = [Windows.Forms.Button]::new(); $installButton.Text = "Install"; $installButton.SetBounds(644, 581, 110, 34); $installButton.Enabled = $false; $form.Controls.Add($installButton)
$detailsCheck = [Windows.Forms.CheckBox]::new(); $detailsCheck.SetBounds(500, 585, 136, 28); $detailsCheck.Text = "Technical details"; $detailsCheck.Enabled = $false; $form.Controls.Add($detailsCheck)
$status = New-Label "Inspecting this PC..." 26 632 720 32 9; $status.ForeColor = [Drawing.Color]::DimGray; $form.Controls.Add($status)

$providerChoice.Add_SelectedIndexChanged({
    $api = $providerChoice.SelectedIndex -ne 0
    $credentialCheck.Visible = $api
    $baseURLField.Enabled = $providerChoice.SelectedIndex -eq 2
})

$script:UsableHosts = @()
$script:ReviewSummary = ""
$script:ReviewDetails = ""
function Reset-Review {
    $script:ReviewSummary = ""; $script:ReviewDetails = ""
    $detailsCheck.Checked = $false; $detailsCheck.Enabled = $false
    $consentCheck.Checked = $false; $consentCheck.Enabled = $false
    $installButton.Enabled = $false; $planView.Clear()
    $planView.Font = [Drawing.Font]::new("Segoe UI", 9)
}
$detailsCheck.Add_CheckedChanged({
    if ($detailsCheck.Checked) {
        $planView.Text = $script:ReviewDetails
        $planView.Font = [Drawing.Font]::new("Consolas", 9)
    } else {
        $planView.Text = $script:ReviewSummary
        $planView.Font = [Drawing.Font]::new("Segoe UI", 9)
    }
})
$hostChoice.Add_SelectedIndexChanged({
    Reset-Review
    $reviewButton.Enabled = ($null -ne $hostChoice.SelectedItem) -and ($script:UsableHosts -contains [string]$hostChoice.SelectedItem.code)
    $status.Text = "Review host setup to see compatibility and installation requirements."
})

$hostSetupButton.Add_Click({
    try {
        Reset-Review
        $response = Invoke-CompanionBackend @{
            schema = "daemon.companion-request.v1"; action = "host_setup"
            host = [string]$hostChoice.SelectedItem.code
        }
        $planView.Text = ([string]$response.host_setup.summary) -replace "\r?\n", "`r`n"
        $status.Text = "Host setup reviewed. No download or installation has started."
    } catch { [Windows.Forms.MessageBox]::Show($_.Exception.Message, "Daemon Harness Companion", "OK", "Warning") | Out-Null }
})

$reviewButton.Add_Click({
    try {
        Reset-Review
        if ($nameField.Text.Trim().Length -eq 0) { throw "Choose a daemon name." }
        $code = [string]$hostChoice.SelectedItem.code
        $kind = @("codex_cli", "openai_api", "openai_compatible_api")[$providerChoice.SelectedIndex]
        $request = @{
            schema = "daemon.companion-request.v1"; action = "prepare"; host = $code
            daemon_name = $nameField.Text
            daemon_gender = if ($genderChoice.SelectedIndex -eq 0) { "feminine" } else { "masculine" }
            provider = @{
                kind = $kind; model = $modelField.Text
                base_url = if ($kind -eq "openai_compatible_api") { $baseURLField.Text } else { "" }
                credential_available = if ($kind -eq "codex_cli") { $false } else { $credentialCheck.Checked }
            }
        }
        $response = Invoke-CompanionBackend $request
        if ([string]::IsNullOrWhiteSpace([string]$response.review_text)) { throw "The setup summary was incomplete." }
        $script:ReviewSummary = ([string]$response.review_text) -replace "\r?\n", "`r`n"
        $script:ReviewDetails = $response | ConvertTo-Json -Depth 12
        $planView.Text = $script:ReviewSummary
        $detailsCheck.Enabled = $true
        $consentCheck.Checked = $false; $consentCheck.Enabled = $true
        $installButton.Enabled = $false
        $status.Text = "Review ready. This preview cannot install yet."
    } catch { [Windows.Forms.MessageBox]::Show($_.Exception.Message, "Daemon Harness Companion", "OK", "Warning") | Out-Null }
})

$consentCheck.Add_CheckedChanged({
    $installButton.Enabled = $false
    if ($consentCheck.Checked) { $status.Text = "Consent recorded locally. The install action is not enabled in this preview." }
})

$form.Add_Shown({
    try {
        $response = Invoke-CompanionBackend @{schema = "daemon.companion-request.v1"; action = "inspect"}
        $inspection = $response.inspection
        $script:UsableHosts = @($inspection.usable_hosts)
        $hostChoice.DisplayMember = "label"; $hostChoice.ValueMember = "code"
        foreach ($detectedHost in $inspection.hosts) {
            $title = if ($detectedHost.host -eq "hermes") { "Hermes (recommended)" } else { "OpenClaw" }
            $detail = if ($detectedHost.version) { $detectedHost.version } elseif ($detectedHost.detected) { "Needs review" } else { "Not installed" }
            [void]$hostChoice.Items.Add([pscustomobject]@{label = "$title - $detail"; code = $detectedHost.host})
        }
        $selected = [string]$inspection.recommended_host
        for ($index = 0; $index -lt $hostChoice.Items.Count; $index++) {
            if ($hostChoice.Items[$index].code -eq $selected) { $hostChoice.SelectedIndex = $index; break }
        }
        $hostChoice.Enabled = $true
        $hostSetupButton.Enabled = $true
        $reviewButton.Enabled = $script:UsableHosts -contains $selected
        if ($script:UsableHosts.Count -eq 0) { $status.Text = "Review a qualified host version. This preview cannot install it yet." }
        elseif ($script:UsableHosts.Count -eq 1) { $status.Text = "The only usable host was selected automatically. No change has been made." }
        else { $status.Text = "Both hosts are available. Hermes is recommended; you remain free to choose." }
        if (-not $inspection.codex_cli_available) { $providerChoice.SelectedIndex = 1 }
    } catch { $status.Text = $_.Exception.Message; [Windows.Forms.MessageBox]::Show($_.Exception.Message, "Daemon Harness Companion", "OK", "Warning") | Out-Null }
})

[void]$form.ShowDialog()
