"""Rename / delete / rescan actions on real archives.
Run:  python3 -I tests/test_actions.py     (exit 2 when a needed tool is missing)"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import engine as E  # noqa: E402
from engine import Engine  # noqa: E402
import settings  # noqa: E402

for tool in ("rar", "unrar", "7z", "zip", "tar"):
    if not shutil.which(tool):
        print(f"MISSING TOOL: {tool}")
        sys.exit(2)

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


work = tempfile.mkdtemp(prefix="ad-actions-")
data, src, trash, other = (os.path.join(work, d) for d in ("data", "src", "trash", "other"))
for d in (data, src, trash, other):
    os.makedirs(d)
# the "mapped folders" of this test: src and trash; "other" is NOT mapped
E.mounts.list_mounts = lambda config_dir="/data": [{"path": p, "config": False, "writable": True} for p in (src, trash)]
eng = Engine(os.path.join(data, "logs"), os.path.join(data, "history.json"))


def cfg_for(**kw):
    c = dict(settings.DEFAULTS)
    c.update(min_age_minutes=0, threads=2, sources=[src], delete_mode="move", move_dir=trash)
    c.update(kw)
    return c


def epoch(s):
    return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))


OLD, NEW = "2009-03-04 05:06:07", "2014-11-12 13:14:15"
tree = os.path.join(work, "tree")
os.makedirs(tree)
for name, when in (("old.bin", OLD), ("new.bin", NEW)):
    with open(os.path.join(tree, name), "wb") as f:
        f.write(os.urandom(30000))
    os.utime(os.path.join(tree, name), (epoch(when),) * 2)


def sh(cmd):
    subprocess.run(cmd, cwd=tree, check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def age(folder):
    t = time.time() - 7200
    for f in os.listdir(folder):
        os.utime(os.path.join(folder, f), (t, t))


def fresh(sub):
    d = os.path.join(src, sub)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    return d


def scan(c=None):
    c = c or cfg_for()
    assert eng.start(c, "manual")
    eng.thread.join(120)
    assert not eng.running
    return eng.snapshot()


def names(d):
    return sorted(os.listdir(d))


# one folder per naming style
styles = {}
d = fresh("single")
sh(["zip", "-q", os.path.join(d, "one.zip"), "old.bin", "new.bin"])
styles["single zip"] = (d, "one.zip", ["one (2014).zip"], ["one.zip"])
d = fresh("tgz")
sh(["tar", "czf", os.path.join(d, "ball.tar.gz"), "old.bin", "new.bin"])
sh(["tar", "cJf", os.path.join(d, "ball2.tar.xz"), "old.bin", "new.bin"])
sh(["tar", "czf", os.path.join(d, "short.tgz"), "old.bin", "new.bin"])
d = fresh("part")
sh(["rar", "a", "-ma5", "-idq", "-v20k", os.path.join(d, "multi.rar"), "old.bin", "new.bin"])
nparts = len(os.listdir(d))
d = fresh("sevenz")
sh(["7z", "a", "-v20k", os.path.join(d, "set.7z"), "old.bin", "new.bin"])
d = fresh("splitzip")
big = os.path.join(tree, "big.bin")
with open(big, "wb") as f:
    f.write(os.urandom(150000))
os.utime(big, (epoch(OLD),) * 2)
sh(["zip", "-q", "-s", "70k", os.path.join(d, "sp.zip"), "big.bin"])
os.remove(big)
d = fresh("replace")
sh(["zip", "-q", os.path.join(d, "film (2001).zip"), "old.bin", "new.bin"])
sh(["zip", "-q", os.path.join(d, "same (2014).zip"), "old.bin", "new.bin"])
sh(["zip", "-q", os.path.join(d, "dated.zip"), "old.bin", "new.bin"])
sh(["zip", "-q", os.path.join(d, "dated (2014).zip"), "old.bin"])
sh(["zip", "-q", os.path.join(d, "clash.zip"), "old.bin", "new.bin"])
shutil.copy(os.path.join(d, "clash.zip"), os.path.join(d, "clash (2014).zip"))     # target exists (and is an archive)
d = fresh("nodate")
with zipfile.ZipFile(os.path.join(d, "nodate.zip"), "w") as zf:
    zf.writestr(zipfile.ZipInfo("x", (1980, 1, 1, 0, 0, 0)), "x")
for sub in os.listdir(src):
    age(os.path.join(src, sub))
snap = scan()
idx = eng.index_load()
check("scan found every set", snap["failed"] == 0 and len(idx) == 14, (snap["failures"], sorted(idx)))
P = lambda *a: os.path.join(src, *a)
# the old "name.rar + name.r00 .." style cannot be made with RAR 7, so this set is made of placeholder files
# with a hand-made index record (renaming and deleting never read the archive)
d = fresh("oldrar")
for n in ("legacy.rar", "legacy.r00", "legacy.r01"):
    with open(os.path.join(d, n), "wb") as f:
        f.write(b"x" * 100)
age(d)
rec = E.blank_record(P("oldrar", "legacy.rar"), eng._archive_group(P("oldrar", "legacy.rar")), eng._signature(P("oldrar", "legacy.rar")))
rec.update(oldest=OLD, newest=NEW, oldest_any=OLD, newest_any=NEW)
eng.index_put(rec)
sig_before = {p: dict(r) for p, r in idx.items()}


def rename(paths, use="newest", preview=False, c=None):
    return eng.rename_archives(paths, use, c or cfg_for(), preview=preview)


def files_state():
    out = {}
    for cur, _d, fs in os.walk(src):
        for f in fs:
            p = os.path.join(cur, f)
            out[p] = os.path.getsize(p)
    return out


# --- preview changes nothing, tells exactly what would happen
before = files_state()
pv = rename([P("part", "multi.part1.rar"), P("tgz", "ball.tar.gz")], preview=True)
r0 = pv["results"][0]
check("rename preview: new names, year, nothing changed", files_state() == before and r0["ok"] and r0["year"] == 2014
      and r0["new_path"] == P("part", "multi (2014).part1.rar") and len(r0["renamed"]) == nparts and pv["count"] == 2
      and eng.index_get(P("part", "multi.part1.rar")) is not None, pv)
check("rename preview: oldest gives the other year", rename([P("tgz", "ball.tar.gz")], "oldest", True)["results"][0]["year"] == 2009)
check("rename preview does not touch stats or history", eng.stats.snapshot()["totals"]["renamed"] == 0 and eng.history[-1]["type"] == "scan")

# --- real renames, every naming style
res = rename([P("single", "one.zip")])["results"][0]
check("single zip renamed", res["ok"] and os.path.isfile(P("single", "one (2014).zip")) and not os.path.exists(P("single", "one.zip")), res)
rec = eng.index_get(P("single", "one (2014).zip"))
check("index entry moved, signature fresh", rec and eng.index_get(P("single", "one.zip")) is None and rec["path"] == P("single", "one (2014).zip")
      and rec["volumes"] == [P("single", "one (2014).zip")] and rec["sig"] == eng._signature(rec["path"]) and rec["entry_count"] == 2, rec)

res = rename([P("tgz", "ball.tar.gz"), P("tgz", "ball2.tar.xz"), P("tgz", "short.tgz")], "oldest")["results"]
check("tar.gz / tar.xz / tgz keep their double extension", all(r["ok"] for r in res) and names(P("tgz")) ==
      ["ball (2009).tar.gz", "ball2 (2009).tar.xz", "short (2009).tgz"], names(P("tgz")))

res = rename([P("part", "multi.part1.rar")])["results"][0]
parts_now = names(P("part"))
check(".partN.rar: every part renamed", res["ok"] and len(parts_now) == nparts and all(n.startswith("multi (2014).part") for n in parts_now), parts_now)
check(".partN.rar: volumes in the index follow", sorted(os.path.basename(v) for v in eng.index_get(P("part", "multi (2014).part1.rar"))["volumes"]) == parts_now)

leg = names(P("oldrar"))
res = rename([P("oldrar", "legacy.rar")])["results"][0]
now = names(P("oldrar"))
check(".rar + .r00 style: all volumes renamed", res["ok"] and now == sorted(n.replace("legacy", "legacy (2014)") for n in leg), (leg, now, res))

leg = names(P("sevenz"))
res = rename([P("sevenz", "set.7z.001")])["results"][0]
now = names(P("sevenz"))
check(".7z.001 style: all volumes renamed", res["ok"] and now == sorted(n.replace("set", "set (2014)") for n in leg) and "set (2014).7z.001" in now, (leg, now, res))

leg = names(P("splitzip"))
res = rename([P("splitzip", "sp.zip")])["results"][0]
now = names(P("splitzip"))
check(".zip + .z01 style: all volumes renamed", res["ok"] and now == sorted(n.replace("sp", "sp (2009)") for n in leg) and "sp (2009).z01" in now, (leg, now, res))
r = eng.list_archive(P("splitzip", "sp (2009).zip"), cfg_for())
check("a renamed set can still be listed", r["status"] == "ok" and r["file_count"] == 1, r["error"])

res = rename([P("replace", "film (2001).zip")])["results"][0]
check("an existing (YYYY) is replaced, not doubled", res["ok"] and os.path.isfile(P("replace", "film (2014).zip"))
      and not os.path.exists(P("replace", "film (2001) (2014).zip")), names(P("replace")))
check("rename updated stats and history", eng.stats.snapshot()["totals"]["renamed"] == 1 + 3 + 1 + 1 + 1 + 1 + 1
      and eng.history[-1]["type"] == "rename" and eng.history[-1]["count"] == 1)

# --- refusals
res = rename([P("replace", "same (2014).zip"), P("nodate", "nodate.zip"), P("replace", "clash.zip")])["results"]
check("no change refused with reason", not res[0]["ok"] and "nothing to change" in res[0]["reason"], res[0])
check("no usable date refused", not res[1]["ok"] and "no usable" in res[1]["reason"], res[1])
check("clash refused, nothing renamed", not res[2]["ok"] and "already exists" in res[2]["reason"] and os.path.isfile(P("replace", "clash.zip")), res[2])
res = rename([P("replace", "dated.zip")], "newest")["results"][0]
check("target already used by another archive in the folder is refused", not res["ok"] and "already exists" in res["reason"], res)

# --- rollback when a later volume cannot be renamed
leg = names(P("oldrar"))
d = fresh("rollback")
sh(["rar", "a", "-ma5", "-idq", "-v20k", os.path.join(d, "rb.rar"), "old.bin", "new.bin"])
age(d)
scan()
first = P("rollback", "rb.part1.rar")
orig = names(d)
real_rename = eng._rename_file
calls = []


def flaky(old, new):
    calls.append(old)
    if len(calls) == 3:
        raise OSError("disk says no")
    return real_rename(old, new)


eng._rename_file = flaky
res = rename([first])["results"][0]
eng._rename_file = real_rename
check("rollback: failure reported, every volume back under its old name", not res["ok"] and "disk says no" in res["reason"]
      and names(d) == orig and eng.index_get(first) is not None and len(calls) >= 5, (res, names(d), orig))
check("rollback: nothing counted", eng.history[-1]["count"] == 0 and eng.history[-1]["type"] == "rename")

# --- guards shared by all actions
outside = os.path.join(other, "o.zip")
with zipfile.ZipFile(outside, "w") as zf:
    zf.writestr(zipfile.ZipInfo("x", (2001, 1, 1, 0, 0, 0)), "x")
eng.index_put(E.blank_record(outside, [outside], eng._signature(outside)) | {"newest": "2001-01-01 00:00:00", "oldest": "2001-01-01 00:00:00"})
ok, refused = eng.check_action_paths([outside, P("nope.zip"), "relative", 5, P("replace", "film (2014).zip")], cfg_for())
reasons = {r["path"]: r["reason"] for r in refused}
check("check_action_paths: outside roots / not in index / junk refused", ok == [P("replace", "film (2014).zip")]
      and "mapped" in reasons[outside] and reasons[P("nope.zip")] == "not in the index" and reasons["relative"] and reasons["5"], reasons)
for fn, args in ((rename, ([outside],)), (lambda p: eng.delete_archives(p, cfg_for(), preview=False), ([outside],))):
    r = fn(*args)["results"][0]
    check("action refuses a path outside the mapped folders", not r["ok"] and "mapped" in r["reason"] and os.path.isfile(outside), r)
r = eng.delete_archives([P("never-scanned.zip")], cfg_for(), preview=False)["results"][0]
check("delete refuses a path that is not in the index", not r["ok"] and r["reason"] == "not in the index")
# changed since the scan
victim = P("replace", "film (2014).zip")
with open(victim, "ab") as f:
    f.write(b"more")
for r in (rename([victim])["results"][0], eng.delete_archives([victim], cfg_for(), preview=False)["results"][0]):
    check("changed since the scan -> skipped", not r["ok"] and "changed since it was scanned" in r["reason"] and os.path.isfile(victim), r)

# --- rescan fixes it
rs = eng.rescan([victim, P("nope.zip")], cfg_for())["results"]
check("rescan re-lists a changed archive, refuses unknown ones", rs[0]["ok"] and not rs[1]["ok"] and eng.index_get(victim)["sig"] == eng._signature(victim)
      and eng.snapshot()["state"] == "idle" and not eng.running, rs)

# --- actions refuse while a scan runs
release = []
real_list = eng.list_archive


def slow(path, cfg, slot=None):
    while not release and not eng._halted(slot):
        time.sleep(0.02)
    return real_list(path, cfg, slot)


eng.list_archive = slow
os.utime(victim, (time.time() - 3000, time.time() - 3000))
assert eng.start(cfg_for(), "manual")
time.sleep(0.3)
for label, r in (("rename", rename([victim])), ("rename preview", rename([victim], preview=True)),
                 ("delete", eng.delete_archives([victim], cfg_for(), preview=False)), ("rescan", eng.rescan([victim], cfg_for()))):
    check(f"{label} refused while a scan is running", all(x["reason"] == "a scan is running" and not x["ok"] for x in r["results"]) and r["results"], r)
release.append(1)
eng.thread.join(60)
eng.list_archive = real_list

# --- delete: preview, then the three modes on volume sets
snap = scan()
before = files_state()
trash_before = names(trash)
vols = names(P("sevenz"))
pv = eng.delete_archives([P("sevenz", "set (2014).7z.001"), P("single", "one (2014).zip")], cfg_for(), preview=True)
r0 = pv["results"][0]
check("delete preview: where it would go, nothing happens", files_state() == before and names(trash) == trash_before and r0["ok"]
      and r0["action"] == "move" and r0["target"].startswith(trash) and r0["bytes"] == sum(os.path.getsize(P("sevenz", v)) for v in vols)
      and pv["count"] == 2 and eng.index_get(P("sevenz", "set (2014).7z.001")) is not None, pv)
check("delete preview does not touch stats or history", eng.stats.snapshot()["totals"]["deleted"] == 0 and eng.history[-1]["type"] != "delete")

res = eng.delete_archives([P("sevenz", "set (2014).7z.001")], cfg_for(), preview=False)
r0 = res["results"][0]
moved = os.path.join(trash, os.path.basename(src), "sevenz")
check("move mode: the whole volume set moved", r0["ok"] and names(P("sevenz")) == [] and names(moved) == vols
      and r0["target"] == os.path.join(moved, "set (2014).7z.001") and res["count"] == 1 and res["bytes"] == r0["bytes"], (res, names(moved)))
check("delete: out of the index, logged, history and stats", eng.index_get(P("sevenz", "set (2014).7z.001")) is None
      and eng.history[-1]["type"] == "delete" and eng.history[-1]["count"] == 1 and eng.history[-1]["bytes"] == r0["bytes"]
      and eng.stats.snapshot()["totals"]["deleted"] == 1 and eng.stats.snapshot()["totals"]["moved"] == r0["bytes"]
      and any("Deleted" in e["msg"] for e in eng.log_since(0, 2000)))
idx_file = E.json.load(open(os.path.join(data, "index.json")))
check("delete: index file saved", P("sevenz", "set (2014).7z.001") not in idx_file)

# recycle: needs a share with a reachable .Recycle.Bin
share = os.path.join(work, "share")
os.makedirs(os.path.join(share, "dl"))
saved_rt = E.mounts.recycle_target
E.mounts.recycle_target = lambda p, m: (os.path.join(share, ".Recycle.Bin", os.path.relpath(os.path.dirname(p), src)), "")
set_vols = names(P("part"))
res = eng.delete_archives([P("part", "multi (2014).part1.rar")], cfg_for(delete_mode="recycle"), preview=True)
check("recycle preview", res["results"][0]["ok"] and res["results"][0]["action"] == "recycle" and ".Recycle.Bin" in res["results"][0]["target"]
      and names(P("part")) == set_vols)
res = eng.delete_archives([P("part", "multi (2014).part1.rar")], cfg_for(delete_mode="recycle"), preview=False)
rb = os.path.join(share, ".Recycle.Bin", "part")
check("recycle mode: whole set in the Recycle Bin layout", res["results"][0]["ok"] and names(rb) == set_vols and names(P("part")) == []
      and eng.stats.snapshot()["totals"]["recycled"] > 0, (res, os.path.isdir(rb)))
# a clash in the bin gets .1
shutil.copy(os.path.join(rb, set_vols[0]), P("part", set_vols[0]))
for v in set_vols[1:]:
    shutil.copy(os.path.join(rb, v), P("part", v))
eng.index_put(E.blank_record(P("part", set_vols[0]), eng._archive_group(P("part", set_vols[0])), eng._signature(P("part", set_vols[0]))))
res = eng.delete_archives([P("part", set_vols[0])], cfg_for(delete_mode="recycle"), preview=False)
check("recycle mode: name clash gets .1", res["results"][0]["ok"] and os.path.isfile(os.path.join(rb, set_vols[0] + ".1")), names(rb))
E.mounts.recycle_target = lambda p, m: (None, "no Recycle Bin on this share")
res = eng.delete_archives([P("tgz", "ball (2009).tar.gz")], cfg_for(delete_mode="recycle"), preview=True)
check("recycle unusable is refused with the reason", not res["results"][0]["ok"] and "Recycle Bin not usable" in res["results"][0]["reason"], res)
E.mounts.recycle_target = saved_rt

before = names(P("oldrar"))
res = eng.delete_archives([P("oldrar", "legacy (2014).rar")], cfg_for(delete_mode="delete"), preview=True)
check("delete preview says permanently", res["results"][0]["ok"] and res["results"][0]["action"] == "delete"
      and "permanent" in res["results"][0]["target"] and names(P("oldrar")) == before)
res = eng.delete_archives([P("oldrar", "legacy (2014).rar")], cfg_for(delete_mode="delete"), preview=False)
check("delete mode: whole set removed for good", res["results"][0]["ok"] and names(P("oldrar")) == []
      and eng.stats.snapshot()["totals"]["removed"] == res["bytes"] > 0 and eng.index_get(P("oldrar", "legacy (2014).rar")) is None, res)

# a volume that cannot be moved: the ones moved already are put back
d = P("splitzip")
vs = names(d)
real_move = eng._move
n = []


def bad_move(s, root, c):
    n.append(s)
    if len(n) == 2:
        raise OSError("no space")
    return real_move(s, root, c)


eng._move = bad_move
res = eng.delete_archives([P("splitzip", "sp (2009).zip")], cfg_for(), preview=False)
eng._move = real_move
check("failed move puts the earlier volumes back", not res["results"][0]["ok"] and names(d) == vs and "no space" in res["results"][0]["reason"]
      and eng.index_get(P("splitzip", "sp (2009).zip")) is not None, res)

# the order of results follows the request; duplicates collapse
res = eng.delete_archives([P("zz.zip"), P("single", "one (2014).zip"), P("zz.zip")], cfg_for(), preview=True)
check("results follow the request order, duplicates once", [r["path"] for r in res["results"]] == [P("zz.zip"), P("single", "one (2014).zip")], res)

# --- preview and confirm are tied: a real run only does what the preview showed
d = fresh("tied")
sh(["zip", "-q", os.path.join(d, "t1.zip"), "old.bin", "new.bin"])
sh(["zip", "-q", os.path.join(d, "t2.zip"), "old.bin", "new.bin"])
sh(["zip", "-q", os.path.join(d, "t3.zip"), "old.bin", "new.bin"])
age(d)
scan()
T = lambda n: P("tied", n)
pv = eng.delete_archives([T("t1.zip")], cfg_for(delete_mode="move"), preview=True)
ex = pv["expect"]
check("delete preview returns expect {path: target}", ex == {T("t1.zip"): pv["results"][0]["target"]} and pv["results"][0]["target"].startswith(trash), pv)
res = eng.delete_archives([T("t1.zip")], cfg_for(delete_mode="delete"), preview=False, expect=ex)["results"][0]
check("delete: mode changed after the preview (move -> permanent) is refused, file untouched",
      not res["ok"] and "preview again" in res["reason"] and os.path.isfile(T("t1.zip")), res)
other_trash = os.path.join(work, "trash2")
os.makedirs(other_trash)
res = eng.delete_archives([T("t1.zip")], cfg_for(delete_mode="move", move_dir=other_trash), preview=False, expect=ex)["results"][0]
check("delete: move folder changed after the preview is refused", not res["ok"] and os.path.isfile(T("t1.zip")) and names(other_trash) == [], res)
res = eng.delete_archives([T("t1.zip"), T("t2.zip")], cfg_for(delete_mode="move"), preview=False, expect=ex)["results"]
check("delete: same settings -> done; an archive missing from expect is refused",
      res[0]["ok"] and not os.path.exists(T("t1.zip")) and not res[1]["ok"] and "preview again" in res[1]["reason"] and os.path.isfile(T("t2.zip")), res)
pv = rename([T("t2.zip")], "newest", preview=True)
check("rename preview returns expect {path: new path}", pv["expect"] == {T("t2.zip"): T("t2 (2014).zip")}, pv.get("expect"))
res = eng.rename_archives([T("t2.zip")], "oldest", cfg_for(), preview=False, expect=pv["expect"])["results"][0]
check("rename: another year than previewed is refused", not res["ok"] and "preview again" in res["reason"] and os.path.isfile(T("t2.zip")), res)
res = eng.rename_archives([T("t2.zip")], "newest", cfg_for(), preview=False, expect=pv["expect"])["results"][0]
check("rename: as previewed -> done", res["ok"] and os.path.isfile(T("t2 (2014).zip")), res)

# --- a rename never replaces a file that appears between the check and the rename
a, b = T("t3.zip"), T("t3 (2014).zip")
real_lexists = os.path.lexists
raced = []


def racing_lexists(path):
    r = real_lexists(path)
    if path == b and not raced:
        raced.append(1)
        with open(b, "wb") as f:                     # somebody creates the target right after the check
            f.write(b"precious")
    return r


E.os.path.lexists = racing_lexists
try:
    err = None
    try:
        eng._rename_file(a, b)
    except OSError as e:
        err = str(e)
finally:
    E.os.path.lexists = real_lexists
check("rename: target created in the race window is not replaced", err and "already exists" in err and open(b, "rb").read() == b"precious"
      and os.path.isfile(a), err)
os.remove(b)
eng._rename_file(a, b)
check("rename: plain rename works (link + unlink)", os.path.isfile(b) and not os.path.exists(a))
real_link = E.os.link
E.os.link = lambda *a_, **k: (_ for _ in ()).throw(OSError(E.errno.EPERM, "links not supported"))
try:
    eng._rename_file(b, a)
finally:
    E.os.link = real_link
check("rename: falls back to rename where hard links are not supported", os.path.isfile(a) and not os.path.exists(b))

# --- the move folder cannot be escaped when the source is reached through a symlink
real_src = os.path.join(work, "realsrc")
os.makedirs(os.path.join(real_src, "sub"))
link = os.path.join(work, "linksrc")
os.symlink(real_src, link)
with zipfile.ZipFile(os.path.join(real_src, "sub", "l.zip"), "w") as zf:
    zf.writestr(zipfile.ZipInfo("x", (2001, 1, 1, 0, 0, 0)), "x")
age(os.path.join(real_src, "sub"))
E.mounts.list_mounts = lambda config_dir="/data": [{"path": p, "config": False, "writable": True} for p in (src, trash, link)]
eng._refresh_mounts()
lc = cfg_for(sources=[link])
lp = os.path.join(link, "sub", "l.zip")          # an index record that kept the unresolved path
eng.index_put(E.blank_record(lp, [lp], eng._signature(lp)) | {"newest": "2001-01-01 00:00:00", "oldest": "2001-01-01 00:00:00"})
pv = eng.delete_archives([lp], lc, preview=True)["results"][0]
check("move via a symlinked source stays inside the move folder", pv["ok"] and pv["target"] == os.path.join(trash, "realsrc", "sub", "l.zip"), pv)
res = eng.delete_archives([lp], lc, preview=False)["results"][0]
check("move via a symlinked source: moved to the right place", res["ok"] and os.path.isfile(os.path.join(trash, "realsrc", "sub", "l.zip")), res)
try:
    eng._move_dest(os.path.join(other, "o.zip"), real_src, lc)
    esc = False
except RuntimeError:
    esc = True
check("a file that is not inside its source folder is refused (no ../ escape)", esc)
saved_rt = E.mounts.recycle_target
E.mounts.recycle_target = lambda p, m: (os.path.join(share, ".Recycle.Bin", "..", "..", "evil"), "")
try:
    eng._recycle_dest(os.path.join(src, "x.zip"))
    esc = False
except RuntimeError:
    esc = True
E.mounts.recycle_target = saved_rt
check("Recycle Bin target that leaves the bin is refused", esc)

# --- failed rollback: the index does not point at files that no longer exist
d = fresh("stuck")
sh(["rar", "a", "-ma5", "-idq", "-v20k", os.path.join(d, "st.rar"), "old.bin", "new.bin"])
age(d)
scan()
first = P("stuck", "st.part1.rar")
real_rename = eng._rename_file
cnt = []


def very_flaky(old, new):
    cnt.append(old)
    if len(cnt) == 3 or new.endswith(os.sep + "st.part1.rar"):
        raise OSError("disk says no")
    return real_rename(old, new)


eng._rename_file = very_flaky
res = rename([first])["results"][0]
eng._rename_file = real_rename
idx_now = eng.index_load()
check("failed rollback: reason kept, index no longer lists missing files",
      not res["ok"] and "NOT restored" in res["reason"] and all(os.path.exists(v) for r_ in idx_now.values() for v in r_.get("volumes", [r_["path"]])
                                                                if r_["path"].startswith(P("stuck"))), (res, sorted(idx_now)[:3]))

# --- records of a source removed from Settings are dropped by the next full scan
gone_dir = fresh("removed-source")
shutil.copy(P("tied", "t2 (2014).zip"), os.path.join(gone_dir, "g.zip"))
age(gone_dir)
scan(cfg_for(sources=[src]))
check("record of a configured source is there", eng.index_get(os.path.join(gone_dir, "g.zip")) is not None)
keep_dir = P("tied")
scan(cfg_for(sources=[keep_dir]))
check("full scan drops records outside every configured source", eng.index_get(os.path.join(gone_dir, "g.zip")) is None
      and eng.index_get(T("t2 (2014).zip")) is not None)
shutil.rmtree(work, ignore_errors=True)
print("\nALL PASSED" if not FAILS else f"\nFAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
