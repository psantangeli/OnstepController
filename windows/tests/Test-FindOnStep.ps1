<#
  Lightweight assertion tests for the portable logic in Find-OnStep.ps1
  (IPv4 enumeration, hosts-file rewrite, TCP :GVP# identify, port sweep).

  Runs on any PowerShell 7 (Windows/macOS/Linux). The Windows-only paths
  (Get-NetIPAddress, scheduled task, real hosts file) are not exercised here.

  Usage:  pwsh -File windows/tests/Test-FindOnStep.ps1
#>
$ErrorActionPreference = "Stop"
$script:Failures = 0
$script:Count = 0

function Assert-Equal($expected, $actual, $msg) {
    $script:Count++
    if ($expected -ne $actual) {
        $script:Failures++
        Write-Host "FAIL: $msg`n   expected: $expected`n   actual:   $actual" -ForegroundColor Red
    } else {
        Write-Host "ok: $msg" -ForegroundColor Green
    }
}
function Assert-True($cond, $msg) { Assert-Equal $true ([bool]$cond) $msg }

# Import the functions without running discovery.
$env:ONSTEP_NO_RUN = "1"
. (Join-Path $PSScriptRoot "..\Find-OnStep.ps1")

# --- Get-Ipv4Hosts -----------------------------------------------------------

$h24 = Get-Ipv4Hosts -IPAddress "192.168.1.50" -PrefixLength 24
Assert-Equal 254 $h24.Count "/24 yields 254 usable hosts"
Assert-Equal "192.168.1.1" $h24[0] "/24 first host"
Assert-Equal "192.168.1.254" $h24[-1] "/24 last host"

$h30 = Get-Ipv4Hosts -IPAddress "10.0.0.1" -PrefixLength 30
Assert-Equal 2 $h30.Count "/30 yields 2 usable hosts"
Assert-Equal "10.0.0.1" $h30[0] "/30 first host"

# Oversized prefix is capped to a /24-sized sweep (not 1M+ hosts).
$hBig = Get-Ipv4Hosts -IPAddress "172.16.5.9" -PrefixLength 8
Assert-Equal 254 $hBig.Count "oversized range capped to 254 hosts"

# --- Update-HostsEntry (temp file) -------------------------------------------

$tmp = New-TemporaryFile
try {
    Set-Content -LiteralPath $tmp -Value @(
        "127.0.0.1   localhost",
        "192.168.9.9 onstepsws   # unrelated, must be preserved"
    ) -Encoding ASCII

    $changed1 = Update-HostsEntry -HostsPath $tmp -HostAlias "onstep" -IPAddress "192.168.4.20"
    Assert-True $changed1 "first write reports a change"
    $lines = Get-Content -LiteralPath $tmp
    Assert-True ($lines -match "^192\.168\.4\.20\s+onstep\s") "managed line written"
    Assert-True ($lines -match "localhost") "unrelated localhost line preserved"
    Assert-True ($lines -match "onstepsws") "similar alias 'onstepsws' NOT clobbered"

    # Re-running with the same IP is a no-op.
    $changed2 = Update-HostsEntry -HostsPath $tmp -HostAlias "onstep" -IPAddress "192.168.4.20"
    Assert-Equal $false $changed2 "same IP -> no change"

    # New IP replaces the old managed line (no duplicate).
    $changed3 = Update-HostsEntry -HostsPath $tmp -HostAlias "onstep" -IPAddress "10.1.1.7"
    Assert-True $changed3 "new IP reports a change"
    $onstepLines = @(Get-Content -LiteralPath $tmp | Where-Object { $_ -match "\sonstep\s" })
    Assert-Equal 1 $onstepLines.Count "exactly one 'onstep' line after IP change"
    Assert-True ($onstepLines[0] -match "10\.1\.1\.7") "managed line now points at new IP"
} finally {
    Remove-Item -LiteralPath $tmp -ErrorAction SilentlyContinue
}

# --- Test-OnStep / Find-OpenHosts against a mock OnStep ----------------------

$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
$listener.Start()
$mockPort = $listener.LocalEndpoint.Port

$runspace = [powershell]::Create()
$runspace.AddScript({
    param($listener)
    while ($true) {
        try { $client = $listener.AcceptTcpClient() } catch { break }
        try {
            $s = $client.GetStream()
            $s.ReadTimeout = 500
            $buf = New-Object byte[] 32
            try { [void]$s.Read($buf, 0, 32) } catch { }   # consume "#:GVP#"
            $resp = [System.Text.Encoding]::ASCII.GetBytes("On-Step#")
            $s.Write($resp, 0, $resp.Length); $s.Flush()
        } catch { }
        Start-Sleep -Milliseconds 30
        $client.Close()
    }
}).AddArgument($listener) | Out-Null
$async = $runspace.BeginInvoke()

try {
    Start-Sleep -Milliseconds 100
    $ok = Test-OnStep -IPAddress "127.0.0.1" -Port $mockPort -TimeoutMs 1000
    Assert-True $ok "Test-OnStep confirms a mock OnStep (:GVP# -> On-Step#)"

    # A definitely-closed port must not confirm.
    $bad = Test-OnStep -IPAddress "127.0.0.1" -Port 1 -TimeoutMs 300
    Assert-Equal $false $bad "Test-OnStep returns false on a closed port"

    # Sweep should report the mock's port as open.
    $open = Find-OpenHosts -IPs @("127.0.0.1") -Port $mockPort -TimeoutMs 500
    Assert-True ($open -contains "127.0.0.1") "Find-OpenHosts detects the open mock port"
} finally {
    $listener.Stop()
    $runspace.Stop() | Out-Null
    $runspace.Dispose()
}

# --- summary -----------------------------------------------------------------

Write-Host ""
if ($script:Failures -eq 0) {
    Write-Host "ALL $($script:Count) CHECKS PASSED" -ForegroundColor Green
    exit 0
} else {
    Write-Host "$($script:Failures)/$($script:Count) CHECKS FAILED" -ForegroundColor Red
    exit 1
}
