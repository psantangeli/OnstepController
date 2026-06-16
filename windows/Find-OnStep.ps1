<#
.SYNOPSIS
    Discover an OnStep telescope mount on the local network and pin a stable
    hostname ("onstep") to its current IP via the Windows hosts file, so ASCOM /
    NINA / PHD2 can connect by name regardless of which subnet you're on.

.DESCRIPTION
    Runs the same discovery cascade as the Pi hand controller:
      1. Cached IP  (last known-good, validated instantly)
      2. mDNS       (onstep.local / onstepsws.local)
      3. Subnet sweep of TCP 9999, confirming each candidate with :GVP#
    Every candidate is confirmed on the real control channel (TCP 9999 + the
    LX200 ':GVP#' query returning 'On-Step#'), so it never pins a wrong host.

    On success it rewrites a single managed line in
    %SystemRoot%\System32\drivers\etc\hosts:

        <discovered-ip>   onstep   # OnStep auto-discovery

    Point your ASCOM OnStep/LX200 driver at host "onstep", port 9999, once.

.PARAMETER Install
    Register a Scheduled Task that runs this script at logon with the elevation
    needed to edit the hosts file. Use once to set up startup discovery.

.PARAMETER Uninstall
    Remove the Scheduled Task created by -Install.

.PARAMETER HostAlias
    The hostname written to the hosts file (default: onstep).

.PARAMETER Port
    OnStep LX200 command port (default: 9999).

.EXAMPLE
    # One-time: run discovery now (must be elevated to edit hosts)
    powershell -ExecutionPolicy Bypass -File .\Find-OnStep.ps1

.EXAMPLE
    # Register it to run automatically at every logon
    powershell -ExecutionPolicy Bypass -File .\Find-OnStep.ps1 -Install
#>
[CmdletBinding()]
param(
    [switch]$Install,
    [switch]$Uninstall,
    [string]$HostAlias = "onstep",
    [int]$Port = 9999,
    [string[]]$MdnsNames = @("onstep.local", "onstepsws.local"),
    [int]$ScanTimeoutMs = 300,
    [int]$ConfirmTimeoutMs = 1000,
    [int]$Retries = 5,
    [int]$RetryDelaySec = 3,
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"
$MARKER = "# OnStep auto-discovery"
# %ProgramData% on Windows; fall back to a temp dir when imported for tests on
# non-Windows PowerShell.
$DataRoot = if ($env:ProgramData) { $env:ProgramData } elseif ($env:TMPDIR) { $env:TMPDIR } else { "/tmp" }
$CacheDir = Join-Path $DataRoot "OnStep"
$CachePath = Join-Path $CacheDir "discovered_host"
$LogPath = Join-Path $CacheDir "discovery.log"

# --- logging -----------------------------------------------------------------

function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $line = "{0} {1} {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Message
    if (-not $Quiet) { Write-Host $line }
    try {
        if (-not (Test-Path $CacheDir)) { New-Item -ItemType Directory -Path $CacheDir -Force | Out-Null }
        Add-Content -LiteralPath $LogPath -Value $line -ErrorAction SilentlyContinue
    } catch { }
}

# --- candidate confirmation (TCP 9999 + :GVP#) -------------------------------

function Test-OnStep {
    <# Returns $true if host:port answers :GVP# as an OnStep device. #>
    param([string]$IPAddress, [int]$Port, [int]$TimeoutMs)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect($IPAddress, $Port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne($TimeoutMs)) { return $false }
        $client.EndConnect($iar)
        $stream = $client.GetStream()
        $stream.ReadTimeout = $TimeoutMs
        $stream.WriteTimeout = $TimeoutMs
        # Leading '#' clears any partial LX200 state, then ask for product name.
        $cmd = [System.Text.Encoding]::ASCII.GetBytes("#:GVP#")
        $stream.Write($cmd, 0, $cmd.Length)
        $sb = New-Object System.Text.StringBuilder
        $buf = New-Object byte[] 1
        for ($i = 0; $i -lt 64; $i++) {
            try { $n = $stream.Read($buf, 0, 1) } catch { break }
            if ($n -le 0) { break }
            $ch = [char]$buf[0]
            [void]$sb.Append($ch)
            if ($ch -eq '#') { break }
        }
        $norm = ($sb.ToString() -replace '-', '').ToLower()
        return $norm.Contains("onstep")
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

# --- IPv4 host enumeration ---------------------------------------------------

function Get-Ipv4Hosts {
    <# Usable host addresses for IP/prefix, excluding network+broadcast.
       Caps oversized ranges by falling back to the /24 around the address. #>
    param([string]$IPAddress, [int]$PrefixLength)

    if ($PrefixLength -lt 1 -or $PrefixLength -gt 32) { $PrefixLength = 24 }
    $bytes = ([System.Net.IPAddress]::Parse($IPAddress)).GetAddressBytes()
    [Array]::Reverse($bytes)
    $ipInt = [uint64]([System.BitConverter]::ToUInt32($bytes, 0))

    $size = [uint64][math]::Pow(2, (32 - $PrefixLength))
    if ($size -gt 1024) {            # too big to sweep; scan the local /24 only
        $size = [uint64]256
    }
    # 0xFFFFFFFF would parse as int32 -1 in PowerShell; use the explicit value.
    $mask = ([uint64]4294967295) - ($size - 1)
    $network = $ipInt -band $mask

    $result = New-Object System.Collections.Generic.List[string]
    for ($i = 1; $i -lt ($size - 1); $i++) {
        $addr = [uint32]($network + $i)
        $b = [System.BitConverter]::GetBytes($addr)
        [Array]::Reverse($b)
        $result.Add(([System.Net.IPAddress]::new($b)).ToString()) | Out-Null
    }
    return $result
}

function Get-LocalIPv4Interfaces {
    <# (IPAddress, PrefixLength) for real IPv4 interfaces (no loopback/APIPA). #>
    Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
        Where-Object {
            $_.IPAddress -notlike '127.*' -and
            $_.IPAddress -notlike '169.254.*' -and
            $_.PrefixOrigin -ne 'WellKnown'
        } |
        ForEach-Object { [pscustomobject]@{ IPAddress = $_.IPAddress; PrefixLength = $_.PrefixLength } }
}

# --- parallel port sweep -----------------------------------------------------

function Find-OpenHosts {
    <# Fire non-blocking connects at all IPs, then collect those that opened.
       Total time ~= TimeoutMs regardless of host count. #>
    param([string[]]$IPs, [int]$Port, [int]$TimeoutMs)

    $pending = foreach ($ip in $IPs) {
        $c = New-Object System.Net.Sockets.TcpClient
        [pscustomobject]@{ IP = $ip; Client = $c; Async = $c.BeginConnect($ip, $Port, $null, $null) }
    }
    Start-Sleep -Milliseconds $TimeoutMs
    $open = New-Object System.Collections.Generic.List[string]
    foreach ($p in $pending) {
        $isOpen = $false
        if ($p.Async.IsCompleted) {
            try { $p.Client.EndConnect($p.Async); $isOpen = $p.Client.Connected } catch { $isOpen = $false }
        }
        if ($isOpen) { $open.Add($p.IP) | Out-Null }
        try { $p.Client.Close() } catch { }
    }
    return $open
}

# --- discovery cascade -------------------------------------------------------

function Find-OnStep {
    param([int]$Port, [string[]]$MdnsNames, [int]$ScanTimeoutMs, [int]$ConfirmTimeoutMs)

    # 1. Cached IP.
    if (Test-Path $CachePath) {
        $cached = (Get-Content -LiteralPath $CachePath -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($cached -and (Test-OnStep -IPAddress $cached -Port $Port -TimeoutMs $ConfirmTimeoutMs)) {
            Write-Log "cached host $cached still valid"
            return $cached
        }
    }

    # 2. mDNS hostnames.
    foreach ($name in $MdnsNames) {
        try { $addrs = [System.Net.Dns]::GetHostAddresses($name) } catch { $addrs = @() }
        foreach ($a in $addrs) {
            if ($a.AddressFamily -ne 'InterNetwork') { continue }
            $ip = $a.IPAddressToString
            if (Test-OnStep -IPAddress $ip -Port $Port -TimeoutMs $ConfirmTimeoutMs) {
                Write-Log "resolved $name -> $ip"
                return $ip
            }
        }
    }

    # 3. Subnet sweep on every local interface.
    foreach ($iface in (Get-LocalIPv4Interfaces)) {
        $hosts = Get-Ipv4Hosts -IPAddress $iface.IPAddress -PrefixLength $iface.PrefixLength
        Write-Log ("sweeping {0}/{1} ({2} hosts) on port {3}" -f $iface.IPAddress, $iface.PrefixLength, $hosts.Count, $Port)
        $open = Find-OpenHosts -IPs $hosts -Port $Port -TimeoutMs $ScanTimeoutMs
        foreach ($ip in $open) {
            if (Test-OnStep -IPAddress $ip -Port $Port -TimeoutMs $ConfirmTimeoutMs) {
                Write-Log "found OnStep at $ip"
                return $ip
            }
        }
    }
    return $null
}

# --- hosts file --------------------------------------------------------------

function Update-HostsEntry {
    <# Rewrite the single managed line for $HostAlias in $HostsPath. Returns $true
       if the file changed. Pure enough to unit-test against a temp file. #>
    param([string]$HostsPath, [string]$HostAlias, [string]$IPAddress)

    $existing = @()
    if (Test-Path -LiteralPath $HostsPath) {
        $existing = @(Get-Content -LiteralPath $HostsPath -ErrorAction Stop)
    }
    $aliasPattern = "(^|\s)$([regex]::Escape($HostAlias))(\s|$)"
    $kept = $existing | Where-Object {
        ($_ -notmatch [regex]::Escape($MARKER)) -and ($_ -notmatch $aliasPattern)
    }
    $newLine = "$IPAddress`t$HostAlias`t$MARKER"
    $updated = @($kept) + $newLine

    $before = ($existing -join "`n")
    $after = ($updated -join "`n")
    if ($before -eq $after) { return $false }

    Set-Content -LiteralPath $HostsPath -Value $updated -Encoding ASCII
    return $true
}

# --- elevation / scheduled task ----------------------------------------------

function Test-Admin {
    $id = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object System.Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Install-Task {
    if (-not (Test-Admin)) { throw "Run -Install from an elevated (Administrator) PowerShell." }
    $taskName = "OnStep Discovery"
    $argline = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSCommandPath`" -Quiet"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argline
    # Run shortly after logon (give Wi-Fi time to associate).
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $trigger.Delay = "PT15S"
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -StartWhenAvailable
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force | Out-Null
    Write-Log "registered scheduled task '$taskName' (runs at logon, elevated)"
}

function Uninstall-Task {
    if (-not (Test-Admin)) { throw "Run -Uninstall from an elevated (Administrator) PowerShell." }
    Unregister-ScheduledTask -TaskName "OnStep Discovery" -Confirm:$false -ErrorAction SilentlyContinue
    Write-Log "removed scheduled task 'OnStep Discovery'"
}

# --- main --------------------------------------------------------------------

# Allow dot-sourcing the functions for tests without executing discovery.
if ($env:ONSTEP_NO_RUN) { return }

if ($Install)   { Install-Task;   return }
if ($Uninstall) { Uninstall-Task; return }

if (-not (Test-Admin)) {
    Write-Log ("NOT elevated -- editing the hosts file requires Administrator. " +
               "Re-run from an elevated PowerShell, or use -Install to schedule it.") "WARN"
}

$found = $null
for ($attempt = 1; $attempt -le $Retries; $attempt++) {
    $found = Find-OnStep -Port $Port -MdnsNames $MdnsNames `
        -ScanTimeoutMs $ScanTimeoutMs -ConfirmTimeoutMs $ConfirmTimeoutMs
    if ($found) { break }
    if ($attempt -lt $Retries) {
        Write-Log ("no OnStep found (attempt {0}/{1}); retrying in {2}s (network may still be coming up)" -f $attempt, $Retries, $RetryDelaySec) "WARN"
        Start-Sleep -Seconds $RetryDelaySec
    }
}

if (-not $found) {
    Write-Log "OnStep not found on any local network." "ERROR"
    exit 1
}

# Cache the winner.
try {
    if (-not (Test-Path $CacheDir)) { New-Item -ItemType Directory -Path $CacheDir -Force | Out-Null }
    Set-Content -LiteralPath $CachePath -Value $found -Encoding ASCII
} catch { }

# Pin the hostname.
$hostsPath = Join-Path $env:SystemRoot "System32\drivers\etc\hosts"
try {
    $changed = Update-HostsEntry -HostsPath $hostsPath -HostAlias $HostAlias -IPAddress $found
    if ($changed) {
        Write-Log "hosts updated: $found -> $HostAlias"
        & ipconfig /flushdns | Out-Null
    } else {
        Write-Log "hosts already current: $found -> $HostAlias"
    }
} catch {
    Write-Log "failed to update hosts file ($hostsPath): $($_.Exception.Message). Are you elevated?" "ERROR"
    exit 2
}

Write-Log "OnStep is reachable as '$HostAlias' (${found}:$Port). Point ASCOM/NINA at host '$HostAlias'."
exit 0
