# AutoDate

A Docker container for Unraid (or any Docker host) that scans a folder on a schedule and **reads the original dates of the files and folders stored inside each archive - without extracting anything**. It finds archives that look new but hold old files (for example an archive created in 2026 whose contents are 10 years old), and then lets you:

- sort and filter the archives by archive date, oldest / newest content date and age,
- rename the selected archives with the year of their contents: `name.zip` → `name (2016).zip`,
- delete the selected archives to the share's Recycle Bin, to a trash folder, or permanently.

Supported: `zip`, `rar`, `7z` and `tar` (incl. `.tar.gz/.tgz/.tar.bz2/.tar.xz`). Multi-part sets are handled as one archive and renamed or deleted together: `name.part1.rar`…, `name.rar` + `.r00`…, `name.zip` + `.z01`…, `name.7z.001`…. Archives inside archives are listed as entries but never opened.

Nothing is ever renamed or deleted unless you select archives and confirm a preview that shows exactly what will happen. Everything is set up in the web UI, which has a live status page, the archives table, a settings page, history, statistics and themes.

> **Created entirely by AI (Claude).** It has been tested, but not in every situation. Check the preview before you confirm a rename or delete, keep backups of data you can't replace, and do your own due diligence before exposing the web UI to the internet.

## Install (Unraid)

Install **AutoDate** from the **Apps** tab, or download the template by hand:

```bash
wget -O /boot/config/plugins/dockerMan/templates-user/my-AutoDate.xml \
  https://raw.githubusercontent.com/rcodehub/Unraid-CA-Apps/main/templates/autodate.xml
```

then **Docker → Add Container → Template: my-AutoDate**.

> Keep only `my-AutoDate.xml` in `templates-user`. If you also have another copy, such as `autodate.xml`, Unraid may load it when you edit or update the container, and your paths go back to the defaults.

## Template settings

| Setting | Container path / variable | Description |
|---|---|---|
| Web UI port | `8080` (host default `8846`) | Web interface |
| App data | `/data` | Settings, archive index, history, statistics, logs. e.g. `/mnt/user/appdata/autodate` |
| Folder to scan | `/downloads` | The share to scan, read/write. e.g. `/mnt/user/downloads` |
| Trash folder (optional) | `/trash` | If set, deleted archives are **moved** here. Must be a separate folder, not inside the scan folder or a parent of it. Use the same kind of path as the scan folder (both `/mnt/user/…` or both `/mnt/diskN/…`) |
| All shares (optional, advanced) | `/mnt/user` | Lets **Scan folder…** and the folder pickers reach any share, and finds the Recycle Bin when the scan folder is a folder inside a share. Gives the container access to all shares - leave empty unless needed |
| Timezone | `TZ` | For times in the logs |
| UI username | `UI_USERNAME` | Login name, default `admin` |
| UI password | `UI_PASSWORD` | Turns on the login page. Leave blank for no login (then anyone who can reach the port can use the UI) |

Only change the *Host Path* side of the paths. On first start `/downloads` is set as the folder to scan. Everything else is configured in the web UI and saved in `/data`.

## Web UI

- **Status** – next/last scan, progress, what each thread is reading (with a **Cancel** button per archive; scheduled scans skip it until it changes, Run now retries it), failed archives, folders waiting because their archives changed too recently and a live log. Buttons: **Run now**, **Scan folder…** (a one-off scan of any folder you pick, with or without sub-folders), **Stop**.
- **Archives** – every scanned archive with the archive date, oldest and newest content date and how much older the contents are. Search, an **Outdated only** filter, type and status filters, sortable columns, and a click on a row shows the entries inside with their dates. Select archives to **Rename with year…**, **Delete…** or **Rescan**. **Clear results…** empties the list (click twice to confirm) so the next scan reads every archive again; no archive is touched.
- **Settings** – all options below, with a folder picker and live checks that show each folder's real location on the server. Folder pickers only show the folders mapped into the container.
- **History** – every scan and every rename / delete with counts.
- **Stats** – lifetime totals (archives and entries read, outdated found, renamed, deleted, space freed and where it went), oldest content year seen, a 30-day chart and archive types.
- **Theme** – 11 colour themes with light and dark versions, plus corner rounding, spacing, font, shadows and page width. Use ☾/☀ to switch light/dark for your browser.

## Settings (web UI)

| Setting | Default | Description |
|---|---|---|
| Source folders | `/downloads` | Folders to scan, including sub-folders. Use container paths (`/downloads`, not `/mnt/user/…`) |
| Scan automatically / interval | off / 60 min | Interval counts from the end of the previous scan |
| Also scan at container start | off | |
| Threads | 4 | Archives read at the same time. 2–4 suits spinning disks |
| Skip archives newer than | 5 min | Leaves archives that changed recently for a later scan, so downloads still in progress aren't read. 0 = off |
| Folders to skip | `@eaDir`, `.Recycle.Bin`, `#recycle`, `.Trash*` | Folder names to ignore. `.Recycle.Bin` and `.autodate-*` are always skipped |
| Include folder dates | off | Folder dates also count towards the oldest / newest content date (files only by default) |
| Clear results before each scan | off | Every scan starts with an empty list and reads every archive again instead of only new or changed ones (slower). **Scan folder…** clears only that folder |
| Outdated if contents are older than | 5 years | Used for the highlighting and the "Outdated only" filter |
| Outdated means | newest file | The newest file is older than that, or the oldest file is |
| Year used for renaming | newest file | Default in the rename dialog: the newest or the oldest file date gives the year |
| Deleted archives go to | see below | Recycle Bin, move folder or deleted permanently |
| Move folder | `/trash` if mapped | Keeps the folder layout: `<move folder>/<source name>/<path>`; name clashes get `.1`, `.2` |

On a fresh install deleted archives are set to go to the **move folder** if `/trash` is mapped, otherwise to the share's **Recycle Bin** if its root can be reached, otherwise they are **deleted permanently** - which is acceptable because nothing is deleted unless you select archives and confirm a preview that says "permanently".

**Recycle Bin:** files go to `<share>/.Recycle.Bin/<same folders>`, the folder the Unraid **Recycle Bin** plugin uses for files deleted over SMB, so you can browse and empty them there. The share's root must be reachable inside the container: map the share itself (`/mnt/user/downloads` → `/downloads`), or also map **All shares** if you map a folder inside a share. Settings shows where each source's Recycle Bin is.

## How it works

Each source folder is walked once. For every archive (or multi-part set) AutoDate asks `unrar`, 7-Zip or `tar` for the **list** of entries with their stored dates; nothing is extracted and nothing is written to the scanned folders. The result is kept in `/data/index.json`, so unchanged archives are not read again on later scans, and archives that disappeared are dropped. An archive that cannot be read (damaged, missing part) is listed with the reason and left alone by scheduled scans until it changes; Run now tries it again.

Scans and actions never overlap: while a delete, rename or rescan is running, **Run now** and scheduled scans wait (Run now says to try again in a moment), and while a scan is running the rename / delete preview asks you to wait.

Dates are shown as stored in the archive (no time zone conversion). Entries without a usable date (for example the 1980-01-01 default of some zip tools) are shown as "unknown" and ignored for oldest / newest. Dates in the future are flagged. Archives whose headers are encrypted cannot be read and are flagged as encrypted.

**Renaming** appends the content year: `name (YYYY).ext`, with every volume of a set renamed consistently so it still opens. An existing ` (YYYY)` at the end of the name is replaced. An archive is refused when it has no usable dates, when a target name already exists, or when the name would not change. A set is renamed all-or-nothing.

## Versions

The build number (top left of the web UI, and in the container log at start) goes up with every published image. Hover it to see the commit and build time. What changed is in [CHANGELOG.md](CHANGELOG.md), and the newest entries are also shown in Community Applications. Images are tagged `latest` and `build-N`, so you can pin a specific build by setting the repository to `ghcr.io/rcodehub/autodate:build-N`.

## Docker Compose

See [`docker-compose.yml`](docker-compose.yml).

## License

MIT – see [LICENSE](LICENSE).
