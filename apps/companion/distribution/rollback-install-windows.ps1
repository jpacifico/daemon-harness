# Daemons Companion candidate. Windows PowerShell 5.1 and PowerShell 7.
# The complete script is parsed before the script block can execute.
    [CmdletBinding()]
    param(
        [string]$Package,
        [string]$Destination,
        [switch]$NoLaunch,
        [switch]$VerifyOnly
    )
    $ErrorActionPreference = 'Stop'
    Set-StrictMode -Version Latest
    $daemonRepo = 'jpacifico/daemon-harness'
    $daemonTag = 'companion-v0.11.16'
    $daemonAsset = 'Daemons-Companion-Windows-x64.exe'
    $daemonDigest = '0d49d88d7a0bf9c20945cb520b781e703a377ce06e4d4160b5af794cd528f39a'
    $daemonScratch = $null
    try {
        if ([Environment]::OSVersion.Platform -ne 'Win32NT') { throw 'This package requires Windows.' }
        $daemonArchitecture = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
        if ($daemonArchitecture -ne 'AMD64') { throw 'This candidate requires Windows x64.' }
        if (-not $Destination) { $Destination = Join-Path $env:LOCALAPPDATA 'Programs\Daemons Companion' }
        if (-not [IO.Path]::IsPathRooted($Destination) -or $Destination -match '["\r\n]') { throw 'Invalid app destination.' }
        $Destination = [IO.Path]::GetFullPath($Destination).TrimEnd('\')
        if ($Destination -eq [IO.Path]::GetPathRoot($Destination).TrimEnd('\')) { throw 'Cannot install into a drive root.' }
        $daemonExe = Join-Path $Destination 'Daemons Companion.exe'
        $daemonScratch = Join-Path ([IO.Path]::GetTempPath()) ('daemons-app-download-' + [guid]::NewGuid().ToString('N'))
        $null = New-Item -ItemType Directory -Path $daemonScratch
        if (-not $Package) {
            $Package = Join-Path $daemonScratch $daemonAsset
            Write-Host 'Downloading Daemons Companion (124 MB)...'
            if (Get-Command gh -ErrorAction SilentlyContinue) {
                $daemonDownload = [Diagnostics.Process]::new()
                try {
                    $daemonDownload.StartInfo.FileName = (Get-Command gh).Source
                    $daemonDownload.StartInfo.Arguments = "release download $daemonTag --repo $daemonRepo --pattern $daemonAsset --output `"$Package`""
                    $daemonDownload.StartInfo.UseShellExecute = $false
                    $daemonDownload.StartInfo.CreateNoWindow = $true
                    $daemonDownload.StartInfo.RedirectStandardOutput = $true
                    $daemonDownload.StartInfo.RedirectStandardError = $true
                    $null = $daemonDownload.Start()
                    $daemonDownloadOutput = $daemonDownload.StandardOutput.ReadToEndAsync()
                    $daemonDownloadError = $daemonDownload.StandardError.ReadToEndAsync()
                    while (-not $daemonDownload.WaitForExit(500)) { Write-Progress -Activity 'Downloading Daemons Companion' -Status 'Receiving the app...' }
                    $daemonDownload.WaitForExit()
                    $null = $daemonDownloadOutput.Result
                    Write-Progress -Activity 'Downloading Daemons Companion' -Completed
                    if ($daemonDownload.ExitCode -ne 0) { throw "Download failed (exit $($daemonDownload.ExitCode)). Check your GitHub access with gh auth login. $($daemonDownloadError.Result.Trim())" }
                } finally {
                    if (-not $daemonDownload.HasExited) { $daemonDownload.Kill(); $daemonDownload.WaitForExit() }
                    $daemonDownload.Dispose()
                }
            } else {
                [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
                try {
                    Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/$daemonRepo/releases/download/$daemonTag/$daemonAsset" -OutFile $Package
                } catch { throw 'This candidate is private. Install GitHub CLI and run gh auth login, then retry.' }
            }
        }
        if (-not (Test-Path -LiteralPath $Package -PathType Leaf)) { throw 'Package must be a regular file.' }
        if ((Get-Item -LiteralPath $Package).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Package cannot be a link.' }
        Write-Host 'Verifying download...'
        $daemonStream = [IO.File]::OpenRead($Package)
        $daemonHasher = [Security.Cryptography.SHA256]::Create()
        try { $daemonActual = [BitConverter]::ToString($daemonHasher.ComputeHash($daemonStream)).Replace('-', '').ToLowerInvariant() }
        finally { $daemonHasher.Dispose(); $daemonStream.Dispose() }
        if ($daemonActual -ne $daemonDigest) {
            throw 'Download checksum mismatch. Nothing was installed.'
        }
        if ($VerifyOnly) { Write-Host 'Download verified.'; return }
        $daemonParent = [IO.DirectoryInfo]::new($Destination)
        while ($null -ne $daemonParent) {
            if ($daemonParent.Exists -and ($daemonParent.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'App destination cannot traverse a link.' }
            $daemonParent = $daemonParent.Parent
        }
        # Stable UUID v5 generated by electron-builder for org.daemonharness.companion.
        # The display name includes a version and is not an installation identity.
        $daemonRegistered = $false
        $daemonRegistry = [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::CurrentUser, [Microsoft.Win32.RegistryView]::Registry64)
        try {
            $daemonKey = $daemonRegistry.OpenSubKey('Software\f8085670-4857-5ac8-977d-f3222147bb78')
            if ($null -ne $daemonKey) {
                try { $daemonRegisteredPath = $daemonKey.GetValue('InstallLocation') } finally { $daemonKey.Dispose() }
                if (-not $daemonRegisteredPath -or -not [string]::Equals([IO.Path]::GetFullPath($daemonRegisteredPath).TrimEnd('\'), $Destination, [StringComparison]::OrdinalIgnoreCase)) {
                    throw 'Daemons Companion is already registered elsewhere. Open that app instead; this command will not relocate it.'
                }
                $daemonRegistered = $true
            }
        } finally { $daemonRegistry.Dispose() }
        if (Test-Path -LiteralPath $Destination) {
            if (-not (Test-Path -LiteralPath $Destination -PathType Container)) { throw 'An unrelated item occupies the app destination.' }
            if (@(Get-ChildItem -LiteralPath $Destination -Force).Count -gt 0 -and
                (-not $daemonRegistered -or -not (Test-Path -LiteralPath $daemonExe -PathType Leaf))) {
                throw 'An unrecognized folder occupies the app destination.'
            }
        }
        $daemonRunning = @(Get-Process -Name 'Daemons Companion' -ErrorAction SilentlyContinue |
            Where-Object { $_.Path -and $_.Path.StartsWith($Destination + '\', [StringComparison]::OrdinalIgnoreCase) })
        if ($daemonRunning.Count) { throw 'Close Daemons Companion, then retry. Your daemon can keep running.' }
        Write-Host "Installing the app in $Destination..."
        # /D must be last and unquoted for NSIS, including paths containing spaces.
        $daemonInstaller = Start-Process -FilePath $Package -ArgumentList @('/S', '/currentuser', "/D=$Destination") -PassThru
        while (-not $daemonInstaller.WaitForExit(500)) { Write-Progress -Activity 'Installing Daemons Companion' -Status 'Preparing the app...' }
        Write-Progress -Activity 'Installing Daemons Companion' -Completed
        if ($daemonInstaller.ExitCode -ne 0) { throw "App installer failed (exit $($daemonInstaller.ExitCode)). Existing daemon data was not changed by this script." }
        if (-not (Test-Path -LiteralPath $daemonExe -PathType Leaf)) { throw 'The installer did not produce the expected app.' }
        Write-Host "Daemons Companion installed: $daemonExe"
        if (-not $NoLaunch) {
            Write-Host 'Opening the app. Continue setup in its window.'
            $null = Start-Process -FilePath $daemonExe
        }
    } finally {
        if ($daemonScratch -and (Test-Path -LiteralPath $daemonScratch)) { Remove-Item -LiteralPath $daemonScratch -Recurse -Force }
    }
