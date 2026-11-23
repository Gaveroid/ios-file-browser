# Manual test checklist (real device)

The automated tests never touch a device. Run this list before a release, or after
changing `backend/pmd3.py`, `dav.py` or `server.py`.

Use Explorer, `cmd` or Windows PowerShell 5.1 (`powershell.exe`) for the drive checks.
PowerShell 7 mislists WebDAV drives on some Windows builds.

## Setup

- [ ] Apple Devices app or iTunes is installed; "Apple Mobile Device Service" is running.
- [ ] Device connected by USB, unlocked, trusted.
- [ ] `uv run pytest` passes.

## Detection and pairing

- [ ] `uv run iosfb devices` lists each connected device with its apps.
- [ ] With no device connected it prints the "No devices found" hint and exits with 1.
- [ ] A device that has never trusted this PC: `iosfb serve` prints "tap Trust" once, and
      the device folder appears within a few seconds of tapping Trust.
- [ ] With the Apple service stopped, the error names Apple Mobile Device Service.

## Mount and browse

- [ ] `uv run iosfb serve --mount Z:` prints the URL and "Mapped Z:".
- [ ] `Z:\` shows one folder per device; each has `Apps` and `Media`.
- [ ] `Media\DCIM` lists camera folders; a folder with thousands of photos lists in a few
      seconds.
- [ ] `Apps` shows the file-sharing apps; opening several shows their documents.
- [ ] File dates and sizes in Explorer look right.
- [ ] `http://127.0.0.1:8765/` in a browser shows the same tree.

## Copy out

- [ ] Copy a photo to the PC and open it.
- [ ] Copy a file over 100 MB; compare its SHA-256 with the same file fetched by
      `pymobiledevice3 afc pull` (media) or `pymobiledevice3 apps pull` (app documents).
- [ ] Download a file over 4 GB with `curl -O http://127.0.0.1:8765/<path>`; memory use of
      the `iosfb` process stays flat.
- [ ] `curl -r 1000-1999 <url>` returns exactly 1000 bytes (HTTP 206).

## Safety

- [ ] Creating, renaming and deleting a file in Explorer are all refused, and the device
      content is unchanged.
- [ ] `Get-NetTCPConnection -LocalPort 8765 -State Listen` shows `127.0.0.1` only.
- [ ] From another machine on the LAN, `http://<pc-ip>:8765/` does not connect.

## Writing (`iosfb serve --writable`)

Work only inside a scratch folder you create, for example `Media\Downloads\iosfb-test`.

- [ ] Create a folder, copy a small file in, open it from the drive.
- [ ] Copy in a file over 100 MB; copy it back out and compare SHA-256.
- [ ] The copied file keeps its original modified date.
- [ ] Edit a text file in Notepad and save; reopen it.
- [ ] Rename a file and a folder; move a file into a subfolder.
- [ ] Repeat create, rename and delete inside one app folder under `Apps`.
- [ ] Move a file from one app folder to another.
- [ ] Delete the scratch folder with its contents; no `.iosfb-part-` files remain.
- [ ] Renaming or deleting `Apps`, `Media`, a device folder or an app folder is refused.
- [ ] Unplug during a copy in: the copy fails, and after replugging the destination holds
      either the old file or nothing (a `.iosfb-part-` file may remain; delete it).
- [ ] The file shows up in the app on the device (for example VLC, or Files → On My iPhone).

## Unplug, replug, lock

- [ ] Unplug during browsing: the device folder disappears within about five seconds,
      Explorer does not hang, other devices keep working.
- [ ] Unplug during a large copy: the copy fails with an error; the server keeps running.
- [ ] Replug: the folder returns without restarting `iosfb`.
- [ ] Lock the device: note whether `DCIM` still lists (record the iOS version).

## Shutdown

- [ ] `Ctrl+C` stops the server and removes the `Z:` mapping.
- [ ] `uv run iosfb unmount Z:` removes a mapping left behind by a killed process.

## Results log

| Date | Device / iOS | Windows build | Result |
|---|---|---|---|
| 2026-11-22 | iPhone 15 Pro / 27.2.1 | 26300.9974 | Mount, browse (39 apps), 2 MB and 379 MB copies match by SHA-256, write refused, loopback only, unplug handled. Trust prompt, >4 GB download, locked-device and Ctrl+C checks not yet run. |
| 2026-11-22 | iPad (9th gen) / 27.2 | 26300.9974 | Mount, browse (12 apps), 3.7 MB and 220 MB copies match by SHA-256. |
| 2026-11-22 | iPad (9th gen) / 27.2 | 26300.9974 | After the review fixes (commit f7dca1c): mount, browse, 3.7 MB and 220 MB copies match by SHA-256, case-variant paths resolve, write refused, byte-range request correct. iPhone not re-run (it had been unplugged). |
| 2026-11-23 | iPad (9th gen) / 27.2 | 26300.9974 | Writable mode, in `Media/Downloads` and the a-Shell folder over HTTP: mkdir, create, replace, 64 MB upload matching by SHA-256, rename, rename over a file, folder rename, set date, recursive delete, no temporary files left. Through the mapped drive in `Media/Downloads`: mkdir, create, append, 48 MB copy in (about 14 MB/s) with date kept, rename, move, delete. Not run: Explorer and Notepad by hand, move between apps, unplug during a copy in, visibility in the app on the device. |
| 2026-11-23 | iPhone 15 Pro / 27.2.1 | 26300.9974 | Writable mode over HTTP: all checks pass in `Media/Downloads`. In the VLC folder mkdir, create and replace passed, then the phone dropped off USB during the 64 MB upload (HTTP 503); the scratch folder `iosfb-write-test` was left in VLC's documents. |
