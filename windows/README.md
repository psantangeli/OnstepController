# OnStep discovery for Windows (ASCOM / NINA)

`Find-OnStep.ps1` discovers your OnStep mount on whatever network the telescope
PC is on and pins a **stable hostname** (`onstep`) to its current IP via the
Windows hosts file. You configure ASCOM **once** to use `onstep`, and it keeps
working at home, in the field, anywhere — no matter how the DHCP subnet changes.

It uses the same discovery cascade as the Pi hand controller:

1. **Cached IP** — last known-good address (validated instantly).
2. **mDNS** — `onstep.local` / `onstepsws.local`.
3. **Subnet sweep** — scans the local network for TCP 9999, confirming each
   candidate with the LX200 `:GVP#` query (must answer `On-Step#`).

## One-time setup

1. **Copy the script** to the telescope PC, e.g. `C:\Tools\OnStep\Find-OnStep.ps1`.

2. **Register it to run at logon** (from an **Administrator** PowerShell — editing
   the hosts file requires elevation):

   ```powershell
   powershell -ExecutionPolicy Bypass -File C:\Tools\OnStep\Find-OnStep.ps1 -Install
   ```

   This creates a Scheduled Task ("OnStep Discovery") that runs at logon, elevated,
   15 s after login so Wi-Fi has time to associate.

3. **Point your driver at the hostname.** In the ASCOM OnStep/LX200 driver setup
   (as used by NINA, SGP, PHD2, etc.):

   ```
   Host / Address:  onstep
   Port:            9999
   ```

That's it. On each logon the task finds the mount and updates the `onstep` entry;
your driver connects by name.

## Running it manually

```powershell
# Run discovery now (Administrator PowerShell, to write the hosts file):
powershell -ExecutionPolicy Bypass -File .\Find-OnStep.ps1

# Remove the startup task:
powershell -ExecutionPolicy Bypass -File .\Find-OnStep.ps1 -Uninstall
```

Useful options: `-HostAlias <name>` (default `onstep`), `-Port <n>` (default 9999),
`-ScanTimeoutMs`, `-Retries`. See `Get-Help .\Find-OnStep.ps1 -Full`.

## What it writes

- **Hosts file** (`%SystemRoot%\System32\drivers\etc\hosts`) — one managed line,
  tagged so re-runs replace it cleanly and never touch your other entries:

  ```
  192.168.4.20	onstep	# OnStep auto-discovery
  ```

- **Cache + log** (`%ProgramData%\OnStep\`) — `discovered_host` (last good IP) and
  `discovery.log` (timestamped results, handy for checking what the startup task
  did).

## Troubleshooting

- **"NOT elevated" / hosts not updated** — the hosts file needs Administrator.
  Run the script (or the scheduled task) elevated. The `-Install` task already
  runs with highest privileges.
- **Execution policy blocks the script** — use `-ExecutionPolicy Bypass` as shown,
  or `Unblock-File .\Find-OnStep.ps1` once after copying it over.
- **Driver only accepts a numeric IP** — some ASCOM dialogs validate the host
  field as four octets and reject a hostname. If yours does, check
  `%ProgramData%\OnStep\discovered_host` for the current IP, or tell me and I'll
  switch the script to write the IP straight into the driver's ASCOM profile.
- **Nothing found at startup** — the script retries a few times (network may still
  be coming up). Check `discovery.log`. Make sure the PC and mount are on the same
  LAN and the mount is powered.
- **`onstep.local` already works for you?** Windows 10+ can resolve `.local` via
  mDNS, so you may be able to use `onstep.local` directly in some drivers — but the
  hosts-file approach is more reliable and works with drivers that don't do mDNS.

## Tests

`tests/Test-FindOnStep.ps1` covers the portable logic (IPv4 enumeration, hosts
rewrite, `:GVP#` identify, port sweep) and runs on any PowerShell 7:

```powershell
pwsh -File tests/Test-FindOnStep.ps1
```
