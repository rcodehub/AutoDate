"""Path / mount handling checks. Run:  python3 -I tests/test_paths.py"""
import importlib
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))
FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


root = tempfile.mkdtemp(prefix="ad-paths-")
cfg, dl, proc, media = (os.path.join(root, d) for d in ("config", "downloads", "processed", "media"))
for d in (cfg, dl, proc, media):
    os.makedirs(d)
os.chmod(media, 0o555)   # read-only for non-root; root can still write, so only checked loosely


def mi(i, mp, fs, src="x"):
    return f"{i} 1 0:{i} /somewhere {mp} rw,noatime master:1 - {fs} {src} rw"


info = "\n".join([
    "20 1 0:20 / / rw - overlay overlay rw",
    "21 20 0:21 / /proc rw - proc proc rw",
    "22 20 0:22 / /dev rw - tmpfs tmpfs rw",
    "23 20 0:23 /etc/hosts /etc/hosts rw - ext4 /dev/sda1 rw",
    mi(30, cfg, "fuse.shfs", "shfs"),
    mi(31, dl, "fuse.shfs", "shfs"),
    mi(32, proc, "xfs", "/dev/md1"),
    mi(33, media.replace(" ", r"\040"), "btrfs", "/dev/sdb1"),
]) + "\n"
mi_path = os.path.join(root, "mountinfo")
open(mi_path, "w").write(info)

os.environ.update(MOUNTINFO_PATH=mi_path, DATA_DIR=cfg, DOWNLOADS_DIR=dl, TRASH_DIR=proc, NO_SCHEDULER="1")
for k in ("UI_PASSWORD", "UI_USERNAME"):
    os.environ.pop(k, None)
for m in ("mounts", "settings", "engine", "main"):
    sys.modules.pop(m, None)
mounts = importlib.import_module("mounts")
settings = importlib.import_module("settings")
main = importlib.import_module("main")
check("settings.json is created at first start", os.path.isfile(os.path.join(cfg, "settings.json")))

ms = mounts.list_mounts(cfg)
paths = [m["path"] for m in ms]
check("system mounts are hidden", not any(p in ("/", "/proc", "/dev", "/etc/hosts") for p in paths), paths)
check("mapped folders are listed", {cfg, dl, proc, media} <= set(paths), paths)
check("config folder is flagged", [m["path"] for m in ms if m["config"]] == [cfg])
check("free space reported", all(m["free"] is not None for m in ms))

sug = mounts.suggested_sources(cfg)
check("first-run source = /downloads", sug == [dl], sug)
mounts.DOWNLOADS_DIR = os.path.join(root, "not-mapped")
sug = mounts.suggested_sources(cfg)
check("no /downloads: other mapped folders, never data or trash", set(sug) == {dl, media}, sug)
mounts.DOWNLOADS_DIR = dl
check("trash detected on the file system", mounts.suggested_move_dir(cfg) == proc)
mounts.TRASH_DIR = os.path.join(root, "not-mapped")
check("no trash mapped: no move folder", mounts.suggested_move_dir(cfg) == "")
mounts.TRASH_DIR = proc
s, e = settings.validate({"sources": [cfg]})
check("app data folder refused as a source", e, e)

pi = mounts.path_info(dl + "/sub/dir", ms)
check("path_info: missing folder inside a mount", not pi["exists"] and pi["mount"] == dl and not pi["host_path"], pi)
pi = mounts.path_info("/mnt/user/Downloads", ms)
check("path_info: Unraid host path is recognised", not pi["exists"] and pi["host_path"], pi)
pi = mounts.path_info(dl, ms)
check("path_info: good folder", pi["exists"] and pi["readable"] and pi["writable"] and pi["mount"] == dl, pi)

# validation, both directions
s, e = settings.validate({"sources": [dl], "delete_mode": "move", "move_dir": dl + "/done"})
check("move folder inside a source is refused", any("must not be inside the source" in x for x in e), e)
s, e = settings.validate({"sources": [proc + "/incoming"], "delete_mode": "move", "move_dir": proc})
check("source inside the move folder is refused", any("must not be inside the move folder" in x for x in e), e)
s, e = settings.validate({"sources": [dl], "delete_mode": "move", "move_dir": proc})
check("separate folders are accepted", not e, e)

# first-start settings come from the mapped folders
os.remove(os.path.join(cfg, "settings.json")) if os.path.exists(os.path.join(cfg, "settings.json")) else None
seed = settings.load()
check("first run: source is /downloads",
      seed["sources"] == [dl], seed["sources"])
check("first run: mapped trash folder becomes the move folder",
      seed["move_dir"] == proc and seed["delete_mode"] == "move", seed)
check("first run: scheduling stays off", seed["schedule_enabled"] is False)

# API
c = main.app.test_client()
d = c.get("/api/mounts").get_json()
check("api/mounts", {m["path"] for m in d["mounts"]} >= {dl, proc}, d)
d = c.get("/api/pathinfo?path=/mnt/user/foo").get_json()
check("api/pathinfo flags host paths", d["info"]["host_path"] is True, d)
r = c.post("/api/settings", json={"sources": [dl + "/nope", dl], "move_dir": proc, "delete_mode": "move"})
d = c.get("/api/settings").get_json()
check("api/settings reports per-source info",
      r.status_code == 200 and not d["sources_info"][dl + "/nope"]["exists"] and d["sources_info"][dl + "/nope"]["mount"] == dl
      and d["sources_info"][dl]["exists"] and d["move_info"]["exists"], d.get("sources_info"))

# --- sources / move folder must be inside mapped folders, away from the app data folder
r = c.post("/api/settings", json={"sources": ["/mnt/user/nope"]})
check("unmapped source refused on save", r.status_code == 400 and "not inside a folder mapped" in str(r.get_json()), r.get_json())
r = c.post("/api/settings", json={"sources": [dl], "delete_mode": "move", "move_dir": "/tmp/elsewhere"})
check("unmapped move folder refused on save", r.status_code == 400 and "not inside a folder mapped" in str(r.get_json()), r.get_json())
r = c.post("/api/settings", json={"sources": [dl], "delete_mode": "move", "move_dir": cfg + "/moved"})
check("move folder inside app data refused", r.status_code == 400 and "app data folder" in str(r.get_json()), r.get_json())
s_, e_ = settings.validate({"sources": [dl], "delete_mode": "move", "move_dir": os.path.dirname(cfg)})
check("move folder containing app data refused", any("app data folder" in x for x in e_), e_)
s_, e_ = settings.validate({"sources": [os.path.dirname(cfg)]})
check("source containing app data refused", any("app data folder" in x for x in e_), e_)
for mode in ("recycle", "move", "delete"):
    r = c.post("/api/settings", json={"sources": [dl], "delete_mode": mode, "move_dir": proc})
    check(f"delete mode {mode} is valid", r.status_code == 200 and settings.load()["delete_mode"] == mode, r.get_json())
r = c.post("/api/settings", json={"sources": [dl], "delete_mode": "shred"})
check("unknown delete mode refused", r.status_code == 400)
r = c.post("/api/settings", json={"sources": [dl], "delete_mode": "move", "move_dir": ""})
check("move mode needs a move folder", r.status_code == 400 and "move folder is required" in str(r.get_json()), r.get_json())
check("default delete mode is the Recycle Bin", settings.DEFAULTS["delete_mode"] == "recycle")
r = c.post("/api/settings", json={"sources": [dl], "outdated_years": 10, "outdated_by": "oldest", "rename_use": "oldest",
                                   "include_folders": True, "delete_mode": "recycle"})
cfg_s = settings.load()
check("new options round-trip", r.status_code == 200 and cfg_s["outdated_years"] == 10 and cfg_s["outdated_by"] == "oldest"
      and cfg_s["rename_use"] == "oldest" and cfg_s["include_folders"] is True, cfg_s)
check("new option defaults", settings.DEFAULTS["outdated_years"] == 5 and settings.DEFAULTS["outdated_by"] == "newest"
      and settings.DEFAULTS["rename_use"] == "newest" and settings.DEFAULTS["include_folders"] is False)
for bad in ({"outdated_by": "middle"}, {"rename_use": "never"}, {"outdated_years": -1}, {"outdated_years": "x"}):
    check(f"bad value refused: {bad}", c.post("/api/settings", json=dict({"sources": [dl]}, **bad)).status_code == 400)
check("removed extraction options are gone", not {"overwrite", "junk_patterns", "max_passes", "archive_mode", "junk_mode",
      "set_perms", "file_mode", "dir_mode", "chown_enabled", "uid", "gid"} & set(settings.DEFAULTS))

# --- new routes are stubs for now (TODO(web agent)): 501 JSON, behind the same login
for url in ("/api/archives", "/api/archive"):
    r = c.get(url)
    check(f"{url}: stub answers 501 json", r.status_code == 501 and r.get_json() == {"error": "not implemented"}, r.status_code)
for url in ("/api/archives/delete", "/api/archives/rename", "/api/archives/rescan"):
    r = c.post(url, json={"paths": []})
    check(f"{url}: stub answers 501 json", r.status_code == 501 and r.get_json() == {"error": "not implemented"}, r.status_code)

# --- JSON bodies that are not objects: 400, never 500
for url in ("/api/settings", "/api/run", "/api/run-folder", "/api/cancel", "/api/ui"):
    for body in ("[1, 2]", '"text"', "42", "null", "{broken"):
        r = c.post(url, data=body, content_type="application/json")
        check(f"{url}: body {body} gives 400", r.status_code == 400 and r.get_json().get("error"), (r.status_code, r.get_data(as_text=True)[:80]))
check("empty body still accepted by /api/cancel", c.post("/api/cancel").status_code in (200, 409))

# --- stats file: tolerant load, no shared tmp file, scans mark the data dirty
import json as _json
import stats as _stats
import threading as _th
sp = os.path.join(root, "stats-test.json")
open(sp, "w").write(_json.dumps({"since": "x", "totals": [], "types": 5, "days": None, "records": {}}))
st = _stats.Stats(sp)
check("stats: wrong-typed keys fall back to blank", st.data["totals"]["archives"] == 0 and st.data["types"] == {}
      and st.data["days"] == {} and isinstance(st.data["since"], float), st.data)
open(sp, "w").write("[1, 2]")
check("stats: non-object file falls back to blank", _stats.Stats(sp).data["totals"]["runs"] == 0)
st = _stats.Stats(sp)
ts = [_th.Thread(target=lambda: [st.save() for _ in range(30)]) for _ in range(6)]
[t.start() for t in ts]
[t.join() for t in ts]
check("stats: parallel saves leave a valid file and no temp files",
      isinstance(_json.load(open(sp)), dict) and [f for f in os.listdir(root) if f.startswith("stats.")] == [])
for _ in range(10):
    st.scanned("/x/a.zip", 3, 100, 0.1)
check("stats: scanned() marks data dirty (saved after 10)", _json.load(open(sp))["totals"]["archives"] == 10)

# --- persistence: a bad value must not wipe the good ones; problems and storage are reported
import json
import settings as S
_json_dumps = json.dumps
open(S.SETTINGS_PATH, "w").write(json.dumps({"sources": [dl], "threads": 7, "interval_minutes": "oops",
                                              "exclude_dirs": ["*.nfo"], "schedule_enabled": True}))
cfg_now, problems, source = S.load_with_report()
check("lenient load keeps valid values", cfg_now["threads"] == 7 and cfg_now["sources"] == [dl]
      and cfg_now["exclude_dirs"] == ["*.nfo"] and cfg_now["schedule_enabled"] is True, cfg_now)
check("lenient load reports the bad one", source == "file" and any("interval" in x.lower() for x in problems), problems)
d = c.get("/api/settings").get_json()
check("api/settings exposes problems + storage",
      d["problems"] and d["storage"]["source"] == "file" and d["storage"]["path"] == S.SETTINGS_PATH
      and d["storage"]["saved_at"] and d["storage"]["config_mapped"] is True, d.get("storage"))
open(S.SETTINGS_PATH, "w").write(_json_dumps({"sources": [dl, "/etc", "/mnt/user/nope"], "move_dir": cfg + "/m",
                                              "delete_mode": "move", "outdated_by": "sideways", "threads": 9}))
cfg_now, problems, source = S.load_with_report()
check("lenient load: bad paths reset, the rest kept",
      cfg_now["sources"] == [dl] and cfg_now["move_dir"] == "" and cfg_now["delete_mode"] == S.DEFAULTS["delete_mode"]
      and cfg_now["outdated_by"] == "newest" and cfg_now["threads"] == 9 and len(problems) >= 3, (cfg_now, problems))
open(S.SETTINGS_PATH, "w").write("{not json")
_, problems, source = S.load_with_report()
check("unreadable file reported", source == "unreadable", source)
os.remove(S.SETTINGS_PATH)
check("missing file = defaults", S.load_with_report()[2] == "defaults")
r = c.post("/api/settings", json={"sources": [dl], "threads": 3})
check("save works and round-trips", r.status_code == 200 and S.load()["threads"] == 3, r.get_data(as_text=True))
check("saved file is valid", c.get("/api/settings").get_json()["storage"]["source"] == "file")
# unwritable location -> clear error, not silent success
real = S.SETTINGS_PATH
S.SETTINGS_PATH = os.path.join(root, "no-such-dir", "x", "settings.json")
S.CONFIG_DIR = "/proc/nonexistent-ea"  # unwritable
res, errs = S.save({"sources": [dl]})
check("save failure is reported", res is None and errs and "Could not store" in errs[0], errs)
S.SETTINGS_PATH, S.CONFIG_DIR = real, cfg

# --- server (host) paths behind each mapping
check("host: user share", mounts.host_path("fuse.shfs", "shfs", "/downloads") == "/mnt/user/downloads")
check("host: array disk", mounts.host_path("xfs", "/dev/md1p1", "/trash") == "/mnt/disk1/trash")
check("host: zfs dataset", mounts.host_path("zfs", "cache/appdata", "/autodate") == "/mnt/cache/appdata/autodate")
check("host: unknown pool", mounts.host_path("btrfs", "/dev/nvme0n1p1", "/appdata") is None)
ms2 = mounts.list_mounts(cfg)
dlm = [m for m in ms2 if m["path"] == dl][0]
check("host: shown for a mapped folder", dlm["host"] == "/mnt/user/somewhere", dlm)
pi = mounts.path_info(dl + "/sub", ms2)
check("host: path inside a mapping", pi["host"] == "/mnt/user/somewhere/sub", pi)

# --- hidden folders are listed in the folder picker
os.makedirs(os.path.join(dl, ".hidden"), exist_ok=True)
os.makedirs(os.path.join(dl, ".autodate-tmp1"), exist_ok=True)
os.makedirs(os.path.join(dl, "Visible"), exist_ok=True)
d = c.get("/api/browse?path=" + dl).get_json()
check("browse: hidden folders listed, work folders not", ".hidden" in d["dirs"] and "Visible" in d["dirs"]
      and not any(x.startswith(".autodate-") for x in d["dirs"]), d)

# --- clear the on-screen log
main.engine.log("info", "something")
check("log: has entries", c.get("/api/log?after=0").get_json()["entries"])
r = c.post("/api/log/clear", json={})
d = c.get("/api/log?after=0").get_json()
check("log: cleared", r.status_code == 200 and not d["entries"] and d["cleared"] == d["last"], d)

# --- scheduler: a scan that finishes between two ticks must not start the next one at once
empty = os.path.join(dl, "empty-src")
os.makedirs(os.path.join(empty, "leftover-folder"))
r = c.post("/api/settings", json={"sources": [empty], "schedule_enabled": True, "interval_minutes": 5,
                                   "run_on_start": False})
check("scheduler: settings saved", r.status_code == 200, r.get_data(as_text=True))
sch = main.Scheduler()
sch.started = time.time() - 3600             # container has been up longer than the interval
runs_before = len(main.engine.history)
sch.tick()                                   # overdue: first scan starts
if main.engine.thread:
    main.engine.thread.join(10)
for _ in range(5):                           # the scan finished between ticks
    sch.tick()
    time.sleep(0.05)
if main.engine.thread:
    main.engine.thread.join(10)
check("scheduler: no scan loop", len(main.engine.history) - runs_before == 1, len(main.engine.history) - runs_before)
check("scheduler: next scan one interval after the last one ended",
      abs(sch.next_run - (main.engine.status["finished_at"] + 300)) < 2, (sch.next_run, main.engine.status["finished_at"]))
main.engine.start(settings.load(), trigger="manual")   # a manual scan also resets the interval
main.engine.thread.join(10)
sch.tick()
check("scheduler: manual scan counts as the last scan", abs(sch.next_run - (main.engine.status["finished_at"] + 300)) < 2)
c.post("/api/settings", json={"sources": [dl], "schedule_enabled": False})

# --- appearance: saved on the server, validated
d = c.get("/api/ui").get_json()["ui"]
check("ui: defaults", d["theme"] == "default" and d["width"] == "wide", d)
r = c.post("/api/ui", json={"theme": "nord", "radius": 99, "density": "spacious", "width": "full", "font": "evil", "x": 1})
d = c.get("/api/ui").get_json()["ui"]
check("ui: saved and cleaned", d["theme"] == "nord" and d["radius"] == 24 and d["density"] == "spacious"
      and d["width"] == "full" and d["font"] == "" and "x" not in d, d)
check("ui: theme.js reachable", c.get("/static/theme.js").status_code == 200)

# --- Recycle Bin and one-off jobs
d = c.get("/api/settings").get_json()
rb = d["recycle"][0] if d["recycle"] else {}
check("recycle: settings page reports each source's bin", rb.get("ok") and rb.get("bin") == os.path.join(dl, ".Recycle.Bin")
      and rb.get("host") == "/mnt/user/somewhere/.Recycle.Bin", d["recycle"])
mounts.TRASH_DIR = os.path.join(root, "not-mapped")
if os.path.exists(os.path.join(cfg, "settings.json")):
    os.remove(os.path.join(cfg, "settings.json"))
seed = settings.load()
check("first start without /trash: Recycle Bin when the share root is reachable",
      seed["delete_mode"] == "recycle", seed)
# ... and when the share root can't be reached, deleting is permanent (only ever on explicit request + preview)
real_mi = mounts.MOUNTINFO
nested = os.path.join(root, "mountinfo-nested")
open(nested, "w").write(f"31 1 0:31 /share/sub {dl} rw - fuse.shfs shfs rw\n")
mounts.MOUNTINFO = nested
seed = settings.load()
check("first start, Recycle Bin unreachable: delete permanently", seed["delete_mode"] == "delete", seed)
mounts.MOUNTINFO = real_mi
mounts.TRASH_DIR = proc
r = c.post("/api/run-folder", json={"path": "/nope/missing"})
check("one-off: missing folder refused", r.status_code == 400)
r = c.post("/api/run-folder", json={"path": cfg})
check("one-off: app data folder refused", r.status_code == 400)
pick = os.path.join(dl, "pick")
os.makedirs(pick)
import zipfile
with zipfile.ZipFile(os.path.join(pick, "x.zip"), "w") as z:
    z.writestr("x.txt", "x")
r = c.post("/api/run-folder", json={"path": pick, "recursive": False})
main.engine.thread.join(20)
rec = main.engine.index_get(os.path.join(os.path.realpath(pick), "x.zip"))
check("one-off: scans the folder, changes nothing, indexes the archive", r.status_code == 200
      and os.path.isfile(os.path.join(pick, "x.zip")) and not os.path.exists(os.path.join(pick, "x.txt"))
      and main.engine.history[-1]["trigger"] == "one-off" and main.engine.history[-1]["type"] == "scan"
      and rec is not None and rec["type"] == "zip", (r.get_json(), main.engine.history[-1]))

# --- folder picker: only mapped folders, never container folders
d = c.get("/api/browse?path=/").get_json()
check("browse: top level lists the mapped folders only (no app data)", d.get("top") and set(d["dirs"]) == {dl, media, proc}, d)
d = c.get("/api/browse?path=/etc").get_json()
check("browse: container folders are not browsable", d.get("top") and "/etc" not in d["dirs"] and "passwd" not in str(d), d)
d = c.get("/api/browse?path=" + cfg).get_json()
check("browse: app data folder is not browsable", d.get("top"), d)
d = c.get("/api/browse?path=" + dl).get_json()
check("browse: up from a mapped folder goes back to the list", d["parent"] == "" and not d.get("top"), d)
os.symlink("/etc", os.path.join(dl, "escape"))
d = c.get("/api/browse?path=" + dl).get_json()
check("browse: links leading outside are hidden", "escape" not in d["dirs"], d)
d = c.get("/api/browse?path=" + os.path.join(dl, "escape")).get_json()
check("browse: and can't be opened", d.get("top"), d)
r = c.post("/api/run-folder", json={"path": "/etc"})
check("one-off: folder outside the mapped folders refused", r.status_code == 400 and "mapped" in r.get_json()["error"], r.get_json())

print("\nALL PASSED" if not FAILS else f"\nFAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
