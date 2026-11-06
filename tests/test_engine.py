"""Smoke test of the placeholder engine (scan flow, index, remembered state, disposal helpers).
Run:  python3 -I tests/test_engine.py

The real listing / rename / delete behaviour is tested by the engine agent in new files (test_formats.py,
test_actions.py ...); this file only proves the scaffold runs end to end."""
import os
import shutil
import sys
import tempfile
import time
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import engine as E  # noqa: E402
from engine import Engine  # noqa: E402
import settings  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def base_cfg(**kw):
    cfg = dict(settings.DEFAULTS)
    cfg.update(min_age_minutes=0, threads=3)
    cfg.update(kw)
    clean, errors = settings.validate(cfg)
    assert not errors, errors
    return clean


def run(engine, cfg, trigger="test", oneoff=None):
    assert engine.start(cfg, trigger, oneoff)
    engine.thread.join(60)
    assert not engine.running
    return engine.snapshot()


def touch(path, data=b"x", age_days=0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    if age_days:
        t = time.time() - age_days * 86400
        os.utime(path, (t, t))


def make_zip(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("a.txt", "hello")


work = tempfile.mkdtemp(prefix="ad-engine-")
data, src, trash = (os.path.join(work, d) for d in ("data", "src", "trash"))
for d in (data, src, trash):
    os.makedirs(d)
eng = Engine(os.path.join(data, "logs"), os.path.join(data, "history.json"))

# --- names and volume sets
check("archive names", all(E.is_archive(n) for n in ("a.zip", "a.RAR", "a.7z", "a.tar.gz", "a.tgz", "a.tar.bz2", "a.tar.xz", "a.7z.001"))
      and not E.is_archive("a.txt") and not E.is_archive(".autodate-work"))
check("secondary volumes", E.is_secondary_volume("a.part2.rar") and E.is_secondary_volume("a.7z.002")
      and not E.is_secondary_volume("a.part1.rar") and not E.is_secondary_volume("a.7z.001") and not E.is_secondary_volume("a.rar"))
for n in ("m.part1.rar", "m.part2.rar", "m.part3.rar", "r.rar", "r.r00", "r.r01", "z.zip", "z.z01", "z.z02", "s.7z.001", "s.7z.002", "t.tar.gz"):
    touch(os.path.join(src, "sets", n))
g = lambda n: sorted(os.path.basename(x) for x in eng._archive_group(os.path.join(src, "sets", n)))
check("volume sets: part rar", g("m.part1.rar") == ["m.part1.rar", "m.part2.rar", "m.part3.rar"], g("m.part1.rar"))
check("volume sets: rar + r00", g("r.rar") == ["r.r00", "r.r01", "r.rar"], g("r.rar"))
check("volume sets: zip + z01", g("z.zip") == ["z.z01", "z.z02", "z.zip"], g("z.zip"))
check("volume sets: 7z.001", g("s.7z.001") == ["s.7z.001", "s.7z.002"], g("s.7z.001"))
check("volume sets: tar.gz alone", g("t.tar.gz") == ["t.tar.gz"])
sig = eng._signature(os.path.join(src, "sets", "r.rar"))
check("signature = [size, newest mtime]", sig[0] == 3 and sig[1] > 0, sig)
touch(os.path.join(src, "sets", "r.r01"), b"yy")
check("signature changes with the set", eng._signature(os.path.join(src, "sets", "r.rar")) != sig)
shutil.rmtree(os.path.join(src, "sets"))

# --- a scan: finds first volumes only, skips excluded folders, fills the index
make_zip(os.path.join(src, "one.zip"))
make_zip(os.path.join(src, "sub", "two.zip"))
touch(os.path.join(src, "sub", "big.part1.rar"))
touch(os.path.join(src, "sub", "big.part2.rar"))
make_zip(os.path.join(src, "@eaDir", "skipme.zip"))
make_zip(os.path.join(src, ".Recycle.Bin", "skipme.zip"))
make_zip(os.path.join(src, ".autodate-work", "skipme.zip"))
cfg = base_cfg(sources=[src])
snap = run(eng, cfg)
check("status shape", {"state", "found", "done", "remaining", "listed", "entries", "outdated", "failed", "cancelled",
                       "skipped", "unchanged", "deferred", "waiting", "failures", "slots", "tools"} <= set(snap), sorted(snap))
check("found the first volumes only, nothing in excluded folders", snap["found"] == 3 and snap["result"] == "completed", snap)
check("all listed, none left", snap["listed"] == 3 and snap["remaining"] == 0 and snap["state"] == "idle", snap)
idx = eng.index_load()
check("index holds one record per archive set", sorted(os.path.basename(p) for p in idx) == ["big.part1.rar", "one.zip", "two.zip"], list(idx))
rec = idx[os.path.join(src, "sub", "big.part1.rar")]
check("record shape", {"path", "volumes", "type", "archive_date", "size", "sig", "status", "error", "entry_count", "file_count",
                       "folder_count", "oldest", "newest", "oldest_any", "newest_any", "age_years", "age_years_oldest",
                       "encrypted", "partial", "truncated", "entries"} <= set(rec)
      and rec["type"] == "rar" and len(rec["volumes"]) == 2 and rec["size"] == 2 and rec["status"] == "ok", rec)
check("index file saved atomically", os.path.isfile(os.path.join(data, "index.json"))
      and not os.path.exists(os.path.join(data, "index.json.tmp")))
check("history entry is a scan", eng.history[-1]["type"] == "scan" and eng.history[-1]["listed"] == 3)
check("stats counted the scan", eng.stats.snapshot()["totals"]["archives"] == 3 and eng.stats.snapshot()["totals"]["runs"] == 1)
check("log file is autodate.log", os.path.isfile(os.path.join(data, "logs", "autodate.log")))

# a fresh engine reads the same index back; unchanged archives are not listed again
eng2 = Engine(os.path.join(data, "logs"), os.path.join(data, "history.json"))
check("index survives a restart", len(eng2.index_load()) == 3)
snap = run(eng2, cfg)
check("second scan: unchanged archives are not listed again", snap["unchanged"] == 3 and snap["listed"] == 0 and snap["remaining"] == 0, snap)
make_zip(os.path.join(src, "one.zip"))
os.utime(os.path.join(src, "one.zip"), (time.time() - 100, time.time() - 100))
snap = run(eng2, cfg)
check("a changed archive is listed again", snap["listed"] == 1 and snap["unchanged"] == 2, snap)

# --- remembered failures: scheduled scans skip them until the signature changes, Run now retries
bad = os.path.join(src, "bad.zip")
touch(bad, b"not a zip")
real_list = eng2.list_archive


def failing(path, cfg, slot=None):
    r = real_list(path, cfg, slot)
    if path == bad:
        r.update(status="failed", error="damaged")
    return r


eng2.list_archive = failing
snap = run(eng2, cfg, "manual")
check("failure is reported and stored in the index", snap["failed"] == 1 and eng2.index_get(bad)["status"] == "failed"
      and snap["failures"][0]["error"] == "damaged", snap["failures"])
snap = run(eng2, cfg, "schedule")
check("scheduled scan skips a remembered failure", snap["skipped"] == 1 and snap["failed"] == 0, snap)
snap = run(eng2, cfg, "manual")
check("Run now retries it", snap["failed"] == 1 and snap["skipped"] == 0, snap)
touch(bad, b"changed content")
snap = run(eng2, cfg, "schedule")
check("a changed signature is retried by scheduled scans", snap["failed"] == 1 and snap["skipped"] == 0, snap)
eng2.list_archive = real_list
snap = run(eng2, cfg, "manual")
check("it is listed fine once it lists fine", snap["failed"] == 0 and eng2.index_get(bad)["status"] == "ok", snap)

# --- min age: recent archives wait
for cur, _, files in os.walk(src):                   # everything made so far is old enough ...
    for fn in files:
        t = time.time() - 3600
        os.utime(os.path.join(cur, fn), (t, t))
fresh = os.path.join(src, "fresh", "new.zip")        # ... except this one
make_zip(fresh)
snap = run(eng2, base_cfg(sources=[src], min_age_minutes=5))
check("recent archive is deferred and listed in the waiting list", snap["deferred"] == 1 and snap["waiting"]
      and snap["waiting"][0]["dir"] == os.path.dirname(fresh) and snap["waiting"][0]["ready_at"] > time.time(), snap["waiting"])
check("deferred archive is not in the index", eng2.index_get(fresh) is None)

# --- one-off job: refuses "/", honours recursive
snap = run(eng2, cfg, "one-off", {"path": "/", "recursive": True})
check("one-off refuses /", snap["result"] == "error" and "root folder" in snap["message"], snap["message"])
snap = run(eng2, base_cfg(sources=[src]), "one-off", {"path": os.path.join(src, "sub"), "recursive": False})
check("one-off scans just that folder", snap["found"] == 2 and snap["result"] == "completed", snap)

# --- stop / cancel when idle
check("stop when idle", eng2.stop() is False)
ok, why = eng2.cancel_job(os.path.join(src, "one.zip"))
check("cancel of an archive that is not being scanned is refused", not ok and why, why)

# --- stop and cancel while running (a slow listing)
release = []


def slow(path, cfg, slot=None):
    while not release and not eng2._halted(slot):
        time.sleep(0.02)
    if eng2._halted(slot):
        raise E.Halted()
    return real_list(path, cfg, slot)


eng2.list_archive = slow
for p in list(eng2.index_load()):
    eng2.index_drop([p])
assert eng2.start(cfg, "manual")
time.sleep(0.5)
busy = [s for s in eng2.snapshot()["slots"] if s["path"]]
check("threads board shows what is being read", len(busy) == 3, busy)
target = busy[0]["path"]
ok, _ = eng2.cancel_job(target)
time.sleep(0.5)
check("cancel hits only that archive", ok and eng2.snapshot()["cancelled"] == 1 and eng2.running, eng2.snapshot()["cancelled"])
check("a cancelled archive is remembered", eng2._remembered_why(target) == "was cancelled"
      and os.path.isfile(os.path.join(data, "cancelled-archives.json")))
check("stop", eng2.stop() is True)
eng2.thread.join(20)
check("stopped run ends as stopped", not eng2.running and eng2.status["result"] == "stopped", eng2.status["result"])
eng2.list_archive = real_list
snap = run(eng2, cfg, "schedule")
check("scheduled scan skips the cancelled archive", snap["skipped"] == 1, snap)
snap = run(eng2, cfg, "manual")
check("Run now lists it again and forgets the cancel", eng2._remembered_why(target) == "" and eng2.index_get(target), snap)

# --- disposal helpers (whole volume set): move folder layout, name clashes, permanent delete
for n in ("v.part1.rar", "v.part2.rar"):
    touch(os.path.join(src, "del", n))
mcfg = base_cfg(sources=[src], delete_mode="move", move_dir=trash)
check("move: volume set moves together", eng2._dispose_archive(os.path.join(src, "del", "v.part1.rar"), src, mcfg)
      and sorted(os.listdir(os.path.join(trash, "src", "del"))) == ["v.part1.rar", "v.part2.rar"]
      and not os.listdir(os.path.join(src, "del")))
for n in ("v.part1.rar", "v.part2.rar"):
    touch(os.path.join(src, "del", n))
eng2._dispose_archive(os.path.join(src, "del", "v.part1.rar"), src, mcfg)
check("move: name clash gets .1", os.path.isfile(os.path.join(trash, "src", "del", "v.part1.rar.1")))
touch(os.path.join(src, "del", "gone.zip"))
dcfg = base_cfg(sources=[src], delete_mode="delete")
check("delete: removed for good", eng2._dispose_archive(os.path.join(src, "del", "gone.zip"), src, dcfg)
      and not os.path.exists(os.path.join(src, "del", "gone.zip")))
check("_root_for finds the source", eng2._root_for(os.path.join(src, "sub", "two.zip"), dcfg) == os.path.realpath(src))

# --- history for actions
eng2.add_action_history("delete", [{"path": "a", "ok": True}, {"path": "b", "ok": False, "reason": "x"}], size=10)
h = eng2.history[-1]
check("action history entry", h["type"] == "delete" and h["count"] == 1 and h["failed"] == 1 and h["bytes"] == 10, h)

# --- outdated rule
now = time.time()
yr = 365.25 * 86400
rec = {"newest": now - 3 * yr, "oldest": now - 12 * yr}
check("outdated: by newest / by oldest / no dates",
      not E.Engine.is_outdated(rec, {"outdated_years": 5, "outdated_by": "newest"})
      and E.Engine.is_outdated(rec, {"outdated_years": 5, "outdated_by": "oldest"})
      and not E.Engine.is_outdated({"newest": None, "oldest": None}, {"outdated_years": 5, "outdated_by": "newest"}))

# --- the TODO stubs say so
for name, args in (("index_query", ()), ("check_action_paths", ([], cfg)), ("delete_archives", ([], cfg)),
                   ("rename_archives", ([], "newest", cfg)), ("rescan", ([], cfg))):
    try:
        getattr(eng2, name)(*args)
        raised = False
    except NotImplementedError:
        raised = True
    check(f"stub {name} is marked TODO", raised)

shutil.rmtree(work)
print("\nALL PASSED" if not FAILS else f"\nFAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
