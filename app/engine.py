"""AutoDate engine (PLACEHOLDER).

AutoDate never extracts anything: it reads the original dates of the files stored inside each archive
(see the spec) and keeps them in an index so the user can sort, filter, rename or delete archives.

This file is the scaffold. It keeps the generic machinery that came from AutoExtract unchanged in behaviour -
logging, the status snapshot shape, start / stop / cancel_job, thread slots, the waiting / min-age helpers,
the volume-set signature, the remembered-state helpers and the Recycle Bin / move / delete disposal helpers,
history and stats hooks - and replaces everything extractor-specific with clearly marked stubs:

    TODO(engine agent)  list_archive, the index (query, hardening), delete / rename / rescan actions.

Everything marked TODO(engine agent) is the engine agent's to build; main.py already imports and calls the
signatures below, so keep them (or update main.py in the same change).
"""
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
    TODO(engine agent): list_archive() fills the dates, counts, flags and entries."""
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
        "archive_date": newest or None,    # newest mtime of the set
        "size": sizes,                     # sum of the volume sizes
        "sig": list(signature),            # [size, int(newest mtime)] of the set when it was listed
        "scanned_at": time.time(),
        "status": "ok",                    # ok | failed (listing failed: damaged, missing volume, ...)
        "error": "",
        "entry_count": 0, "file_count": 0, "folder_count": 0,
        "oldest": None, "newest": None,                # content dates over FILES (epoch seconds, naive local time as stored)
        "oldest_any": None, "newest_any": None,        # over files + folders
        "age_years": None, "age_years_oldest": None,   # archive date - newest / oldest file date, in years
        "encrypted": False,                # headers encrypted: nothing can be read
        "partial": False,                  # some entries had no date
        "future": False,                   # some date is later than now + 1 day
        "truncated": False,                # more than MAX_ENTRIES entries; the rest were only counted
        "entries": [],                     # [{"name", "folder": bool, "size", "date": epoch|None}]
    }


class Engine:
    def __init__(self, log_dir, history_path):
        self.lock = threading.RLock()
        self.move_lock = threading.Lock()
        self.cancel_file_lock = threading.Lock()
        self.index_lock = threading.RLock()
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
        return bool(t and t.is_alive())

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
            if self.running:
                return False
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._run, args=(dict(cfg), trigger, oneoff), daemon=True)
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
            self._slots = [None] * cfg["threads"]
            self.thread.start()
            return True

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
                # TODO(engine agent): a full (not one-off, not stopped) scan drops the index records of archives
                # that vanished from the scanned sources: self.index_drop([...]) then self.index_save().
                pass
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
        for dirpath, dirnames, filenames in os.walk(root):
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
            oldest_year = time.localtime(rec["oldest"]).tm_year if rec.get("oldest") else None
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
        d = rec.get("newest") if cfg.get("outdated_by", "newest") == "newest" else rec.get("oldest")
        if not d:
            return False
        return (time.time() - d) / (365.25 * 86400) > cfg.get("outdated_years", 5)

    # ------------------------------------------------------------ TODO(engine agent): reading dates
    def list_archive(self, path, cfg, slot=None):
        """TODO(engine agent): read the dates of every entry of the archive set starting at `path` (first volume)
        WITHOUT extracting, and return its index record (see blank_record() for the exact shape and the spec,
        "Reading dates"). Use self._exec(cmd, slot) to run unrar / 7z / tar (own process group, stdin closed,
        killed by Stop / Cancel); raise Halted when self._halted(slot) after a tool returns. A listing that
        fails (damaged, missing volume) returns a record with status "failed" and the error text - do not raise.

        PLACEHOLDER: returns the file-level facts only (type, volumes, size, archive date, signature) and no
        entries, so a scan runs end to end."""
        group = self._archive_group(path)
        return blank_record(path, group, self._signature(path))

    def _exec(self, cmd, slot=None):
        """Run a tool in its own process group (so Stop / Cancel can kill it and anything it spawned) with stdin
        closed. Returns (exit code or None, combined output)."""
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, errors="replace",
                                 start_new_session=True)
        except OSError as e:
            return None, str(e)
        with self.lock:
            self.procs.add(p)
            if slot is not None:
                self._slot_procs.setdefault(slot, set()).add(p)
            cancel_now = self._halted(slot)
        if cancel_now:                                     # cancelled between the check and the start
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except OSError:
                pass
        try:
            out, _ = p.communicate()
        finally:
            with self.lock:
                self.procs.discard(p)
                if slot is not None:
                    self._slot_procs.get(slot, set()).discard(p)
        return p.returncode, out or ""

    # ------------------------------------------------------------ the index
    # {path (first volume): record}. Minimal working version so the scaffold runs end to end.
    # TODO(engine agent): cap entries per record at MAX_ENTRIES (flag "truncated"), keep the file compact,
    # make sure a big index never blocks the status endpoints (index_lock is held only for dict operations).
    def index_load(self):
        """Read /data/index.json into memory (once); returns the dict. A missing or damaged file starts empty."""
        with self.index_lock:
            if self._index is None:
                try:
                    with open(self.index_path) as f:
                        data = json.load(f)
                    self._index = data if isinstance(data, dict) else {}
                except (OSError, ValueError):
                    self._index = {}
            return self._index

    def index_save(self):
        """Write the index atomically (temp file + replace)."""
        with self.index_lock:
            snap = json.dumps(self.index_load())
            try:
                tmp = self.index_path + ".tmp"
                with open(tmp, "w") as f:
                    f.write(snap)
                os.replace(tmp, self.index_path)
            except OSError as e:
                self.log("warn", f"Could not save {self.index_path}: {e}")

    def index_get(self, path):
        """The full record of one archive (with entries), or None when it is not in the index."""
        with self.index_lock:
            return self.index_load().get(path)

    def index_put(self, rec):
        with self.index_lock:
            self.index_load()[rec["path"]] = rec

    def index_drop(self, paths):
        with self.index_lock:
            idx = self.index_load()
            for p in paths:
                idx.pop(p, None)

    def index_query(self, sort="name", desc=False, q="", outdated=False, type="", status="", offset=0, limit=200,
                    cfg=None):
        """TODO(engine agent): the list behind GET /api/archives. Filter by text `q` (name / folder), `outdated`
        (self.is_outdated with cfg), `type` (zip|rar|7z|tar) and `status` (ok|failed|encrypted), sort by one of
        name, folder, archive_date, oldest, newest, age_years, files, size (unknown dates last), page with
        offset / limit. Returns {"total": n, "items": [record WITHOUT entries + "outdated": bool + "volumes":
        count], "summary": {"archives", "outdated", "failed", "bytes"}}."""
        raise NotImplementedError("TODO(engine agent): index_query")

    # ------------------------------------------------------------ TODO(engine agent): actions
    def check_action_paths(self, paths, cfg):
        """TODO(engine agent): shared guard of the actions. Returns ({path: record} for the paths that are in the
        index, inside the mapped roots and whose signature still matches, {path: reason} for the refused ones -
        "not in the index", "outside the mapped folders", "changed since it was scanned - scan again")."""
        raise NotImplementedError("TODO(engine agent): check_action_paths")

    def delete_archives(self, paths, cfg, preview=True):
        """TODO(engine agent): dispose the WHOLE volume set of every archive per cfg["delete_mode"]
        (recycle | move | delete) using _dispose_archive(); preview=True changes nothing. Returns
        {"results": [{"path", "ok", "action", "target" | "reason"}]}. Logs, add_action_history("delete", ...),
        stats.deleted(size, mode), drops deleted archives from the index."""
        raise NotImplementedError("TODO(engine agent): delete_archives")

    def rename_archives(self, paths, use, cfg, preview=True):
        """TODO(engine agent): append the content year ("name (YYYY).ext", use = "newest" | "oldest"), renaming
        all volumes consistently, all-or-nothing per set with rollback; preview=True changes nothing. Returns
        {"results": [{"path", "ok", "new_path", "renamed": [[old, new], ...] | "reason"}]}. Logs,
        add_action_history("rename", ...), stats.renamed(), moves the index entry to the new path."""
        raise NotImplementedError("TODO(engine agent): rename_archives")

    def rescan(self, paths, cfg):
        """TODO(engine agent): re-list just these archives now (the paths must be in the index) in the thread pool
        / as a small one-off run, replacing their index records. Returns (ok, error text)."""
        raise NotImplementedError("TODO(engine agent): rescan")

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
