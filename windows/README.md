# OnStep discovery for Windows (ASCOM / NINA)

`find_onstep.py` discovers your OnStep mount on whatever network the telescope
PC is on and pins a **stable hostname** (`onstep`) to its current IP via the
Windows hosts file. Configure ASCOM **once** to use `onstep`, and it keeps
working at home, in the field, anywhere — no matter how the DHCP subnet changes.

It **reuses the exact discovery code from the hand controller**
(`onstep_handset/discovery.py`) — the same cascade proven on the Pi:

1. **Cached IP** — last known-good address (validated instantly).
2. **mDNS** — `onstep.local` / `onstepsws.local`.
3. **Subnet sweep** — scans the local network for TCP 9999, confirming each
   candidate with the LX200 `:GVP#` query (must answer `On-Step#`).

> This replaces the earlier PowerShell version. If the hostname never resolved
> before, it was almost certainly the PowerShell sweep failing to detect open
> ports; this Python version uses the handset's battle-tested sweep instead.

## Requirements

- **Python 3** on the telescope PC (3.8+). The discovery code is **standard
  library only** — no `pip install` needed.
- The repo checked out so `find_onstep.py` sits next to the `onstep_handset/`
  package (i.e. clone this repo on the Windows PC). The script adds the repo root
  to `sys.path` and imports `onstep_handset.discovery`.

## One-time setup

```bat
:: From the repo root, in a normal Command Prompt / PowerShell:
python windows\find_onstep.py            :: discover now (self-elevates for hosts)
python windows\find_onstep.py --install  :: also run at every logon
```

`--install` registers a Task Scheduler job ("OnStep Discovery") that runs at
logon, elevated, 15 s after login (so Wi-Fi has time to associate). Writing the
hosts file needs Administrator, so the script **self-elevates** (UAC prompt) —
you don't need to open an admin shell yourself.

Then point your ASCOM OnStep/LX200 driver at:

```
Host / Address:  onstep
Port:            9999
```

## Usage / options

```bat
python windows\find_onstep.py              :: discover + update hosts
python windows\find_onstep.py --once       :: single attempt (no retry loop)
python windows\find_onstep.py --uninstall  :: remove the logon task
python windows\find_onstep.py --prefix 23  :: sweep a /23 (default is /24)
```

Other flags: `--hostname <name>` (default `onstep`), `--port <n>` (9999),
`--hostnames onstep.local ...` (mDNS names), `--scan-timeout`, `--retries`,
`--quiet`. Run `python windows\find_onstep.py -h` for all.

## What it writes

- **Hosts file** (`%SystemRoot%\System32\drivers\etc\hosts`) — one managed line,
  tagged so re-runs replace it cleanly and never touch your other entries:

  ```
  192.168.4.20	onstep	# OnStep auto-discovery
  ```

- **Cache + log** (`%ProgramData%\OnStep\`) — `discovered_host` (last good IP, so
  next run is instant) and `discovery.log` (timestamped results).

## Notes / troubleshooting

- **Subnet size** — when the netmask can't be auto-detected on Windows the sweep
  uses a **/24** (covers almost all home/field networks). For a larger network
  pass `--prefix 23`/`22`/etc.
- **Driver only accepts a numeric IP?** Some ASCOM dialogs reject hostnames. If
  yours does, read the current IP from `%ProgramData%\OnStep\discovered_host`, or
  ask for the variant that writes the IP straight into the driver's ASCOM profile.
- **Nothing found at startup** — the script retries a few times while the network
  comes up; check `discovery.log`. Make sure the PC and mount are on the same LAN.
- **`onstep.local` already resolves for you?** Windows 10+ can do mDNS, so some
  drivers can use `onstep.local` directly — but the hosts-file approach is more
  reliable and works with drivers that don't do mDNS.

## macOS / Linux (discovery-only)

The hosts-pinning and `--install` are Windows-only. On macOS/Linux the same
script runs as a **discovery-only** helper — it prints the mount's IP on stdout
(and caches it) but never touches `/etc/hosts`:

```bash
python3 windows/find_onstep.py            # prints e.g. 192.168.4.20
IP=$(python3 windows/find_onstep.py)      # capture it for INDI/Alpaca/scripts
```

Use that IP directly in INDI/Alpaca, or add `<ip>  onstep` to `/etc/hosts`
yourself (`sudo`). (No `--install`; logs go to stderr so stdout is just the IP.)

## Tests

`tests/test_find_onstep.py` covers the hosts rewrite, the discovery reuse
(against a mock OnStep), and the macOS/Linux discovery-only path. Runs on any OS:

```
python -m pytest windows/tests/
```
