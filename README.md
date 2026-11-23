# iOS File Browser

Browse the files on an iPhone or iPad from Windows as an ordinary drive letter.

`iosfb` reads the device over USB and serves it as a WebDAV share on `127.0.0.1`,
read-only unless you start it with `--writable`. Windows mounts that with its built-in client, so there is no file-system
driver to install and nothing is exposed to your network.

```
Z:\
└── Gavin's iPhone\
    ├── Apps\            Documents folder of every app that enables file sharing
    │   ├── VLC\
    │   └── ...
    └── Media\           DCIM (camera roll), Downloads, Books, Recordings, ...
```

## What you can and cannot reach

iOS decides this, not the tool. On a device that is not jailbroken:

| Content | Reachable |
|---|---|
| Media area: `DCIM`, `Downloads`, `Books`, `Recordings`, `Music`, ... | Yes |
| Documents of apps that enable file sharing (the apps listed under "File Sharing" in iTunes/Finder) | Yes |
| The "On My iPhone" folder of those same apps in the Files app | Yes, it is the same folder |
| iCloud Drive, other cloud providers, and "On My iPhone" folders of apps without file sharing | No |
| Other app data, system files | No |

Photos stored only in iCloud ("Optimise iPhone Storage") are not on the device, so
`DCIM` may hold fewer files than the Photos app shows.

`DECISIONS.md` has the background and sources.

## Requirements

- Windows 10 or 11.
- **Apple Mobile Device Support**, which provides Apple's USB driver and the service that
  talks to the device. Install either the
  [Apple Devices](https://apps.microsoft.com/detail/9np83lwlpz9k) app from the Microsoft
  Store or iTunes. This is the one piece of system software the tool depends on; no
  open source replacement exists for Windows.
- [uv](https://docs.astral.sh/uv/) (or Python 3.10+ and pip).

## Install

```powershell
git clone <this repository>
cd ios-file-browser
uv sync
```

## Use

1. Connect the device by USB and unlock it. The first time, tap **Trust** on the device
   and enter its passcode.
2. Check that it is seen:

   ```powershell
   uv run iosfb devices
   ```

3. Start the share and map it to a drive letter:

   ```powershell
   uv run iosfb serve --mount Z:
   ```

   Leave this window open. Drive `Z:` now appears in Explorer. Press `Ctrl+C` to stop;
   the drive mapping is removed for you.

To try it without a device, add `--demo` to either command.

### Devices on Wi-Fi

A device that allows Wi-Fi connections is served without a cable, as long as it is on the
same network as the PC and awake. Allow it once while the device is connected by USB:
tick "Show this iPhone when on Wi-Fi" in the Apple Devices app, or run
`uv run pymobiledevice3 lockdown wifi-connections on`.

A device reachable both ways is used over USB. Wi-Fi is slower, and the device drops out
when it sleeps. `--usb-only` ignores Wi-Fi devices. The share itself stays on `127.0.0.1`.

### Writing to the device

The share is read-only by default. To copy files in, edit, rename and delete:

```powershell
uv run iosfb serve --mount Z: --writable
```

- It works in `Media` and inside each app's folder under `Apps`. The fixed folders
  themselves (the device, `Apps`, `Media`, each app) cannot be created, renamed or deleted.
- **Deleting is immediate and permanent.** There is no Recycle Bin on this drive and no
  undo on the device.
- A file being copied in appears as `.iosfb-part-…` until it is complete, then takes its
  real name. If a copy fails, the previous version of the file is kept. A `.iosfb-part-…`
  file left behind after a crash or unplug can be deleted.
- File dates are carried over when copying in.
- Moving between two apps, or between devices, copies and then deletes the original.
- Changing files inside `Media` folders that iOS manages (`DCIM`, `PhotoData`,
  `iTunes_Control`, `Books`) can confuse the Photos, Music and Books apps: their databases
  are not updated. `Downloads` and the app folders are the safe places to write.
  Photos copied into `DCIM` do not show up in the Photos app.
- The Windows size limits below apply to copying in as well.

### Mounting by hand

Run `uv run iosfb serve` (default port 8765, change it with `--port`), then either:

- **Command line:** `net use Z: http://127.0.0.1:8765/` and later `net use Z: /delete`
- **Explorer:** This PC → `...` → **Map network drive** → Folder:
  `http://127.0.0.1:8765/` → Finish. Untick "Reconnect at sign-in", because the share
  only exists while `iosfb serve` is running.

You can also open `http://127.0.0.1:8765/` in a web browser for a plain listing.

## Limits of the Windows WebDAV client

These come from Windows, not from this tool:

- **Files are downloaded in full before they open.** Opening a 2 GB video from `Z:` waits
  for the whole file. Copying it out first is usually the better choice. Expect roughly
  15 to 25 MB/s over USB.
- **Files over 50 MB fail until you raise a Windows limit**, and nothing over 4 GB can be
  read at all. To allow up to 4 GB, run this once in an administrator PowerShell, then
  restart the `WebClient` service or reboot:

  ```powershell
  Set-ItemProperty HKLM:\SYSTEM\CurrentControlSet\Services\WebClient\Parameters FileSizeLimitInBytes 4294967295 -Type DWord
  ```

  For bigger files, download them from `http://127.0.0.1:8765/` with a browser or `curl`,
  which has no size limit.
- **Paths longer than about 260 characters are not accessible** through the drive.
- **PowerShell 7 lists WebDAV drives incorrectly on some Windows builds** (names gain an
  invisible trailing character and commands fail with "Null character in path"). Explorer,
  `cmd` and Windows PowerShell 5.1 are not affected. Seen on build 26300 with a stock
  WebDAV server too.
- Free space shown for the drive is that of `C:`, not the device.
- Microsoft has deprecated the WebDAV client. It still ships with Windows 11 and has no
  announced removal date.

## Behaviour notes

- **Read-only unless `--writable` is given.** Without it, writes, renames and deletes are
  refused and nothing on the device is changed.
- Web pages cannot change files even in writable mode: requests that carry a browser
  `Origin` header are refused.
- **Local only.** The server binds to `127.0.0.1` and rejects requests addressed to any
  other host name. There is no password because no other machine can connect.
- **Unplugging is fine.** The device folder disappears and returns when you plug it back
  in. Listings are cached for five seconds, so a change can take that long to show.
- Several devices can be connected at once; each gets its own folder.
- Characters Windows forbids in file names (`: ? * " < > |`) are shown as their
  full-width look-alikes.

## Development

```powershell
uv run pytest
```

The tests need no device: they run against an in-memory backend and a fake of the device
library. `docs/MANUAL_TEST.md` is the checklist for real-device runs.

Layout:

| Path | Role |
|---|---|
| `src/iosfb/backend/base.py` | The interface a device backend implements |
| `src/iosfb/backend/pmd3.py` | Real backend, built on pymobiledevice3 |
| `src/iosfb/backend/mock.py` | In-memory backend for tests and `--demo` |
| `src/iosfb/vfs.py` | The virtual tree, naming and listing cache |
| `src/iosfb/dav.py`, `server.py` | WebDAV provider and loopback HTTP server |
| `src/iosfb/cli.py` | Command line |

## License

GPL-3.0-or-later, because the device backend,
[pymobiledevice3](https://github.com/doronz88/pymobiledevice3), is GPL-3.0. See `LICENSE`.
