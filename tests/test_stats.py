"""Lifetime statistics checks. Run:  python3 -I tests/test_stats.py"""
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))
from stats import Stats, archive_type  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


work = tempfile.mkdtemp(prefix="ad-stats-")
path = os.path.join(work, "stats.json")
st = Stats(path)
# scanned(path, entries, size, seconds, outdated=False, oldest_year=None)
st.scanned("/src/a.rar", 10, 4000, 2, True, 2009)
st.scanned("/src/b.zip", 3, 2_000_000, 1, False, 2020)
st.scanned("/src/c.7z", 7, 100_000_000, 4, True, None)
st.renamed(2)
st.deleted(1000, "recycle")
st.deleted(500_000, "move")
st.deleted(40_000_000, "delete")
st.failed()
st.run_finished()
snap = st.snapshot()
t = snap["totals"]
check("archives/entries/bytes counted", t["archives"] == 3 and t["entries"] == 20 and t["bytes"] == 102_004_000
      and t["outdated"] == 2 and t["seconds"] == 7, t)
check("renamed and deleted", t["renamed"] == 2 and t["deleted"] == 3 and t["freed"] == 40_501_000, t)
check("where deleted archives went", t["recycled"] == 1000 and t["moved"] == 500_000 and t["removed"] == 40_000_000, t)
check("failures and runs", t["failed"] == 1 and t["runs"] == 1, t)
check("per type", snap["types"] == {"rar": 1, "zip": 1, "7z": 1} and snap["type_bytes"]["7z"] == 100_000_000,
      (snap["types"], snap["type_bytes"]))
check("archive_type", archive_type("x.7z.001") == "7z" and archive_type("x.tar.gz") == "tar")
day = snap["days"][time.strftime("%Y-%m-%d")]
check("per day", day["archives"] == 3 and day["entries"] == 20 and day["renamed"] == 2 and day["deleted"] == 3
      and day["failed"] == 1, day)
r = snap["records"]
check("records", r["largest"]["value"] == 100_000_000 and r["largest"]["path"] == "/src/c.7z"
      and r["most_entries"]["value"] == 10 and r["oldest_year"]["value"] == 2009
      and r["oldest_year"]["path"] == "/src/a.rar", r)
check("snapshot is a copy", snap is not st.data and snap["totals"] is not st.data["totals"])

st2 = Stats(path)
check("persisted across a reload", st2.snapshot()["totals"] == t and st2.snapshot()["records"]["largest"]["value"] == 100_000_000)
check("file mode is 0644", stat.S_IMODE(os.stat(path).st_mode) == 0o644, oct(os.stat(path).st_mode))

# concurrent saves: one file, always valid, no temp files left behind
errors = []


def hammer(n):
    try:
        for i in range(40):
            st2.scanned(f"/src/t{n}-{i}.zip", 1, 10, 0)
            st2.save()
    except Exception as e:  # noqa: BLE001
        errors.append(e)


ths = [threading.Thread(target=hammer, args=(n,)) for n in range(8)]
for th in ths:
    th.start()
for th in ths:
    th.join()
st2.save()
try:
    with open(path) as f:
        saved = json.load(f)
    valid = saved["totals"]["archives"] == 3 + 8 * 40
except (OSError, ValueError, KeyError) as e:
    valid, saved = False, e
check("concurrent saves leave a complete, valid file", not errors and valid, (errors, saved if not valid else ""))
check("no temp files left behind", sorted(os.listdir(work)) == ["stats.json"], os.listdir(work))

# a damaged or odd file never stops the app
for name, content in (("not json", "{nope"), ("list", "[1, 2]"), ("empty", "")):
    open(path, "w").write(content)
    check(f"unreadable file ({name}) starts blank", Stats(path).snapshot()["totals"]["archives"] == 0)
open(path, "w").write(json.dumps({"since": "x", "totals": [1], "types": 5, "type_bytes": {"zip": "x", "rar": 3},
                                  "days": {"2026-01-01": "bad", "2026-01-02": {"archives": "n"}},
                                  "records": {"largest": 7, "most_files": {"value": 3, "path": "/p", "unit": "files", "at": 1}}}))
odd = Stats(path)
odd.scanned("/x.zip", 1, 1, 0)
odd.failed()
s = odd.snapshot()
check("wrong-typed keys are replaced, valid parts kept", s["totals"]["archives"] == 1 and isinstance(s["since"], float)
      and "2026-01-01" not in s["days"] and s["days"]["2026-01-02"]["archives"] == 0 and s["records"]["largest"]["value"] == 1
      and s["records"]["most_files"]["value"] == 3 and s["type_bytes"] == {"rar": 3, "zip": 1} and s["types"] == {"zip": 1}, s)
open(path, "w").write(json.dumps({"totals": {"archives": "many", "entries": 4}}))
check("wrong-typed counters become 0", Stats(path).snapshot()["totals"]["archives"] == 0
      and Stats(path).snapshot()["totals"]["entries"] == 4)

st2.reset()
check("reset", st2.snapshot()["totals"]["archives"] == 0 and not st2.snapshot()["records"]
      and Stats(path).snapshot()["totals"]["archives"] == 0)
shutil.rmtree(work)

print("\nALL PASSED" if not FAILS else f"\nFAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
