# Decisions

Findings and decisions from Phase 0 (research and feasibility), 2026-11-22.

## Goal

Browse an iPhone's files from Windows with no custom driver on the mount side: a local
server reads the device over USB and exposes it on 127.0.0.1 as a share that Windows can
mount with its built-in client.

## What is reachable on a non-jailbroken device

Checked on an iPhone 15 Pro running iOS 27.2.1 and an iPad on 27.2, both stock.

| Area | Reachable | Via |
|---|---|---|
| Media area (`/var/mobile/Media`: `DCIM`, `Downloads`, `Books`, `Recordings`, `Music`, `PhotoData`, ...) | Yes, read-write | AFC (`com.apple.afc`) |
| `Documents` of apps with `UIFileSharingEnabled` (39 of 408 apps on the test phone) | Yes, read-write | house_arrest `VendDocuments` |
| "On My iPhone" folder of those apps in the Files app (32 of the 39 also set `LSSupportsOpeningDocumentsInPlace`) | Yes, it is the same folder | house_arrest |
| Full app containers | No. `VendContainer` fails with `InstallationLookupFailed` for store apps; it works for development-signed apps only | - |
| iCloud Drive, third-party file providers, "On My iPhone" folders of apps without file sharing | No | nothing over USB |
| Crash logs | Yes (not planned) | `com.apple.crashreportcopymobile` |
| Whole file system | No | needs a jailbreak with AFC2 |

Notes:

- AFC and house_arrest are classic lockdown services. They work over usbmux without the
  iOS 17+ RSD tunnel.
- App sandbox access has been limited to file-sharing apps' `Documents` since iOS 8.3.
- Community reports say `DCIM` looks empty while the phone is locked. Not yet confirmed
  here; it is on the manual test checklist.

## Hard dependency: Apple Mobile Device Support

Every backend evaluated needs **Apple Mobile Device Support** on Windows. It provides the
USB driver and the Apple Mobile Device Service, whose usbmuxd endpoint listens on
`127.0.0.1:27015`. It is installed by iTunes or by the Microsoft Store "Apple Devices"
app. No maintained driverless (libusb/WinUSB) usbmuxd exists for Windows.

So "no driver install" is true for mounting only. Talking to the phone still needs
Apple's own driver and service.

## Backend

| Option | License | Assessment |
|---|---|---|
| **pymobiledevice3** (chosen) | GPL-3.0-or-later | Very active. asyncio API. AFC with offset reads (`pread`), house_arrest, installation_proxy. Reuses Apple's pairing records. |
| idevice (Rust) | MIT | Viable permissive alternative with the same services. |
| go-ios | MIT | Viable; has afc, house_arrest, installationproxy. |
| libimobiledevice | LGPL-2.1 | Active upstream but no maintained Windows builds; the win32 ports are archived. |

**Decision: Python + pymobiledevice3.** It is the fastest path and the most actively
maintained. The original requirement was a permissive license; the project owner decided
licensing is not a major concern, so the project is **GPL-3.0-or-later**, as the
dependency requires. The backend sits behind a small interface (`iosfb.backend.base`), so
a permissive backend can replace it later.

Prior art:

- `pymobiledevice3 afc webdav` and `apps webdav` serve one AFC root or one app over WebDAV.
- iDescriptor (AGPL-3.0) mounts via WinFsp, which is a driver.

This project adds one tree for all devices and all file-sharing apps, read-only by
default, with caching, replug handling and a one-command mount.

## Share protocol

| | Drive letter | Setup without admin | Large files | Outlook |
|---|---|---|---|---|
| FTP | No. Explorer gives a shell folder only | Yes | Poor | Rejected |
| **WebDAV** (chosen) | Yes | Yes | Windows downloads the whole file to a cache before an app can read it. 4 GB per-file cap; 50 MB by default until `FileSizeLimitInBytes` is raised (admin) | WebClient deprecated in Nov 2023, still shipped, no removal date |
| SMB on port 445 | Yes | No. Windows owns 445; freeing it means disabling Windows file sharing or adding a loopback adapter | Best | Rejected |
| SMB on a custom port | Yes, via `net use /TCPPORT` or `New-SmbMapping -TcpPort` only | Disputed (Microsoft Learn says admin; Project Zero says not) | Best | Windows 11 24H2+ only. Needs SMB2+, NTLMv2 and signing. No public report of it working against a user-mode server on localhost |

**Decision: WebDAV first**, served by WsgiDAV + cheroot, anonymous, bound to 127.0.0.1.
Anonymous is used because Windows refuses Basic auth over plain HTTP by default
(`BasicAuthLevel=1`), and the socket is loopback-only.

The virtual tree (`iosfb.vfs`) is protocol-neutral so an SMB front end can be added. The
candidate is impacket's `smbserver` (SMB 3.1.1 and signing landed in 2026) on a custom
port. That is a later spike, not a commitment.

## Other decisions

- Serve all connected devices, one top-level folder each.
- Read-only by default. Writes are an opt-in stretch goal.
- Tests run without a device, against a mock backend. Real-device checks are in
  `docs/MANUAL_TEST.md`.

## Sources

- pymobiledevice3: https://github.com/doronz88/pymobiledevice3 (docs: `docs/installation.md`, `docs/guides/python-api.md`, `docs/guides/webdav-mounting.md`; issues 1351 and 1373 on `VendContainer`)
- idevice: https://github.com/jkcoxson/idevice
- go-ios: https://github.com/danielpaulus/go-ios
- libimobiledevice / ifuse: https://github.com/libimobiledevice/ifuse
- iDescriptor: https://github.com/iDescriptor/iDescriptor
- iOS 8.3 sandbox restriction: https://www.macrumors.com/2015/04/13/ios-8-3-ifunbox-itools-sandbox-app-access
- SMB ports on Windows: https://learn.microsoft.com/en-us/windows-server/storage/file-server/smb-ports
- SMB alternative ports and localhost: https://projectzero.google/2025/01/windows-exploitation-tricks-trapping.html
- SMB signing and guest changes in 24H2: https://techcommunity.microsoft.com/blog/filecab/accessing-a-third-party-nas-with-smb-in-windows-11-24h2-may-fail/4154300
- Port 445 workarounds: https://github.com/TalAloni/SMBLibrary/blob/master/ServerNotes.md
- impacket SMB server signing and SMB 3.1.1: https://github.com/fortra/impacket/pull/1975, https://github.com/fortra/impacket/pull/2216
- WebClient deprecation: https://learn.microsoft.com/en-us/windows/whats-new/deprecated-features
- WebClient start behaviour: https://specterops.io/blog/2025/08/19/will-webclient-start/
- WebDAV limits on Windows: https://docs.cryptomator.org/desktop/volume-type/, https://support.microsoft.com/kb/900900, https://rclone.org/commands/rclone_serve_webdav/
- WsgiDAV: https://wsgidav.readthedocs.io

## Findings from the first real-device runs (2026-11-22)

- AFC and house_arrest work on iOS 27.2 over plain usbmux, including offset reads
  (`FILE_READ_OFFSET`). Throughput is about 26 MB/s direct and 16 MB/s through the
  mounted drive.
- `AfcService.stat` converts timestamps with `datetime.fromtimestamp`, which raises
  `OSError(22)` on Windows for dates it cannot represent. One such file failed the whole
  folder, so the backend reads the raw `GET_FILE_INFO` reply (a private call,
  `AfcService._do_operation`) and converts dates leniently. Revisit if upstream changes.
- System apps can report `UIFileSharingEnabled` yet refuse `VendDocuments` (Fitness does),
  so only user apps are listed.
- Only connection-level errors drop a device session. Treating every `OSError` as a
  disconnect closed open file handles on unrelated requests.
- PowerShell 7 mislists WebDAV drives on Windows build 26300 (names gain a trailing NUL).
  A stock WsgiDAV server shows the same, so it is a Windows/.NET issue. Explorer, `cmd`
  and Windows PowerShell 5.1 are fine.
- Windows shows errors for WebDAV folders without a modification date, so virtual folders
  carry the server start time.

## Design points settled by the independent review (2026-11-22)

- Every path component is resolved through its parent folder's (cached) listing. This
  costs a listing per ancestor on a cold path, but it keeps listing, stat and open in
  agreement when names collide after sanitising, makes lookups case-insensitive like
  Windows, and means a name the device did not list is never sent to it (no `..` tricks).
- The device backend drops a connection by object identity, not by UDID, and allows one
  connection attempt per device at a time. Requests arrive on eight threads, so without
  this a burst opened several connections and a late error on an old connection closed
  the new one.
- A device operation that takes longer than 60 seconds drops the connection.
- PROPFIND with `Depth: infinity` (or no Depth header) is served as depth 1.

## Write support (added 2026-11-23)

- Off by default; `iosfb serve --writable` turns it on. The flag lives on the tree
  (`Vfs(writable=True)`), which refuses every change itself, so a second front end
  cannot forget the check.
- Uploads are written to `.iosfb-part-<random>` beside the destination and renamed into
  place when complete (AFC rename replaces an existing file). A failed or aborted upload
  removes the temporary file and leaves the old content. The temporary files are not
  hidden from listings, so a leftover is visible and can be deleted.
- Moves inside one root (the media area, or one app) are an AFC rename. Across roots or
  devices there is no rename, so the tree copies to a temporary name, renames it into
  place, then deletes the source.
- A move or copy never replaces an existing folder, and replaces a file only when the
  client asks for it (`Overwrite: T`).
- The device, `Apps`, `Media` and app folders are fixed: no create, rename or delete.
  The backend also refuses any change to the top of a root.
- New names are used on the device as given. Control characters, `/`, names over 255
  bytes and the `.iosfb-part-` prefix are refused.
- Windows sends the original file date in a PROPPATCH after each upload. WsgiDAV's
  `emulate_win32_lastmod` hotfix passes it on, and it is set with AFC `SET_FILE_TIME`.
- A refusal by the device (no space, protected folder) is HTTP 403, not 503.
- Requests with an `Origin` header may only read. Together with the Host check this
  keeps web pages in a local browser from changing files.
- Verified on an iPad (iOS 27.2) in `Media/Downloads` and an app folder, over HTTP and
  through the mapped drive, and on an iPhone 15 Pro (iOS 27.2.1) in `Media/Downloads`
  over HTTP. The iPhone dropped off USB before its app-folder run finished.
- Not handled: nothing stops writes into iOS-managed media folders (`DCIM`,
  `PhotoData`, `iTunes_Control`); the README warns about them instead.

## Not done yet

- SMB front end on a custom port (`net use /TCPPORT`), which would remove the WebDAV
  size and whole-file-download limits. Unproven; needs a spike with impacket.
- Cleaning up the drive mapping when the console window is closed (Ctrl+C and Ctrl+Break
  are handled; `iosfb unmount Z:` removes a stale mapping).
- Real-device checks still open: the write checks not yet run by hand, first-time Trust prompt, a file over 4 GB, a locked
  device, and Ctrl+C cleanup. See `docs/MANUAL_TEST.md`.

## Devices over Wi-Fi (added 2026-11-23)

- Devices that Apple's service lists as `Network` are served too; USB is preferred when
  both are listed. `--usb-only` restores the old behaviour.
- Pairing is attempted over USB only; a Wi-Fi device must already be trusted.
- A connection is not moved from Wi-Fi to USB when a cable is plugged in later; it
  switches the next time the connection is re-made.
- Not yet verified on a real device over Wi-Fi: the listing logic is unit-tested only.
