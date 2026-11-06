"""Settings: defaults, first-start values from the mapped folders, validation, storage."""
import json
import os
import re
import threading

import mounts

# App data: settings.json, history, logs and the login key. Mapped to e.g. /mnt/user/appdata/autodate.
CONFIG_DIR = os.environ.get("DATA_DIR") or "/data"
SETTINGS_PATH = os.path.join(CONFIG_DIR, "settings.json")

DEFAULTS = {
    "sources": [],                    # folders to scan (paths as seen inside the container)
    "schedule_enabled": False,        # scan on an interval
    "interval_minutes": 60,           # minutes between the end of one scan and the start of the next
    "run_on_start": False,            # also scan right after the container starts
    "threads": 4,                     # archives listed in parallel
    "min_age_minutes": 5,             # ignore archives modified more recently than this (still downloading)
    "include_folders": False,         # folder dates also count towards oldest/newest content date
    "outdated_years": 5,              # an archive is "outdated" when its contents are older than this many years ...
    "outdated_by": "newest",          # ... judged by its newest file or its oldest file: newest | oldest
    "rename_use": "newest",           # which content date gives the year in "name (YYYY).ext": newest | oldest
    "delete_mode": "recycle",         # deleted archives go to: recycle (share's .Recycle.Bin) | move | delete
    "move_dir": "",                   # where "move" puts deleted archives
    "exclude_dirs": ["@eaDir", ".Recycle.Bin", "#recycle", ".Trash*"],
}

_lock = threading.Lock()


MODES = ("recycle", "move", "delete")
YEAR_SOURCES = ("newest", "oldest")


def env_defaults():
    """First-start settings, from the folders mapped in the template: /downloads is scanned; deleted archives go
    to /trash if it is mapped, otherwise to the share's Recycle Bin (.Recycle.Bin, as used by the Unraid Recycle
    Bin plugin) when the share root can be reached, otherwise they are deleted permanently. That last case is
    acceptable here: nothing is ever deleted unless the user selects archives and confirms a preview that says
    "permanently"."""
    d = dict(DEFAULTS)
    d["sources"] = mounts.suggested_sources(CONFIG_DIR)
    move = mounts.suggested_move_dir(CONFIG_DIR)
    if move:
        d.update(move_dir=move, delete_mode="move")
    elif d["sources"]:
        mnt = mounts.list_mounts(CONFIG_DIR)
        if any(mounts.recycle_target(os.path.join(p, "x"), mnt)[1] for p in d["sources"]):
            d.update(delete_mode="delete")      # a source's Recycle Bin can't be reached (second value = why not)
    return d


def _lines(value):
    """Accept a list or a newline/comma separated string; return clean unique entries."""
    if isinstance(value, str):
        value = re.split(r"[\n,]", value)
    out = []
    for x in value or []:
        x = str(x).strip()
        if x and x not in out:
            out.append(x)
    return out


def _within(path, parent):
    path, parent = os.path.normpath(path), os.path.normpath(parent)
    return path == parent or path.startswith(parent.rstrip(os.sep) + os.sep)


def validate(data, mapped=False):
    """Return (clean_settings, errors). Unknown keys are dropped, missing ones get defaults.
    mapped=True also requires the source and move folders to be inside folders mapped into the container
    (what save() and load() do; the mappings are read from the mount table)."""
    s, errors, _ = _validate(data, mapped)
    return s, errors


def _validate(data, mapped=True):
    """Return (clean_settings, errors, bad) where bad maps a setting name to the problems found with it
    (for sources: the offending paths), so a lenient load can reset just those values."""
    errors = []
    bad = {}
    s = dict(DEFAULTS)
    roots = mounts.browse_roots(CONFIG_DIR) if mapped else None

    def err(key, msg, path=None):
        errors.append(msg)
        bad.setdefault(key, []).append(path)

    def num(key, lo, hi, label):
        try:
            v = int(data.get(key, s[key]))
        except (TypeError, ValueError):
            err(key, f"{label} must be a whole number")
            return
        if not lo <= v <= hi:
            err(key, f"{label} must be between {lo} and {hi}")
            return
        s[key] = v

    def boolean(key):
        v = data.get(key, s[key])
        s[key] = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")

    s["sources"] = [os.path.normpath(p) for p in _lines(data.get("sources", s["sources"]))]
    for p in s["sources"]:
        if not p.startswith("/"):
            err("sources", f"Source folder must be an absolute path: {p}", p)
        elif _within(p, CONFIG_DIR) or _within(CONFIG_DIR, p):
            err("sources", f"Source folder {p} is or contains the app data folder - choose a folder to scan "
                           "such as /downloads", p)
        elif roots is not None and not mounts.inside_roots(p, roots):
            err("sources", f"Source folder {p} is not inside a folder mapped into the container "
                           "(such as /downloads or /mnt/user)", p)

    for k in ("schedule_enabled", "run_on_start", "include_folders"):
        boolean(k)
    num("interval_minutes", 1, 525600, "Scan interval")
    num("threads", 1, 32, "Threads")
    num("min_age_minutes", 0, 100000, "Minimum file age")
    num("outdated_years", 0, 200, "Outdated after (years)")

    for k, label, choices in (("delete_mode", "Deleted archives go to", MODES),
                              ("outdated_by", "Outdated means", YEAR_SOURCES),
                              ("rename_use", "Year used for renaming", YEAR_SOURCES)):
        v = str(data.get(k, s[k])).lower()
        if v not in choices:
            err(k, f"{label} must be " + ", ".join(choices[:-1]) + " or " + choices[-1])
        else:
            s[k] = v

    s["exclude_dirs"] = _lines(data.get("exclude_dirs", s["exclude_dirs"]))

    s["move_dir"] = str(data.get("move_dir", "") or "").strip()
    if s["move_dir"]:
        s["move_dir"] = os.path.normpath(s["move_dir"])
        md = s["move_dir"]
        if not md.startswith("/"):
            err("move_dir", "Move folder must be an absolute path")
        elif _within(md, CONFIG_DIR) or _within(CONFIG_DIR, md):
            err("move_dir", f"Move folder {md} is or contains the app data folder - choose a separate folder "
                            "such as /trash")
        elif roots is not None and not mounts.inside_roots(md, roots):
            err("move_dir", f"Move folder {md} is not inside a folder mapped into the container "
                            "(such as /trash or /mnt/user)")
        else:
            for src in s["sources"]:
                if _within(md, src):
                    err("move_dir", f"Move folder {md} must not be inside the source folder {src}")
                elif _within(src, md):
                    err("move_dir", f"Source folder {src} must not be inside the move folder {md} - "
                                    "give the scanned folder and the processed folder separate mappings")
    if s["delete_mode"] == "move" and not s["move_dir"]:
        err("move_dir", "A move folder is required when deleted archives are set to be moved")

    return s, errors, bad


def _lenient(data):
    """Validate saved settings; every value that fails is replaced by its default (a bad source folder is
    dropped from the list), everything else is kept. Returns (settings, problems)."""
    clean, problems, bad = _validate(data)
    if not problems:
        return clean, problems
    fixed = dict(clean)
    for key, items in bad.items():
        if key == "sources":
            fixed[key] = [p for p in clean[key] if p not in items]
        else:
            fixed[key] = DEFAULTS[key]
    if fixed["delete_mode"] == "move" and not fixed["move_dir"]:   # "move" needs a move folder
        fixed["delete_mode"] = DEFAULTS["delete_mode"]
    final, left, _ = _validate(fixed)
    if left:                                  # should not happen; never return something that fails validation
        final = _validate(DEFAULTS)[0]
    return final, problems


def load_with_report():
    """Return (settings, problems, source). One bad value never throws the others away:
    every valid saved setting is kept, only the invalid ones fall back to their default.
    source: "file" (saved settings), "defaults" (nothing saved yet) or "unreadable"."""
    with _lock:
        data, source = None, "defaults"
        try:
            with open(SETTINGS_PATH) as f:
                data = json.load(f)
            source = "file"
            if not isinstance(data, dict):
                data, source = None, "unreadable"
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            source = "unreadable"
        if data is None:                      # nothing usable saved: first-run defaults
            data = env_defaults()
        clean, problems = _lenient(data)
        return clean, problems, source


def load():
    return load_with_report()[0]


def storage_info():
    """Where settings live and whether they are really being kept."""
    try:
        saved_at = os.path.getmtime(SETTINGS_PATH)
    except OSError:
        saved_at = None
    return {"path": SETTINGS_PATH, "saved_at": saved_at}


def save(data):
    clean, errors = validate(data, mapped=True)
    if errors:
        return None, errors
    with _lock:
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            tmp = SETTINGS_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(clean, f, indent=2)
            os.replace(tmp, SETTINGS_PATH)
            with open(SETTINGS_PATH) as f:        # read it back: prove it was stored
                if json.load(f) != clean:
                    raise OSError("settings file did not read back correctly")
        except (OSError, ValueError) as e:
            return None, [f"Could not store settings in {SETTINGS_PATH} ({e}). "
                          "Check that the App data folder is mapped to a writable folder."]
    return clean, []
