"""AutoDate engine.

AutoDate never extracts anything: it reads the original dates of the files stored inside each archive
(see the spec) and keeps them in an index so the user can sort, filter, rename or delete archives.

Reads the dates of the files inside archives (unrar / 7z / tar, never extracting), keeps them in an index
(/data/index.json) and runs the user's rename / delete / rescan actions. Dates are naive local-time strings
"YYYY-MM-DD HH:MM:SS" (None = unknown).
"""
import contextlib
import datetime
import fnmatch
import json
import logging
import logging.handlers
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import mounts
from stats import Stats, archive_type

ARCHIVE_SUFFIXES = (
    ".zip", ".rar", ".7z", ".tar", ".tar.gz", ".tgz",
    ".tar.bz2", ".tbz2", ".tar.xz", ".txz",
)
PART_RE = re.compile(r"\.part0*(\d+)\.rar$", re.I)
NUMVOL_RE = re.compile(r"^(.*\.(?:7z|zip))\.(\d{3})$", re.I)   # name.7z.001, name.zip.001
WORK_PREFIX = ".autodate-"         # private work folders (never scanned, never shown by the folder picker)
FIRST_PART_RE = re.compile(r"^(.*)\.part0*1\.rar$", re.I)
RAR_RE = re.compile(r"^(.*)\.rar$", re.I)

FUTURE_SLACK = 120                 # seconds a file date may be ahead of the clock before it is not trusted
MAX_REMEMBERED = 5000              # entries kept in each "remembered archives" file
MAX_ENTRIES = 20000                # entries kept per archive in the index (the rest are only counted: "truncated")
DIR_MODE, FILE_MODE = 0o777, 0o666   # best effort on folders / files the app creates or moves (shares are used over SMB)
ACTION_MAX_PATHS = 5000            # most archive paths one action request may name


class Halted(Exception):
    """Raised by list_archive() when the whole run was stopped or this one archive was cancelled."""


def which_tools():
    return {
        "unzip": shutil.which("unzip"),
        "unrar": shutil.which("unrar"),
        "7z": shutil.which("7zz") or shutil.which("7z") or shutil.which("7za"),
        "tar": shutil.which("tar"),
    }


def is_archive(name):
    if name.startswith(WORK_PREFIX):
        return False
    if NUMVOL_RE.match(name):
        return True
    return name.lower().endswith(ARCHIVE_SUFFIXES)


def is_secondary_volume(name):
    """name.part2.rar, name.part03.rar, name.7z.002 ... (only the first volume is listed)."""
    m = PART_RE.search(name)
    if m:
        return int(m.group(1)) != 1
    m = NUMVOL_RE.match(name)
    return bool(m) and int(m.group(2)) != 1


def _human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def blank_record(path, volumes, signature):
    """The index record of one archive (set) as the spec defines it, before any dates are known.
    list_archive() fills the dates, counts, flags and entries."""
    sizes, newest = 0, 0
    for f in volumes:
        try:
            st = os.stat(f)
            sizes, newest = sizes + st.st_size, max(newest, st.st_mtime)
        except OSError:
            pass
    return {
        "path": path,                      # first volume
        "volumes": list(volumes),          # all volume paths (the first one included)
        "type": archive_type(path),        # zip | rar | 7z | tar
        "archive_date": epoch_to_str(newest),   # newest mtime of the set, local time "YYYY-MM-DD HH:MM:SS"
        "size": sizes,                     # sum of the volume sizes
        "sig": list(signature),            # [size, int(newest mtime)] of the set when it was listed
        "scanned_at": time.time(),
        "status": "ok",                    # ok | failed (listing failed: damaged, missing volume, ...)
        "error": "",
        "entry_count": 0, "file_count": 0, "folder_count": 0,
        "oldest": None, "newest": None,                # content dates over FILES ("YYYY-MM-DD HH:MM:SS", naive local time as stored)
        "oldest_any": None, "newest_any": None,        # over files + folders
        "age_years": None, "age_years_oldest": None,   # archive date - newest / oldest file date, in years
        "encrypted": False,                # headers encrypted: nothing can be read
        "partial": False,                  # some entries had no date
        "future": False,                   # some date is later than now + 1 day
        "truncated": False,                # more than MAX_ENTRIES entries; the rest were only counted
        "folders_in": False,               # oldest / newest include folder dates (setting include_folders when listed)
        "entries": [],                     # [{"name", "folder": bool, "size", "date": "YYYY-MM-DD HH:MM:SS"|None}]
    }


# ---------------------------------------------------------------- dates
# Every date in a record / entry is a naive local-time string "YYYY-MM-DD HH:MM:SS" (as stored in the archive, no
# timezone conversion; the archive's own date is its mtime in the container's local time) or None when unknown.
DATE_FMT = "%Y-%m-%d %H:%M:%S"
YEAR_SECONDS = 365.25 * 86400
DATE_RE = re.compile(r"^(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d):(\d\d)")
LIST_TIMEOUT = 900                 # seconds one rar / 7z listing may take before it is killed
TAR_TIMEOUT = 7200                 # tar has to read (decompress) the whole file
NAME_MAX = 255
YEAR_TAIL_RE = re.compile(r" \((\d{4})\)$")


def parse_date(text):
    """A date as printed by unrar / 7z / tar -> "YYYY-MM-DD HH:MM:SS", or None when missing or not usable
    (empty, unparseable, before 1970-01-02, or the 1980-01-01 00:00:00 default of zip / DOS)."""
    m = DATE_RE.match((text or "").strip())
    if not m:
        return None
    try:
        y, mo, d, h, mi, se = (int(x) for x in m.groups())
        dt = datetime.datetime(y, mo, d, h, mi, se)
    except ValueError:
        return None
    if dt < datetime.datetime(1970, 1, 2) or dt == datetime.datetime(1980, 1, 1):
        return None
    return dt.strftime(DATE_FMT)


def to_epoch(v):
    """A record date (string, or an epoch number) -> epoch seconds, or None."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    try:
        return datetime.datetime.strptime(str(v)[:19], DATE_FMT).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def epoch_to_str(t):
    try:
        return time.strftime(DATE_FMT, time.localtime(t)) if t else None
    except (OverflowError, OSError, ValueError):
        return None


def _years(later, earlier):
    a, b = to_epoch(later), to_epoch(earlier)
    return None if a is None or b is None else round((a - b) / YEAR_SECONDS, 2)


class _Acc:
    """Collects entries of one listing: counts, date ranges, the (capped) entry list."""
    def __init__(self, cap=None):
        self.cap = MAX_ENTRIES if cap is None else cap
        self.entries, self.n, self.files, self.folders = [], 0, 0, 0
        self.unknown, self.future, self.truncated = 0, False, False
        self.f_old = self.f_new = self.a_old = self.a_new = None
        self.limit = datetime.datetime.fromtimestamp(time.time() + 86400).strftime(DATE_FMT)

    def add(self, name, folder, size, date):
        self.n += 1
        if folder:
            self.folders += 1
        else:
            self.files += 1
        if date is None:
            self.unknown += 1
        else:
            if date > self.limit:
                self.future = True
            self.a_old = date if self.a_old is None or date < self.a_old else self.a_old
            self.a_new = date if self.a_new is None or date > self.a_new else self.a_new
            if not folder:
                self.f_old = date if self.f_old is None or date < self.f_old else self.f_old
                self.f_new = date if self.f_new is None or date > self.f_new else self.f_new
        if len(self.entries) < self.cap:
            self.entries.append({"name": name, "folder": bool(folder), "size": int(size or 0), "date": date})
        else:
            self.truncated = True


def _int(v):
    try:
        return int(str(v).strip().split()[0])
    except (ValueError, IndexError):
        return 0


class UnrarParser:
    """`unrar lt` output: blocks starting with a Name: line."""
    KV = re.compile(r"^\s+([A-Za-z0-9][A-Za-z0-9 \-]*?):\s?(.*)$")

    def __init__(self, add):
        self.add, self.cur, self.msgs = add, None, []
        self.enc_headers, self.dangling = False, False

    def _flush(self):
        c, self.cur = self.cur, None
        if not c:
            return
        t = c.get("type", "file").lower()
        if t.startswith("service"):
            return
        self.dangling = c.get("ratio", "").strip().endswith("-->")
        self.add(c["name"], t.startswith("dir"), _int(c.get("size", 0)), parse_date(c.get("mtime", "")))

    def feed(self, line):
        if line.startswith("        Name: ") or line.startswith("Name: "):
            self._flush()
            self.cur = {"name": line.split("Name: ", 1)[1]}
            return
        if not line.strip():
            self._flush()
            return
        m = self.KV.match(line)
        if m:
            k, v = m.group(1).lower(), m.group(2)
            if self.cur is not None:
                if k in ("type", "size", "mtime", "ratio"):
                    self.cur[k] = v
                return
            if k == "details" and "encrypted headers" in v.lower():
                self.enc_headers = True
            if k == "archive":
                return
        self.msgs.append(line.strip())

    def close(self):
        self._flush()


class SevenZipParser:
    """`7z l -slt -ba` output: blank-line separated Key = Value blocks starting with Path."""
    KV = re.compile(r"^([A-Za-z][A-Za-z ]*?) =(?: (.*))?$")

    def __init__(self, add):
        self.add, self.cur, self.msgs = add, None, []

    def _flush(self):
        c, self.cur = self.cur, None
        if not c or "path" not in c or c.get("alternate stream", "-") == "+":
            return
        attr = c.get("attributes", "").lstrip()
        folder = c.get("folder", "") == "+" or attr.startswith("D")
        self.add(c["path"], folder, _int(c.get("size") or 0), parse_date(c.get("modified", "")))

    def feed(self, line):
        if line.startswith(("ERROR:", "WARNING:", "System ERROR")):
            self.msgs.append(line.strip())
            return
        if not line.strip():
            self._flush()
            return
        m = self.KV.match(line)
        if m:
            k, v = m.group(1).lower(), m.group(2) or ""
            if k == "path":
                self._flush()
                self.cur = {}
            if self.cur is not None:
                self.cur[k] = v
            return
        self.msgs.append(line.strip())

    def close(self):
        self._flush()


class TarParser:
    """`tar -tv --full-time` output."""
    LINE = re.compile(r"^([-dlhbcpsCDMV])\S{9}[+.@]?\s+(.+?)\s+(\d+(?:,\s*\d+)?)\s+"
                      r"(\d{4,}-\d\d-\d\d \d\d:\d\d:\d\d)(?:\.\d+)? (.*)$")

    def __init__(self, add):
        self.add, self.msgs = add, []

    def feed(self, line):
        m = self.LINE.match(line)
        if not m:
            if line.strip():
                self.msgs.append(line.strip())
            return
        kind, _owner, size, date, name = m.groups()
        if kind == "l" and " -> " in name:
            name = name.split(" -> ", 1)[0]
        elif kind == "h" and " link to " in name:
            name = name.split(" link to ", 1)[0]
        folder = kind == "d" or name.endswith("/")
        self.add(name.rstrip("/") if folder else name, folder, _int(size.split(",")[0]) if kind not in "bc" else 0,
                 parse_date(date))

    def close(self):
        pass


def rename_base(first_name):
    """The part of the first volume's file name before the archive style tail ('x' for x.part1.rar, x.7z.001,
    x.tar.gz ...), or None when the name is not an archive name."""
    for rx in (FIRST_PART_RE,):
        m = rx.match(first_name)
        if m:
            return m.group(1)
    m = NUMVOL_RE.match(first_name)
    if m:
        return re.sub(r"\.(7z|zip)$", "", m.group(1), flags=re.I)
    low = first_name.lower()
    for suf in sorted(ARCHIVE_SUFFIXES, key=len, reverse=True):
        if low.endswith(suf):
            return first_name[:-len(suf)]
    return None


def new_base(base, year):
    """x -> 'x (2016)'; 'x (2010)' -> 'x (2016)'."""
    m = YEAR_TAIL_RE.search(base)
    return (base[:m.start()] if m else base) + f" ({year})"


class Engine:
    def __init__(self, log_dir, history_path):
        self.lock = threading.RLock()
        self.move_lock = threading.Lock()
        self.cancel_file_lock = threading.Lock()
        self.index_lock = threading.RLock()
        self.index_save_lock = threading.Lock()   # one index writer at a time (the index lock is not held while writing)
        self._index_dirty = False
        self._last_autosave = time.time()
        self._action_lock = threading.Lock()      # real rename / delete actions run one at a time
        self._acting = 0                          # actions running now (a scan cannot start meanwhile)
        self._rescanning = False
        self._walk_errors = set()
        self.stop_event = threading.Event()
        self.procs = set()
        self.thread = None
        self.history_path = history_path
        data_dir = os.path.dirname(history_path) or "."
        self.data_dir = data_dir
        # the index: {path: record} of every archive listed (spec: /data/index.json)
        self.index_path = os.path.join(data_dir, "index.json")
        self._index = None           # loaded lazily by index_load()
        # archives the user cancelled: scheduled scans leave them alone until they change (Run now retries)
        self.cancelled_path = os.path.join(data_dir, "cancelled-archives.json")
        self._cancelled_mem = {}     # the file is read once per run and kept here
        self.skipped = 0             # archives left alone because they failed / were cancelled earlier and have not changed
        self.unchanged = 0           # archives not listed again because the index is up to date
        self.tools = which_tools()

        self.log_buf = deque(maxlen=2000)
        self.log_seq = 0
        self.log_cleared = 0
        self._file_log = None
        try:
            os.makedirs(log_dir, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                os.path.join(log_dir, "autodate.log"), maxBytes=2_000_000, backupCount=3)
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            self._file_log = logging.getLogger(f"autodate.{id(self)}")
            self._file_log.propagate = False
            self._file_log.setLevel(logging.INFO)
            self._file_log.addHandler(handler)
        except OSError:
            self._file_log = None

        self.history = self._load_history()
        self.stats = Stats(os.path.join(data_dir, "stats.json"))
        self.status = self._blank_status()
        self._warned = set()
        self.seen = set()            # archives counted so far (for found/remaining)
        self.failures = []
        self.failed_paths = set()
        self.deferred = set()        # archives skipped because they are too recent
        self.deferred_ready = {}     # archive -> time it is old enough to be listed
        self._mounts = []
        self.cancelled = 0
        self.cancelled_paths = set()
        self._slot_procs = {}        # slot -> running tool processes (so one archive can be cancelled alone)
        self._skip_logged = set()
        self._slots = []
        self._refresh_mounts()

    def _refresh_mounts(self):
        self._mounts = mounts.list_mounts(self.data_dir)

    # ------------------------------------------------------------ logging
    def log(self, level, msg):
        with self.lock:
            self.log_seq += 1
            self.log_buf.append({"id": self.log_seq, "t": time.time(), "level": level, "msg": msg})
        if self._file_log:
            getattr(self._file_log, "warning" if level == "warn" else
                    "error" if level == "error" else "info")(msg)

    def clear_log(self):
        """Empty the on-screen log (the log file in /data/logs is kept)."""
        with self.lock:
            self.log_buf.clear()
            self.log_cleared = self.log_seq

    def log_since(self, after=0, limit=400):
        with self.lock:
            items = [e for e in self.log_buf if e["id"] > after]
        return items[-limit:]

    def _warn_once(self, key, msg):
        if key not in self._warned:
            self._warned.add(key)
            self.log("warn", msg)

    # ------------------------------------------------------------ history
    # Entries: scans {"type": "scan", ...} (written by _finish) and actions {"type": "delete"|"rename", ...}
    # (written by add_action_history, called by the delete / rename actions).
    def _load_history(self):
        try:
            with open(self.history_path) as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save_history(self):
        try:
            tmp = self.history_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.history[-100:], f)
            os.replace(tmp, self.history_path)
        except OSError as e:
            self.log("warn", f"Could not save history: {e}")

    def add_action_history(self, kind, results, size=0, started_at=None):
        """Record a rename / delete action: kind = "rename" | "delete"; results = the per-archive result dicts
        of the API (each has "ok"); size = bytes of the archives acted on successfully."""
        done = sum(1 for r in results if r.get("ok"))
        entry = {"type": kind, "started_at": started_at or time.time(), "finished_at": time.time(),
                 "count": done, "failed": len(results) - done, "bytes": size, "results": results[:200]}
        with self.lock:
            self.history.append(entry)
            self._save_history()
        return entry

    # ------------------------------------------------------------ status
    def _blank_status(self):
        return {
            "state": "idle", "trigger": None,
            "started_at": None, "finished_at": None, "result": None, "message": "",
            "current_dir": "",
            "listed": 0, "entries": 0, "outdated": 0, "slots": [],
        }

    @property
    def running(self):
        t = self.thread
        return bool((t and t.is_alive()) or self._rescanning)

    def snapshot(self):
        with self.lock:
            s = dict(self.status)
            now = time.time()
            s["slots"] = [
                ({"n": i + 1, "path": sl["path"], "elapsed": int(now - sl["t"]), "phase": sl.get("phase", "listing"),
                  "cancel": sl.get("cancel", False)} if sl
                 else {"n": i + 1, "path": None, "elapsed": 0})
                for i, sl in enumerate(self._slots)
            ] if self.status["state"] != "idle" else []
            failed = self.failures
            s["found"] = len(self.seen)
            s["failed"] = len(failed)
            s["cancelled"] = self.cancelled
            s["skipped"] = self.skipped
            s["unchanged"] = self.unchanged
            s["done"] = s["listed"] + s["failed"] + self.cancelled + self.skipped + self.unchanged
            s["remaining"] = max(0, s["found"] - s["done"])
            s["deferred"] = len(self.deferred)
            s["waiting"] = self._waiting()
            s["failures"] = failed[-50:]
            s["tools"] = self.tools
            return s

    def _waiting(self):
        """Folders with archives skipped as too recent: [{dir, count, names, ready_at}], soonest first."""
        groups = {}
        for p in self.deferred:
            g = groups.setdefault(os.path.dirname(p), {"dir": os.path.dirname(p), "count": 0, "names": [], "ready_at": None})
            g["count"] += 1
            g["names"].append(os.path.basename(p))
            r = self.deferred_ready.get(p)
            if r and (g["ready_at"] is None or r > g["ready_at"]):
                g["ready_at"] = r            # the whole folder is ready when its newest archive is
        out = sorted(groups.values(), key=lambda g: (g["ready_at"] or 0, g["dir"]))
        for g in out:
            g["names"] = sorted(g["names"])[:5]
        return out[:100]

    # ------------------------------------------------------------ control
    def start(self, cfg, trigger="manual", oneoff=None):
        """oneoff = {"path": folder, "recursive": bool}: scan just that folder now (one-off job)."""
        with self.lock:
            if self.running or self._acting:
                return False
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._run, args=(dict(cfg), trigger, oneoff), daemon=True)
            self._fresh_run_state(trigger, cfg["threads"])
            self.thread.start()
            return True

    _RUN_ATTRS = ("status", "seen", "failures", "failed_paths", "deferred", "deferred_ready", "cancelled", "skipped",
                  "unchanged", "cancelled_paths", "_slot_procs", "_skip_logged", "_slots")

    def _fresh_run_state(self, trigger, threads):
        self.status = self._blank_status()
        self.status.update(state="running", trigger=trigger, started_at=time.time())
        self.seen = set()
        self.failures = []
        self.failed_paths = set()
        self.deferred = set()
        self.deferred_ready = {}
        self.cancelled = 0
        self.skipped = 0
        self.unchanged = 0
        self.cancelled_paths = set()
        self._slot_procs = {}
        self._skip_logged = set()
        self._slots = [None] * threads

    def stop(self):
        with self.lock:
            if not self.running:
                return False
            self.stop_event.set()
            self.status["state"] = "stopping"
            for p in list(self.procs):
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except OSError:
                    pass
        self.log("warn", "Stop requested - finishing up")
        return True

    def cancel_job(self, path):
        """Cancel the listing of one archive while the others carry on. Nothing is changed."""
        with self.lock:
            for i, sl in enumerate(self._slots):
                if sl and sl["path"] == path:
                    sl["cancel"] = True
                    for p in list(self._slot_procs.get(i, ())):
                        try:
                            os.killpg(p.pid, signal.SIGTERM)
                        except OSError:
                            pass
                    break
            else:
                return False, "That archive is not being scanned any more"
        self.log("warn", f"Cancel requested: {path}")
        return True, ""

    # ------------------------------------------------------------ the run
    def _run(self, cfg, trigger, oneoff=None):
        cfg["_skip_dirs"] = set()
        cfg["_trigger"] = trigger
        cfg["_oneoff"] = bool(oneoff)
        result, message = "completed", ""
        self.log("info", f"=== Scan started ({trigger}) ===")
        try:
            self._refresh_mounts()
            self._cancelled_mem = self._load_remembered(self.cancelled_path)
            self.index_load()
            sources = []
            if oneoff:
                target = os.path.realpath(oneoff["path"])
                if not os.path.isdir(target):
                    raise RuntimeError(f"Folder not found: {target}")
                if target == os.sep:
                    raise RuntimeError("The root folder (/) cannot be used here - choose a folder inside one of your shares")
                sources = [target]
                if not oneoff.get("recursive", True):
                    try:
                        cfg["_skip_dirs"] = {e.path for e in os.scandir(target) if e.is_dir(follow_symlinks=False)}
                    except OSError:
                        pass
                self.log("info", f"One-off: scanning {target}" +
                         (" and its sub-folders" if oneoff.get("recursive", True) else " (this folder only)"))
            else:
                for s in cfg["sources"]:
                    s = os.path.realpath(s)
                    if os.path.isdir(s):
                        sources.append(s)
                    else:
                        self.log("warn", f"Source folder not found, skipping: {s}")
            if not sources:
                raise RuntimeError("No valid source folders configured")

            found = []
            self._walk_errors = set()
            for s in sources:
                found += self._prescan(s, cfg)
            self.log("info", f"Found {len(self.seen)} archive(s) to look at"
                             f" ({len(self.deferred)} too recent, skipped for now)")

            with ThreadPoolExecutor(max_workers=cfg["threads"]) as pool:
                futures = []
                for a in found:
                    if self.stop_event.is_set():
                        break
                    with self.lock:
                        self.status["current_dir"] = os.path.dirname(a)
                    verdict = self._decide(a, cfg)
                    if verdict == "list":
                        futures.append(pool.submit(self._work, a, cfg))
                for f in futures:
                    f.result()
            if self.stop_event.is_set():
                result = "stopped"
            else:
                if not oneoff:
                    self._drop_vanished(sources)
            self.index_save()
        except Exception as e:  # noqa: BLE001 - report any failure to the UI
            result, message = "error", str(e)
            self.log("error", f"Scan failed: {e}")
        finally:
            self._finish(trigger, result, message)

    def _finish(self, trigger, result, message):
        with self.lock:
            st = self.status
            st["finished_at"] = time.time()
            st["state"] = "idle"
            st["result"] = result
            st["message"] = message
            st["current_dir"] = ""
            entry = {
                "type": "scan",
                "started_at": st["started_at"], "finished_at": st["finished_at"],
                "trigger": trigger, "result": result, "message": message,
                "found": len(self.seen), "listed": st["listed"], "unchanged": self.unchanged,
                "failed": len(self.failures), "outdated": st["outdated"], "entries": st["entries"],
                "deferred": len(self.deferred),
                "cancelled": self.cancelled,
                "skipped": self.skipped,
                "failures": self.failures[-50:],
            }
            self.history.append(entry)
            self._save_history()
        self.stats.run_finished()
        self.log("info", f"=== Scan {result}: {entry['listed']} listed, {entry['unchanged']} unchanged, "
                         f"{entry['failed']} failed ===")

    # ------------------------------------------------------------ scanning
    def _excluded(self, name, cfg):
        if name.startswith(WORK_PREFIX) or name == mounts.RECYCLE_NAME:
            return True                               # never scan work folders or the Recycle Bin
        low = name.lower()
        return any(fnmatch.fnmatch(low, p.lower()) for p in cfg["exclude_dirs"])

    def _group_mtime(self, path):
        """Newest change of the archive and all its other volumes. A file dated in the future (clock or timezone
        mismatch on the machine that wrote it) would look too new forever, so its change time (when it really
        arrived on this server) is used instead."""
        newest = 0
        now = time.time()
        for f in self._archive_group(path):
            try:
                st = os.stat(f)
            except OSError:
                continue
            t = st.st_mtime
            if t > now + FUTURE_SLACK:
                t = st.st_ctime
                self._warn_once(("future", path), f"{os.path.basename(f)} is dated in the future "
                                f"({time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime))}) - using the time it "
                                f"arrived instead: {path}")
            newest = max(newest, t)
        return newest

    def _eligible(self, path, cfg):
        age = cfg["min_age_minutes"]
        if age <= 0:
            return True
        changed = self._group_mtime(path)
        ok = (time.time() - changed) >= age * 60
        if not ok:
            with self.lock:
                new = path not in self.deferred
                self.deferred.add(path)
                self.deferred_ready[path] = changed + age * 60
            if new:
                mins = max(0, (time.time() - changed) / 60)
                self.log("info", f"Skipped for now (changed {mins:.0f} min ago, could still be downloading; "
                                 f"setting: skip archives newer than {age} min): {path}")
        return ok

    def _register(self, path):
        with self.lock:
            self.seen.add(path)
            self.deferred.discard(path)
            self.deferred_ready.pop(path, None)

    def _prescan(self, root, cfg):
        """Walk `root`; register and return the first volume of every eligible archive (sorted by folder)."""
        out = []
        def onerr(e):
            self._walk_errors.add(root)
            self.log("warn", f"Could not read folder {getattr(e, 'filename', root)}: {e}")
        for dirpath, dirnames, filenames in os.walk(root, onerror=onerr):
            if self.stop_event.is_set():
                break
            dirnames[:] = sorted(d for d in dirnames if not self._excluded(d, cfg)
                                 and not os.path.islink(os.path.join(dirpath, d))
                                 and os.path.join(dirpath, d) not in cfg.get("_skip_dirs", ()))
            for fn in sorted(filenames):
                if is_archive(fn) and not is_secondary_volume(fn):
                    p = os.path.join(dirpath, fn)
                    if os.path.islink(p):
                        continue
                    if self._eligible(p, cfg):
                        self._register(p)
                        out.append(p)
        return out

    def _decide(self, path, cfg):
        """"list" | "unchanged" | "skipped". Unchanged archives are not listed again; an archive remembered as
        failed / cancelled is left alone by scheduled scans until its signature changes (Run now retries)."""
        sig = self._signature(path)
        rec = self.index_get(path)
        sched = cfg.get("_trigger") == "schedule"
        if sched:
            why = self._remembered_why(path)
            if why:
                if path not in self._skip_logged:
                    self._skip_logged.add(path)
                    with self.lock:
                        self.skipped += 1
                    self.log("info", f"Skipped {path}: it {why} earlier and has not changed "
                                     "(Run now or Scan folder... tries it again)")
                return "skipped"
        if rec and rec.get("sig") == sig and rec.get("status") == "ok":
            with self.lock:
                self.unchanged += 1
            return "unchanged"
        return "list"

    # ------------------------------------------------------------ worker
    def _acquire_slot(self, path):
        with self.lock:
            for i, sl in enumerate(self._slots):
                if sl is None:
                    self._slots[i] = {"path": path, "t": time.time(), "phase": "listing", "cancel": False}
                    return i
        return 0

    def _release_slot(self, i):
        with self.lock:
            self._slots[i] = None
            self._slot_procs.pop(i, None)

    def _halted(self, slot):
        """True when the whole run was stopped or this one archive was cancelled."""
        if self.stop_event.is_set():
            return True
        sl = self._slots[slot] if slot is not None and slot < len(self._slots) else None
        return bool(sl and sl.get("cancel"))

    def _work(self, path, cfg):
        """List one archive and store the record in the index. Returns "ok", "cancelled" or "fail"."""
        if self.stop_event.is_set():
            return "fail"
        slot = self._acquire_slot(path)
        try:
            self.log("info", f"[thread {slot + 1}] Reading dates in {path}")
            t0 = time.time()
            try:
                rec = self.list_archive(path, cfg, slot)
            except Halted:
                if self.stop_event.is_set():
                    return "fail"
                with self.lock:
                    self.cancelled += 1
                    self.cancelled_paths.add(path)
                self._remember("cancelled", path)
                self.log("warn", f"Cancelled: {path}. Scheduled scans skip it until it changes; "
                                 "Run now (or Scan folder...) tries it again")
                return "cancelled"
            secs = time.time() - t0
            self.index_put(rec)
            if rec.get("status") == "failed":
                # remembered in the index (status "failed" + signature): scheduled scans skip it until it changes
                self._fail(path, rec.get("error") or "listing failed")
                return "fail"
            self._forget(path)
            outdated = self.is_outdated(rec, cfg)
            with self.lock:
                self.status["listed"] += 1
                self.status["entries"] += rec.get("entry_count", 0)
                self.status["outdated"] += 1 if outdated else 0
            oldest_year = int(rec["oldest"][:4]) if rec.get("oldest") else None
            self._autosave()
            self.stats.scanned(path, rec.get("entry_count", 0), rec.get("size", 0), secs, outdated, oldest_year)
            self.log("info", f"Done: {os.path.basename(path)} - {rec.get('entry_count', 0)} entries in {secs:.1f}s")
            return "ok"
        except Exception as e:  # noqa: BLE001 - one archive must never stop the others
            self._fail(path, f"unexpected error: {e}")
            return "fail"
        finally:
            self._release_slot(slot)

    def _fail(self, path, err):
        with self.lock:
            self.failed_paths.add(path)
            self.failures.append({"path": path, "error": err})
        self.stats.failed()
        self.log("error", f"FAILED: {path} - {err}")

    @staticmethod
    def is_outdated(rec, cfg):
        """Outdated = the newest (or oldest, per settings) file date in the archive is older than N years."""
        suffix = "_any" if cfg.get("include_folders") and "newest_any" in rec else ""
        d = to_epoch(rec.get("newest" + suffix) if cfg.get("outdated_by", "newest") == "newest"
                     else rec.get("oldest" + suffix))
        if d is None:
            return False
        return (time.time() - d) / YEAR_SECONDS > cfg.get("outdated_years", 5)

    # ------------------------------------------------------------ reading dates
    def list_archive(self, path, cfg, slot=None):
        """Read the dates of every entry of the archive set starting at `path` (first volume) WITHOUT extracting,
        and return its index record. A listing that fails (damaged, missing volume, ...) returns a record with
        status "failed" and the error text; Halted is raised when the run was stopped / the archive cancelled."""
        group = self._archive_group(path)
        rec = blank_record(path, group, self._signature(path))
        include = bool(cfg.get("include_folders"))
        if not os.path.isfile(path):
            rec.update(status="failed", error="archive not found")
            return rec
        miss = self._missing_volume(path, group)
        if miss:
            rec.update(status="failed", error=miss)
            return rec
        kind = rec["type"]
        if kind == "rar":
            acc, state, err = self._list_rar(path, slot)
        elif kind in ("zip", "7z"):
            acc, state, err = self._list_7z(path, slot)
        else:
            acc, state, err = self._list_tar(path, slot)
        if self._halted(slot):
            raise Halted()
        rec["entries"] = acc.entries
        rec.update(entry_count=acc.n, file_count=acc.files, folder_count=acc.folders,
                   oldest_any=acc.a_old, newest_any=acc.a_new, truncated=acc.truncated, future=acc.future,
                   partial=acc.unknown > 0 and state == "ok")
        if state == "encrypted":
            rec.update(encrypted=True, error="headers are encrypted - the dates cannot be read without the password")
        elif state == "failed":
            rec.update(status="failed", error=err or "listing failed")
        self._apply_folders(rec, include, (acc.f_old, acc.f_new))
        return rec

    @staticmethod
    def _apply_folders(rec, include, files=None):
        """Set oldest / newest (+ ages) from the files-only or the files+folders range."""
        f_old, f_new = files if files else (rec.get("oldest"), rec.get("newest"))
        if include:
            old, new = rec.get("oldest_any"), rec.get("newest_any")
        else:
            old, new = f_old, f_new
        rec.update(oldest=old, newest=new, folders_in=bool(include),
                   age_years=_years(rec.get("archive_date"), new),
                   age_years_oldest=_years(rec.get("archive_date"), old))

    def _view(self, rec, cfg):
        """The record as the current settings see it (include_folders may have changed since it was listed)."""
        include = bool((cfg or {}).get("include_folders"))
        if bool(rec.get("folders_in")) == include or "oldest_any" not in rec:
            return rec
        v = dict(rec)
        files = self._file_range(rec)
        self._apply_folders(v, include, files)
        return v

    @staticmethod
    def _file_range(rec):
        """Oldest / newest FILE date of a record (from the entries when folders were included when it was listed)."""
        if not rec.get("folders_in"):
            return rec.get("oldest"), rec.get("newest")
        dates = [e["date"] for e in rec.get("entries", ()) if e.get("date") and not e.get("folder")]
        return (min(dates), max(dates)) if dates else (None, None)

    def _missing_volume(self, path, group):
        """Volume sets must be complete: numbered parts without gaps."""
        base = os.path.basename(path)
        nums = []
        m = FIRST_PART_RE.match(base)
        if m:
            nums = sorted(int(x.group(1)) for x in (PART_RE.search(os.path.basename(v)) for v in group) if x)
        elif NUMVOL_RE.match(base):
            nums = sorted(int(NUMVOL_RE.match(os.path.basename(v)).group(2)) for v in group
                          if NUMVOL_RE.match(os.path.basename(v)))
        elif RAR_RE.match(base) and len(group) > 1:
            nums = [0] + sorted(int(re.search(r"\.r(\d\d)$", v, re.I).group(1)) + 1 for v in group[1:])
        elif base.lower().endswith(".zip") and len(group) > 1:
            nums = [0] + sorted(int(re.search(r"\.z(\d+)$", v, re.I).group(1)) for v in group[1:])
            if nums[1:] and nums[1:] != list(range(1, len(nums))):
                return "a volume of this split archive is missing"
            return ""
        if nums:
            first = 0 if (RAR_RE.match(base) and not m and not NUMVOL_RE.match(base)) else 1
            if nums != list(range(first, first + len(nums))):
                return "a volume of this multi-part archive is missing"
        return ""

    def _stream(self, cmd, parser, slot, timeout):
        """Run cmd, feed each output line to the parser. Returns (rc, other output lines)."""
        rc, extra = self._exec(cmd, slot, timeout=timeout, on_line=lambda line: parser.feed(line) or True)
        parser.close()
        return rc, extra

    @staticmethod
    def _first_msg(*lists):
        for lst in lists:
            for m in lst:
                m = re.sub(r"^(Open |System )?(ERROR|WARNING)S?:?\s*", "", m).strip()
                if m and not re.fullmatch(r"(.+) : \1", m):      # 7z repeats the file name on its own line
                    return m[:300]
        return ""

    def _list_rar(self, path, slot):
        """unrar first (it keeps the stored times as they are); 7z when unrar is missing or cannot read it."""
        unrar = self.tools.get("unrar")
        first = None
        if unrar:
            acc = _Acc()
            p = UnrarParser(acc.add)
            rc, extra = self._stream([unrar, "lt", "-p-", "-idc", "--", path], p, slot, LIST_TIMEOUT)
            if self._halted(slot):
                raise Halted()
            low = " ".join(p.msgs + extra).lower()
            if p.enc_headers or ("password" in low and acc.n == 0):
                return acc, "encrypted", ""
            bad = re.search(r"not rar archive|cannot find|cannot open|unexpected end|corrupt|checksum error|"
                            r"crc failed|bad archive|no such file", low)
            if rc is None:
                err = extra[0] if extra else "unrar could not be run"
            elif rc != 0 or bad:
                err = self._first_msg(p.msgs, extra) or f"unrar exit code {rc}"
            elif p.dangling:
                err = "the archive ends in the middle of a file - a volume is missing or the download is incomplete"
            else:
                return acc, "ok", ""
            first = err
        r = self._list_7z(path, slot) if self.tools.get("7z") else (_Acc(), "failed", "")
        if r[1] == "failed" and first:
            return r[0], "failed", first
        return r

    def _list_7z(self, path, slot):
        z = self.tools.get("7z")
        acc = _Acc()
        if not z:
            return acc, "failed", "7z is not installed"
        p = SevenZipParser(acc.add)
        rc, extra = self._stream([z, "l", "-slt", "-ba", "-p", "--", path], p, slot, LIST_TIMEOUT)
        if self._halted(slot):
            raise Halted()
        msgs = p.msgs + extra
        low = " ".join(msgs).lower()
        if acc.n == 0 and ("encrypted" in low or "wrong password" in low or "password" in low):
            return acc, "encrypted", ""
        if rc is None:
            return acc, "failed", (extra[0] if extra else "7z could not be run")
        if rc >= 2 or any(m.startswith(("ERROR", "System ERROR")) for m in msgs):
            return acc, "failed", self._first_msg(msgs) or f"7z exit code {rc}"
        if rc == 1 and re.search(r"unexpected end|headers error|missing volume|can not open", low):
            return acc, "failed", self._first_msg(msgs)
        return acc, "ok", ""

    def _list_tar(self, path, slot):
        acc = _Acc()
        tar = self.tools.get("tar")
        if not tar:
            return acc, "failed", "tar is not installed"
        p = TarParser(acc.add)
        rc, extra = self._stream([tar, "-tv", "--full-time", "-f", path], p, slot, TAR_TIMEOUT)
        if self._halted(slot):
            raise Halted()
        if rc is None:
            return acc, "failed", (extra[0] if extra else "tar could not be run")
        if rc != 0:
            msgs = [m for m in p.msgs + extra if "Child returned" not in m and "not recoverable" not in m]
            return acc, "failed", self._first_msg(msgs) or f"tar exit code {rc}"
        return acc, "ok", ""

    def _exec(self, cmd, slot=None, timeout=None, on_line=None):
        """Run a tool in its own process group (so Stop / Cancel can kill it and anything it spawned) with stdin
        closed. Returns (exit code or None, output). With on_line the output is streamed line by line to it; a
        line it returns True for is consumed, the others are returned (at most 40) as the output. A tool that runs
        longer than `timeout` seconds is killed: (None, "timed out after N s")."""
        env = dict(os.environ, LC_ALL="C.UTF-8")
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, errors="replace",
                                 start_new_session=True, env=env)
        except OSError as e:
            return None, [str(e)] if on_line else str(e)
        with self.lock:
            self.procs.add(p)
            if slot is not None:
                self._slot_procs.setdefault(slot, set()).add(p)
            cancel_now = self._halted(slot)
        timed = []

        def kill(sig=signal.SIGTERM):
            try:
                os.killpg(p.pid, sig)
            except OSError:
                pass

        def on_timeout():
            timed.append(1)
            kill(signal.SIGKILL)

        if cancel_now:                                     # cancelled between the check and the start
            kill()
        timer = None
        if timeout:
            timer = threading.Timer(timeout, on_timeout)
            timer.daemon = True
            timer.start()
        extra = []
        try:
            if on_line:
                try:
                    for line in p.stdout:
                        line = line.rstrip("\n")
                        if not on_line(line) and line.strip() and len(extra) < 40:
                            extra.append(line)
                except Exception:
                    kill(signal.SIGKILL)
                    raise
                p.wait()
                out = extra
            else:
                out, _ = p.communicate()
                out = out or ""
        finally:
            if timer:
                timer.cancel()
            kill(signal.SIGKILL) if p.poll() is None else None
            try:
                p.wait(5)
            except Exception:  # noqa: BLE001
                pass
            with self.lock:
                self.procs.discard(p)
                if slot is not None:
                    self._slot_procs.get(slot, set()).discard(p)
        if timed:
            msg = f"timed out after {int(timeout)} s"
            return None, [msg] if on_line else msg
        return p.returncode, out

    # ------------------------------------------------------------ the index
    # {path (first volume): record}. Records are replaced as a whole, never changed in place, so a snapshot of the
    # dict can be written without holding the lock.
    def index_load(self):
        """Read /data/index.json into memory (once); returns the dict. A missing file starts empty; a damaged one is
        renamed aside (index.json.corrupt-...) and the index starts empty."""
        with self.index_lock:
            if self._index is None:
                self._index = self._read_index()
            return self._index

    def _read_index(self):
        try:
            with open(self.index_path) as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            aside = f"{self.index_path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
            try:
                os.replace(self.index_path, aside)
                where = f"kept as {aside}"
            except OSError:
                where = "could not be moved aside"
            self.log("warn", f"The index file was damaged ({e}); starting with an empty index (the old file {where}). "
                             "Run now lists everything again")
            return {}
        good = {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict)}
        for k, v in good.items():
            v.setdefault("path", k)
        if len(good) != len(data):
            self.log("warn", f"{len(data) - len(good)} damaged record(s) were dropped from the index")
        return good

    def index_save(self, force=False):
        """Write the index atomically (temp file + fsync + replace), one writer at a time."""
        with self.index_save_lock:
            with self.index_lock:
                if not (self._index_dirty or force) and os.path.exists(self.index_path):
                    return
                snap = dict(self.index_load())
                self._index_dirty = False
            tmp = self.index_path + ".tmp"
            try:
                with open(tmp, "w") as f:
                    json.dump(snap, f, separators=(",", ":"))
                    f.flush()
                    os.fsync(f.fileno())
                try:
                    os.chmod(tmp, 0o644)
                except OSError:
                    pass
                os.replace(tmp, self.index_path)
                self._last_autosave = time.time()
            except OSError as e:
                with self.index_lock:
                    self._index_dirty = True
                self.log("warn", f"Could not save {self.index_path}: {e}")
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def _autosave(self):
        """Save a long scan's progress now and then (a restart should not lose hours of listing)."""
        if time.time() - self._last_autosave > 60:
            self._last_autosave = time.time()
            self.index_save()

    def index_get(self, path):
        """The full record of one archive (with entries), or None when it is not in the index."""
        with self.index_lock:
            return self.index_load().get(path)

    def index_put(self, rec):
        if len(rec.get("entries", ())) > MAX_ENTRIES:
            rec = dict(rec, entries=rec["entries"][:MAX_ENTRIES], truncated=True)
        with self.index_lock:
            self.index_load()[rec["path"]] = rec
            self._index_dirty = True

    def index_drop(self, paths):
        n = 0
        with self.index_lock:
            idx = self.index_load()
            for p in paths:
                if idx.pop(p, None) is not None:
                    n += 1
            if n:
                self._index_dirty = True
        return n

    def _drop_vanished(self, sources):
        """End of a full scan: forget archives that are no longer in the scanned sources."""
        with self.lock:
            keep = set(self.seen) | set(self.deferred)
        gone = []
        for s in sources:
            if s in self._walk_errors:
                self.log("warn", f"{s} could not be read completely - the index is left as it is for this folder")
                continue
            prefix = s.rstrip(os.sep) + os.sep
            mine = [p for p in self.index_load() if p.startswith(prefix)]
            if not mine:
                continue
            try:
                empty = not any(True for _ in os.scandir(s))
            except OSError:
                empty = True
            if empty:
                self.log("warn", f"{s} is empty - is the share mounted? The index is left as it is for this folder")
                continue
            gone += [p for p in mine if p not in keep]
        if gone:
            n = self.index_drop(gone)
            self.log("info", f"Removed {n} archive(s) that are no longer there from the index")

    def index_query(self, sort="name", desc=False, q="", outdated=False, type="", status="", offset=0, limit=200,
                    cfg=None):
        """The list behind GET /api/archives; see the API contract. `summary` covers the whole index."""
        cfg = cfg or {}
        with self.index_lock:
            recs = list(self.index_load().values())
        recs = [self._view(r, cfg) for r in recs]
        flags = {id(r): self.is_outdated(r, cfg) for r in recs}
        summary = {"archives": len(recs), "outdated": sum(1 for r in recs if flags[id(r)]),
                   "failed": sum(1 for r in recs if r.get("status") == "failed"),
                   "bytes": sum(int(r.get("size") or 0) for r in recs)}
        needle = str(q or "").strip().lower()
        if needle:
            recs = [r for r in recs if needle in os.path.basename(r["path"]).lower()
                    or needle in os.path.dirname(r["path"]).lower()]
        if outdated:
            recs = [r for r in recs if flags[id(r)]]
        if type:
            recs = [r for r in recs if r.get("type") == type]
        if status == "failed":
            recs = [r for r in recs if r.get("status") == "failed"]
        elif status == "encrypted":
            recs = [r for r in recs if r.get("encrypted")]
        elif status == "ok":
            recs = [r for r in recs if r.get("status") == "ok" and not r.get("encrypted")]
        keys = {
            "name": lambda r: os.path.basename(r["path"]).casefold(),
            "folder": lambda r: os.path.dirname(r["path"]).casefold(),
            "archive_date": lambda r: r.get("archive_date"),
            "oldest": lambda r: r.get("oldest"),
            "newest": lambda r: r.get("newest"),
            "age_years": lambda r: r.get("age_years"),
            "files": lambda r: r.get("file_count"),
            "size": lambda r: r.get("size"),
        }
        key = keys.get(sort, keys["name"])
        recs.sort(key=lambda r: r["path"])                       # stable tie-break
        known = [r for r in recs if key(r) is not None]
        unknown = [r for r in recs if key(r) is None]
        known.sort(key=key, reverse=bool(desc))
        recs = known + unknown
        try:
            offset = max(0, int(offset))
            limit = min(5000, max(1, int(limit)))
        except (TypeError, ValueError):
            offset, limit = 0, 200
        items = []
        for r in recs[offset:offset + limit]:
            it = {k: v for k, v in r.items() if k != "entries"}
            it.update(outdated=flags[id(r)], volumes=len(r.get("volumes") or ()), files=r.get("file_count", 0),
                      name=os.path.basename(r["path"]), folder=os.path.dirname(r["path"]))
            items.append(it)
        return {"total": len(recs), "items": items, "summary": summary}

    # ------------------------------------------------------------ actions
    def _action_roots(self):
        return mounts.browse_roots(self.data_dir, self._mounts)

    @contextlib.contextmanager
    def _action(self, preview):
        """Gate of every action: refused (yields the reason) while a scan runs; a real action also keeps scans
        from starting and runs one at a time."""
        with self.lock:
            busy = self.running
            if not busy and not preview:
                self._acting += 1
        try:
            if busy:
                yield "a scan is running"
            elif preview:
                yield None
            else:
                with self._action_lock:
                    yield None
        finally:
            if not busy and not preview:
                with self.lock:
                    self._acting -= 1

    @staticmethod
    def _clean_paths(paths):
        """(unique string paths in order, refused list for the rest)."""
        out, bad, seen = [], [], set()
        if not isinstance(paths, (list, tuple)):
            return out, bad
        for p in paths:
            if not isinstance(p, str) or not p:
                bad.append({"path": p if isinstance(p, str) else str(p), "ok": False, "reason": "not a valid path"})
            elif p not in seen:
                seen.add(p)
                if len(out) >= ACTION_MAX_PATHS:
                    bad.append({"path": p, "ok": False, "reason": f"too many archives (most {ACTION_MAX_PATHS})"})
                else:
                    out.append(p)
        return out, bad

    def check_action_paths(self, paths, cfg, check_sig=True):
        """Shared guard of the actions. Returns (ok_paths, refused) where refused = [{"path", "ok": False,
        "reason"}]: a path must be in the index, inside the mapped folders, still exist and (check_sig) be unchanged
        since it was scanned."""
        paths, refused = self._clean_paths(paths)
        roots = self._action_roots()
        ok = []
        for p in paths:
            rec = self.index_get(p)
            if rec is None:
                why = "not in the index"
            elif not mounts.inside_roots(p, roots):
                why = "outside the mapped folders"
            elif not os.path.isfile(p):
                why = "the archive no longer exists - scan again"
            elif check_sig and rec.get("sig") != self._signature(p):
                why = "changed since it was scanned - scan again"
            else:
                ok.append(p)
                continue
            refused.append({"path": p, "ok": False, "reason": why})
        return ok, refused

    @staticmethod
    def _ordered(paths, results):
        """Results in the order the paths were given (refusals of invalid paths last)."""
        by = {r["path"]: r for r in results}
        order = [p for p in paths if isinstance(p, str) and p in by]
        seen = set(order)
        return [by[p] for p in dict.fromkeys(order)] + [r for r in results if r["path"] not in seen]

    def _all_refused(self, paths, why, **extra):
        cleaned, bad = self._clean_paths(paths)
        return [dict({"path": p, "ok": False, "reason": why}, **extra) for p in cleaned] + bad

    # ---- delete
    def _plan_dest(self, src, root, cfg, mode, taken=()):
        """Where `src` would end up (None for delete); raises RuntimeError with the reason when it can't."""
        if mode == "delete":
            return None
        if mode == "recycle":
            parent, why = mounts.recycle_target(src, self._mounts)
            if why:
                raise RuntimeError(f"Recycle Bin not usable: {why}")
            dest = os.path.join(parent, os.path.basename(src))
        elif mode == "move":
            if not cfg.get("move_dir"):
                raise RuntimeError("no move folder is set")
            base = os.path.basename(root.rstrip(os.sep)) or "root"
            dest = os.path.join(cfg["move_dir"], base, os.path.relpath(src, root))
        else:
            raise RuntimeError(f"unknown delete mode {mode!r}")
        if os.path.exists(dest) or dest in taken:
            n = 1
            while os.path.exists(f"{dest}.{n}") or f"{dest}.{n}" in taken:
                n += 1
            dest = f"{dest}.{n}"
        return dest

    def _volume_bytes(self, vols):
        n = 0
        for v in vols:
            try:
                n += os.stat(v).st_size
            except OSError:
                pass
        return n

    def delete_archives(self, paths, cfg, preview=True):
        """Dispose the WHOLE volume set of every archive per cfg["delete_mode"] (recycle | move | delete).
        preview=True changes nothing and reports where each would go."""
        mode = cfg.get("delete_mode", "recycle")
        started = time.time()
        with self._action(preview) as busy:
            if busy:
                res = self._all_refused(paths, busy, action=mode)
                return {"results": res, "count": 0, "bytes": 0, "preview": preview, "mode": mode}
            ok_paths, refused = self.check_action_paths(paths, cfg)
            results = [dict(r, action=mode, bytes=0) for r in refused]
            for p in ok_paths:
                results.append(self._delete_one(p, cfg, mode, preview))
            results = self._ordered(paths if isinstance(paths, list) else [], results)
            done = [r for r in results if r["ok"]]
            total = sum(r["bytes"] for r in done)
            if not preview and results:
                self.index_save()
                self.add_action_history("delete", results, total, started)
                self.log("info", f"Delete: {len(done)} archive(s), {_human(total)} ({self._act_text(mode, cfg)})"
                         + (f"; {len(results) - len(done)} refused / failed" if len(done) != len(results) else ""))
            return {"results": results, "count": len(done), "bytes": total, "preview": preview, "mode": mode}

    def _delete_one(self, path, cfg, mode, preview):
        vols = self._archive_group(path)
        res = {"path": path, "ok": False, "action": mode, "bytes": self._volume_bytes(vols), "volumes": len(vols)}
        root = self._root_for(path, cfg)
        try:
            taken, plan = set(), []
            for v in vols:
                d = self._plan_dest(v, root, cfg, mode, taken)
                if d:
                    taken.add(d)
                plan.append((v, d))
        except RuntimeError as e:
            res["reason"] = str(e)
            return res
        first = plan[0][1]
        if preview:
            res.update(ok=True, target=first or "deleted permanently")
            return res
        done = []
        try:
            for v, _d in plan:
                if mode == "recycle":
                    d = self._recycle(v, cfg)
                elif mode == "move":
                    d = self._move(v, root, cfg)
                else:
                    os.remove(v)
                    d = None
                done.append((v, d))
        except (OSError, RuntimeError) as e:
            for v, d in reversed(done):                 # put back what was already moved
                if d:
                    try:
                        shutil.move(d, v)
                    except OSError:
                        pass
            left = len(done) if mode == "delete" else 0
            res["reason"] = f"could not {self._act_text(mode, cfg)}: {e}" + (
                f" ({left} of {len(plan)} volumes were already deleted)" if left else "")
            self.log("warn", f"Delete failed for {path}: {e}")
            if left:
                self.index_drop([path])
            return res
        res.update(ok=True, target=done[0][1] or "deleted permanently")
        self.index_drop([path])
        self._forget(path)
        self.stats.deleted(res["bytes"], mode)
        self.log("info", f"Deleted ({self._act_text(mode, cfg)}): {path}")
        return res

    # ---- rename
    def _rename_file(self, old, new):
        """Rename one file, never replacing something that exists."""
        if os.path.lexists(new):
            raise OSError(f"{os.path.basename(new)} already exists")
        os.rename(old, new)

    def _plan_rename(self, path, rec, use, cfg):
        """(year, [(old, new), ...]) or raises ValueError(reason)."""
        view = self._view(rec, cfg)
        date = view.get("newest" if use == "newest" else "oldest")
        if not date:
            raise ValueError("no usable file dates in this archive")
        year = int(str(date)[:4])
        d, name = os.path.split(path)
        base = rename_base(name)
        if base is None:
            raise ValueError("not a recognised archive name")
        nb = new_base(base, year)
        if nb == base:
            raise ValueError(f"the name already ends with ({year}) - nothing to change")
        pairs = []
        for v in self._archive_group(path):
            vn = os.path.basename(v)
            if vn[:len(base)].lower() != base.lower():
                raise ValueError(f"volume {vn} does not match the archive name")
            new = nb + vn[len(base):]
            if len(new.encode()) > NAME_MAX:
                raise ValueError("the new name would be too long")
            pairs.append((v, os.path.join(d, new)))
        for _o, n in pairs:
            if os.path.lexists(n):
                raise ValueError(f"a file named {os.path.basename(n)} already exists")
        return year, pairs

    def rename_archives(self, paths, use, cfg, preview=True):
        """Append the content year ("name (YYYY).ext"; use = "newest" | "oldest") to every volume of each set,
        all-or-nothing per set with rollback; preview=True changes nothing."""
        use = use or cfg.get("rename_use", "newest")
        started = time.time()
        with self._action(preview) as busy:
            if busy:
                return {"results": self._all_refused(paths, busy, new_path=None), "count": 0}
            if use not in ("newest", "oldest"):
                return {"results": self._all_refused(paths, "use must be newest or oldest", new_path=None), "count": 0}
            ok_paths, refused = self.check_action_paths(paths, cfg)
            results = [dict(r, new_path=None) for r in refused]
            targets = set()
            moved_bytes = 0
            for p in ok_paths:
                rec = self.index_get(p)
                res = {"path": p, "ok": False, "new_path": None}
                try:
                    year, pairs = self._plan_rename(p, rec, use, cfg)
                    if any(n in targets for _o, n in pairs):
                        raise ValueError("another selected archive would get the same name")
                except ValueError as e:
                    res["reason"] = str(e)
                    results.append(res)
                    continue
                targets.update(n for _o, n in pairs)
                res.update(year=year, new_path=pairs[0][1], renamed=[[o, n] for o, n in pairs])
                if preview:
                    res["ok"] = True
                else:
                    err = self._do_rename(pairs)
                    if err:
                        res.update(new_path=None, reason=err)
                        res.pop("renamed")
                    else:
                        res["ok"] = True
                        self._move_index_entry(rec, pairs)
                        moved_bytes += rec.get("size", 0)
                        self.log("info", f"Renamed: {p} -> {pairs[0][1]}")
                results.append(res)
            results = self._ordered(paths if isinstance(paths, list) else [], results)
            done = [r for r in results if r["ok"]]
            if not preview and results:
                self.index_save()
                if done:
                    self.stats.renamed(len(done))
                self.add_action_history("rename", results, moved_bytes, started)
            return {"results": results, "count": len(done), "preview": preview}

    def _do_rename(self, pairs):
        """Rename all volumes; on a failure rename the done ones back. Returns an error text or ""."""
        done = []
        try:
            for o, n in pairs:
                self._rename_file(o, n)
                done.append((o, n))
        except OSError as e:
            bad = []
            for o, n in reversed(done):
                try:
                    self._rename_file(n, o)
                except OSError as e2:
                    bad.append(f"{os.path.basename(n)}: {e2}")
            self.log("warn", f"Rename failed ({e})" + (f"; could not restore: {bad}" if bad else "; all restored"))
            return (f"could not rename {os.path.basename(o)}: {e}" +
                    ("; all volumes were put back" if not bad else f"; NOT restored: {'; '.join(bad)}"))
        return ""

    def _move_index_entry(self, rec, pairs):
        new_first = pairs[0][1]
        new = dict(rec, path=new_first)
        new["volumes"] = self._archive_group(new_first)
        new["sig"] = self._signature(new_first)
        with self.index_lock:
            idx = self.index_load()
            idx.pop(rec["path"], None)
            idx[new_first] = new
            self._index_dirty = True
        with self.cancel_file_lock:
            self._cancelled_mem.pop(rec["path"], None)

    # ---- rescan
    def rescan(self, paths, cfg):
        """Re-list just these archives now (they must be in the index and inside the mapped folders), replacing
        their records. Shown on the threads board like a scan. Returns {"results": [{"path", "ok", ...}]}."""
        with self.lock:
            busy = self.running or self._acting
            if not busy:
                saved = {a: getattr(self, a) for a in self._RUN_ATTRS}
                self._rescanning = True
                self.stop_event.clear()
                self._fresh_run_state("rescan", cfg.get("threads", 4))
        if busy:
            return {"results": self._all_refused(paths, "a scan is running")}
        try:
            ok_paths, refused = self.check_action_paths(paths, cfg, check_sig=False)
            results = [dict(r) for r in refused]
            self.log("info", f"=== Rescan of {len(ok_paths)} archive(s) ===")
            outcomes = {}
            with ThreadPoolExecutor(max_workers=max(1, min(cfg.get("threads", 4), len(ok_paths) or 1))) as pool:
                futs = {p: pool.submit(self._work, p, cfg) for p in ok_paths}
                for p, f in futs.items():
                    outcomes[p] = f.result()
            for p in ok_paths:
                rec = self.index_get(p)
                oc = outcomes[p]
                if oc == "ok":
                    results.append({"path": p, "ok": True, "entries": rec.get("entry_count", 0)})
                elif oc == "cancelled":
                    results.append({"path": p, "ok": False, "reason": "cancelled"})
                elif rec and rec.get("status") == "failed" and p not in self.cancelled_paths:
                    results.append({"path": p, "ok": False, "reason": rec.get("error") or "listing failed"})
                else:
                    results.append({"path": p, "ok": False, "reason": "stopped"})
            self.index_save()
            return {"results": self._ordered(paths if isinstance(paths, list) else [], results)}
        finally:
            with self.lock:
                for a, v in saved.items():
                    setattr(self, a, v)
                self._rescanning = False

    # ------------------------------------------------------------ volume sets, signature, remembered state
    def _split_zip_parts(self, path):
        d, base = os.path.split(path)
        rx = re.compile("^" + re.escape(base[:-4]) + r"\.z\d\d+$", re.I)
        try:
            return [os.path.join(d, n) for n in os.listdir(d) if rx.match(n)]
        except OSError:
            return []

    def _archive_group(self, path):
        """The archive plus its other volumes (name.part2.rar, name.r00, name.z01, name.7z.002 ...)."""
        d, base = os.path.split(path)
        try:
            names = os.listdir(d)
        except OSError:
            return [path]
        m = NUMVOL_RE.match(base)
        if m:
            rx = re.compile("^" + re.escape(m.group(1)) + r"\.\d{3}$", re.I)
            return sorted(os.path.join(d, n) for n in names if rx.match(n)) or [path]
        if base.lower().endswith(".zip"):
            return [path] + sorted(self._split_zip_parts(path))
        m = FIRST_PART_RE.match(base)
        if m:
            rx = re.compile("^" + re.escape(m.group(1)) + r"\.part\d+\.rar$", re.I)
            return sorted(os.path.join(d, n) for n in names if rx.match(n)) or [path]
        m = RAR_RE.match(base)
        if m:
            rx = re.compile("^" + re.escape(m.group(1)) + r"\.r\d\d$", re.I)
            return [path] + sorted(os.path.join(d, n) for n in names if rx.match(n))
        return [path]

    def _signature(self, path):
        """Size and newest change of the whole volume set: an archive is listed again (or retried after a
        failure / cancel) once its signature changes."""
        size, newest = 0, 0
        for f in self._archive_group(path):
            try:
                st = os.stat(f)
                size, newest = size + st.st_size, max(newest, st.st_mtime)
            except OSError:
                pass
        return [size, int(newest)]

    def _load_remembered(self, path):
        try:
            with open(path) as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_remembered(self, path, d):
        """Write a remembered-archives file: entries whose archive is gone are dropped, newest 5000 kept."""
        for k in [k for k in d if not os.path.exists(k)]:
            del d[k]
        for k in list(d)[:-MAX_REMEMBERED]:
            del d[k]
        try:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(d, f)
            os.replace(tmp, path)
        except OSError as e:
            self.log("warn", f"Could not save {path}: {e}")

    def _remembered_why(self, path):
        """"was cancelled" / "failed" when this archive is remembered and unchanged since, else "".
        Cancelled archives live in cancelled-archives.json; failed ones are index records with status "failed"."""
        sig = self._signature(path)
        with self.cancel_file_lock:
            if self._cancelled_mem.get(path) == sig:
                return "was cancelled"
        rec = self.index_get(path)
        if rec and rec.get("status") == "failed" and rec.get("sig") == sig:
            return "failed"
        return ""

    def _remember(self, kind, path):
        """Remember a cancelled archive with its signature (kind is always "cancelled": failures are in the index)."""
        with self.cancel_file_lock:
            self._cancelled_mem.pop(path, None)
            self._cancelled_mem[path] = self._signature(path)
            self._save_remembered(self.cancelled_path, self._cancelled_mem)

    def _forget(self, path):
        """The archive was listed fine: it is no longer remembered as cancelled."""
        with self.cancel_file_lock:
            if self._cancelled_mem.pop(path, None) is not None:
                self._save_remembered(self.cancelled_path, self._cancelled_mem)

    # ------------------------------------------------------------ disposal (Recycle Bin / move folder / delete)
    def _root_for(self, path, cfg):
        """The source folder `path` is inside (for the move folder layout), else its own folder."""
        real = os.path.realpath(path)
        for s in cfg.get("sources", []):
            rs = os.path.realpath(s)
            if real == rs or real.startswith(rs.rstrip(os.sep) + os.sep):
                return rs
        return os.path.dirname(real)

    def _act_text(self, mode, cfg):
        return {"move": f"move to {cfg['move_dir']}", "recycle": "move to the Recycle Bin"}.get(mode, "delete permanently")

    def _dispose_archive(self, path, root, cfg):
        """Dispose the whole volume set per cfg["delete_mode"]. Returns True when every volume was handled."""
        ok = True
        for f in self._archive_group(path):
            ok = self._dispose(f, root, cfg, cfg["delete_mode"]) and ok
        return ok

    def _dispose(self, path, root, cfg, mode):
        """One file: mode = recycle | move | delete."""
        try:
            if mode == "move":
                self._move(path, root, cfg)
            elif mode == "recycle":
                self._recycle(path, cfg)
            else:
                os.remove(path)
            return True
        except (OSError, RuntimeError) as e:
            self.log("warn", f"Could not {self._act_text(mode, cfg)} {path}: {e}")
            return False

    def _recycle(self, src, cfg):
        """Move into <share>/.Recycle.Bin/<path inside the share>, where the Unraid Recycle Bin plugin keeps
        files deleted over SMB (so they are listed and emptied by the plugin)."""
        parent, why = mounts.recycle_target(src, self._mounts)
        if why:
            raise RuntimeError(f"Recycle Bin not usable: {why}")
        dest = os.path.join(parent, os.path.basename(src))
        with self.move_lock:
            missing, p = [], parent
            while not os.path.isdir(p):
                missing.append(p)
                p = os.path.dirname(p)
            for m in reversed(missing):
                os.mkdir(m)
                self._apply_perm(m, True)
            if os.path.exists(dest):                 # never overwrite something already in the bin
                n = 1
                while os.path.exists(f"{dest}.{n}"):
                    n += 1
                dest = f"{dest}.{n}"
            shutil.move(src, dest)
        self._apply_perm(dest, False)
        return dest

    def _move(self, src, root, cfg):
        """Move into move_dir/<source folder name>/<path relative to source>."""
        base = os.path.basename(root.rstrip(os.sep)) or "root"
        dest = os.path.join(cfg["move_dir"], base, os.path.relpath(src, root))
        with self.move_lock:
            missing, p = [], os.path.dirname(dest)
            while not os.path.isdir(p):
                missing.append(p)
                p = os.path.dirname(p)
            for m in reversed(missing):
                os.mkdir(m)
                self._apply_perm(m, True)
            if os.path.exists(dest):                 # never overwrite something already moved
                n = 1
                while os.path.exists(f"{dest}.{n}"):
                    n += 1
                dest = f"{dest}.{n}"
            shutil.move(src, dest)
        self._apply_perm(dest, False)
        return dest

    def _apply_perm(self, path, is_dir):
        """Best effort: folders the app creates / files it moves stay usable over SMB (there are no permission
        settings in AutoDate)."""
        try:
            os.chmod(path, DIR_MODE if is_dir else FILE_MODE)
        except OSError as e:
            self._warn_once(("perm", type(e).__name__), f"Permission change failed ({path}): {e}")
