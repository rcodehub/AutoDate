# Changelog

Newest first. The latest entries are copied into the Unraid template (shown in Community Applications) each time an image is published.

### 2026-11-06
- New AutoDate icon in Community Applications and the browser tab.
- Opening an archive's file list no longer makes the page jump; it stays at the archive you opened.
- First release of AutoDate.
- Reads the original dates of the files inside zip, rar, 7z and tar archives (including multi-part sets) without extracting anything.
- Archives tab with the archive date, oldest and newest content dates and age, with sorting, search, type and status filters, and highlighting of outdated archives (contents older than N years).
- Click a row to see every file inside with its date. Archives with encrypted headers or damage are marked instead of hanging the scan.
- Rename archives with the year of their contents (oldest or newest file), across all volumes of a multi-part set, after a preview.
- Delete whole archive sets to the Recycle Bin, a move folder or permanently, after a preview.
- Content dates are exact, including summer time (daylight saving) in the container's time zone, for zip, rar (also multi-volume sets) and 7z.
- Confirming a rename or delete only does what the preview showed; if Settings or names changed in between, the archive is refused and you preview again.
- An index remembers what was read, so unchanged archives are not read again.
- Status page with live progress, per-archive Cancel, a log and a **Scan folder...** button for one-off scans. Settings, History, Stats, themes and a login page.
