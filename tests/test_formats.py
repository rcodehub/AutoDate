"""Listing the dates inside real archives (zip, rar incl. volume sets and encrypted headers, 7z, tar family)
without extracting. Archives are built at test time with known old dates.
Run:  python3 -I tests/test_formats.py     (exit 2 when a needed tool is missing)"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import engine as E  # noqa: E402
from engine import Engine  # noqa: E402
import settings  # noqa: E402

for tool in ("rar", "unrar", "7z", "zip", "tar", "xz", "bzip2", "gzip"):
    if not shutil.which(tool):
        print(f"MISSING TOOL: {tool}")
        sys.exit(2)

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def cfg_for(**kw):
    c = dict(settings.DEFAULTS)
    c.update(min_age_minutes=0, threads=2)
    c.update(kw)
    return c


def epoch(s):
    return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))


def sh(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


work = tempfile.mkdtemp(prefix="ad-formats-")
data, out = os.path.join(work, "data"), os.path.join(work, "out")
os.makedirs(data)
os.makedirs(out)
eng = Engine(os.path.join(data, "logs"), os.path.join(data, "history.json"))

# the content: 3 files (random data so volume sets get several parts), 2 folders
# winter dates: 7z shows summer-time (DST) stamps one hour off, whatever the tool stored
OLD_FILE, NEW_FILE = "2010-01-05 10:00:00", "2012-12-09 12:30:15"
OLD_DIR, NEW_DIR = "2005-03-03 03:03:03", "2020-02-02 02:02:02"
tree = os.path.join(work, "tree")
root = os.path.join(tree, "root")
os.makedirs(os.path.join(root, "sub dir"))
for rel, when in (("a.txt", OLD_FILE), ("sub dir/b c.bin", NEW_FILE), ("sub dir/mid.bin", "2011-01-01 00:00:00")):
    with open(os.path.join(root, rel), "wb") as f:
        f.write(os.urandom(30000))
    os.utime(os.path.join(root, rel), (epoch(when), epoch(when)))
os.utime(os.path.join(root, "sub dir"), (epoch(OLD_DIR), epoch(OLD_DIR)))
os.utime(root, (epoch(NEW_DIR), epoch(NEW_DIR)))
ARCHIVE_AGE = time.time() - 3600


def aged(path):
    for f in os.listdir(os.path.dirname(path)):
        if f.startswith(os.path.basename(path).split(".")[0]):
            os.utime(os.path.join(os.path.dirname(path), f), (ARCHIVE_AGE, ARCHIVE_AGE))
    return path


def build(name, cmd):
    sh(cmd, tree)
    return aged(os.path.join(out, name))


def expect(rec, who, files=3, folders=2, include=False):
    ok = (rec["status"] == "ok" and not rec["encrypted"] and rec["file_count"] == files and rec["folder_count"] == folders
          and rec["entry_count"] == files + folders and rec["oldest_any"] == OLD_DIR and rec["newest_any"] == NEW_DIR)
    ok_range = (rec["oldest"], rec["newest"]) == ((OLD_DIR, NEW_DIR) if include else (OLD_FILE, NEW_FILE))
    check(f"{who}: counts and folder/file split", ok, rec.get("error") or {k: rec[k] for k in
          ("status", "file_count", "folder_count", "oldest_any", "newest_any")})
    check(f"{who}: oldest / newest ({'with' if include else 'without'} folders)", ok_range, (rec["oldest"], rec["newest"]))


def ls(path, **kw):
    return eng.list_archive(path, cfg_for(**kw))


# --- zip (7z), rar, 7z, tar family
z = build("plain.zip", ["7z", "a", "-tzip", os.path.join(out, "plain.zip"), "root"])
expect(ls(z), "zip")
expect(ls(z, include_folders=True), "zip", include=True)
r = ls(z)
check("zip: age against the archive date", r["archive_date"] == E.epoch_to_str(ARCHIVE_AGE)
      and abs(r["age_years"] - (ARCHIVE_AGE - epoch(NEW_FILE)) / E.YEAR_SECONDS) < 0.01
      and r["age_years_oldest"] > r["age_years"], (r["age_years"], r["age_years_oldest"]))
check("zip: entries listed with names with spaces, sizes, dates",
      {"name": "root/sub dir/b c.bin", "folder": False, "size": 30000, "date": NEW_FILE} in r["entries"], r["entries"])
check("zip: signature and volumes", r["sig"] == eng._signature(z) and r["volumes"] == [z] and r["type"] == "zip")

rr = build("plain.rar", ["rar", "a", "-ma5", "-r", "-idq", os.path.join(out, "plain.rar"), "root"])
expect(ls(rr), "rar")
expect(ls(rr, include_folders=True), "rar", include=True)

s7 = build("plain.7z", ["7z", "a", os.path.join(out, "plain.7z"), "root"])
expect(ls(s7), "7z")

for ext, flag in (("tar.gz", "z"), ("tar.xz", "J"), ("tar.bz2", "j"), ("tar", ""), ("tgz", "z")):
    p = build(f"plain.{ext}", ["tar", "c" + flag + "f", os.path.join(out, f"plain.{ext}"), "root"])
    r = ls(p)
    expect(r, ext)
    check(f"{ext}: type tar", r["type"] == "tar")

# --- volume sets
vr = build("vol.rar", ["rar", "a", "-ma5", "-r", "-idq", "-v20k", os.path.join(out, "vol.rar"), "root"])
vr = os.path.join(out, "vol.part1.rar")
parts = [f for f in os.listdir(out) if f.startswith("vol.part")]
r = ls(vr)
check("rar volume set: several volumes seen together", len(r["volumes"]) == len(parts) >= 3, r["volumes"])
expect(r, "rar volume set")
check("rar volume set: size is the sum", r["size"] == sum(os.path.getsize(os.path.join(out, f)) for f in parts))
os.remove(os.path.join(out, "vol.part2.rar"))
r = ls(vr)
check("rar volume set with a missing volume -> failed", r["status"] == "failed" and "missing" in r["error"], r["error"])

sv = build("big.7z", ["7z", "a", "-v20k", os.path.join(out, "big.7z"), "root"])
v1 = os.path.join(out, "big.7z.001")
r = ls(v1)
check("7z .001 set: whole set read from the first volume", len(r["volumes"]) >= 3 and r["type"] == "7z", r["volumes"])
expect(r, "7z .001 set")

big = os.path.join(tree, "bigroot")
os.makedirs(big)
for i in range(2):
    with open(os.path.join(big, f"f{i}.bin"), "wb") as f:
        f.write(os.urandom(90000))
    os.utime(os.path.join(big, f"f{i}.bin"), (epoch(OLD_FILE), epoch(OLD_FILE)))
subprocess.run(["zip", "-q", "-s", "70k", "-r", os.path.join(out, "split.zip"), "bigroot"], cwd=tree, check=True,
               stdin=subprocess.DEVNULL)
aged(os.path.join(out, "split.zip"))
r = ls(os.path.join(out, "split.zip"))
check("split zip: z01 volumes found and listed", len(r["volumes"]) >= 2 and r["status"] == "ok" and r["file_count"] == 2
      and r["oldest"] == r["newest"] and r["oldest"].startswith("2010-01-05"), (r["status"], r["error"], r["volumes"], r["oldest"]))

# --- zip with controlled dates: unknown (1980 default), future, fine
zp = os.path.join(out, "dates.zip")
with zipfile.ZipFile(zp, "w") as zf:
    for name, dt in (("old.txt", (2001, 2, 3, 4, 5, 6)), ("default.txt", (1980, 1, 1, 0, 0, 0)), ("new.txt", (2003, 4, 5, 6, 7, 8))):
        zf.writestr(zipfile.ZipInfo(name, dt), "x")
aged(zp)
r = ls(zp)
check("zip: dates as stored (no timezone shift)", r["oldest"] == "2001-02-03 04:05:06" and r["newest"] == "2003-04-05 06:07:08", (r["oldest"], r["newest"]))
check("zip: the 1980 default is unknown, partial flag set, not future",
      r["partial"] and r["future"] is False and {"name": "default.txt", "folder": False, "size": 1, "date": None} in r["entries"], r["entries"])
zp2 = os.path.join(out, "future.zip")
with zipfile.ZipFile(zp2, "w") as zf:
    zf.writestr(zipfile.ZipInfo("f.txt", (2099, 1, 1, 0, 0, 0)), "x")
    zf.writestr(zipfile.ZipInfo("p.txt", (2001, 1, 1, 0, 0, 0)), "x")
r = ls(aged(zp2))
check("zip: dates in the future are flagged", r["future"] is True and r["newest"] == "2099-01-01 00:00:00" and r["age_years"] < 0, r["newest"])
zp3 = os.path.join(out, "nodates.zip")
with zipfile.ZipFile(zp3, "w") as zf:
    zf.writestr(zipfile.ZipInfo("only.txt", (1980, 1, 1, 0, 0, 0)), "x")
r = ls(aged(zp3))
check("zip: nothing dated -> null dates and ages, still ok", r["status"] == "ok" and r["oldest"] is None and r["newest"] is None
      and r["age_years"] is None and r["partial"], r)
zp4 = os.path.join(out, "empty.zip")
with zipfile.ZipFile(zp4, "w"):
    pass
r = ls(aged(zp4))
check("zip: an empty archive is ok with no entries", r["status"] == "ok" and r["entry_count"] == 0 and r["oldest"] is None, r)

# --- summer-time dates through 7z (known 7z quirk: may be shown one hour off); unrar / tar / zipfile are exact
sd = os.path.join(tree, "summer")
os.makedirs(sd)
with open(os.path.join(sd, "s.txt"), "w") as f:
    f.write("1")
os.utime(os.path.join(sd, "s.txt"), (epoch("2014-07-01 12:00:00"),) * 2)
sh(["rar", "a", "-ma5", "-idq", os.path.join(out, "summer.rar"), "summer"], tree)
sh(["7z", "a", os.path.join(out, "summer.7z"), "summer"], tree)
check("summer date through unrar is exact", [e["date"] for e in ls(aged(os.path.join(out, "summer.rar")))["entries"] if not e["folder"]] == ["2014-07-01 12:00:00"])
d7 = ls(aged(os.path.join(out, "summer.7z")))["newest"]
check("summer date through 7z within one hour", abs(E.to_epoch(d7) - epoch("2014-07-01 12:00:00")) <= 3600, d7)

# --- tar edge cases: names with spaces, symlink, hardlink
tdir = os.path.join(tree, "tt")
os.makedirs(os.path.join(tdir, "my dir"))
with open(os.path.join(tdir, "my dir", "x y.txt"), "w") as f:
    f.write("1")
os.utime(os.path.join(tdir, "my dir", "x y.txt"), (epoch(OLD_FILE), epoch(OLD_FILE)))
os.symlink("x y.txt", os.path.join(tdir, "my dir", "link -> odd"))
sh(["tar", "cf", os.path.join(out, "links.tar"), "tt"], tree)
r = ls(aged(os.path.join(out, "links.tar")))
names = sorted(e["name"] for e in r["entries"])
check("tar: spaces and symlinks", "tt/my dir/x y.txt" in names and "tt/my dir/link" in names and r["folder_count"] == 2
      and r["file_count"] == 2, names)

# --- truncation of very long entry lists
old = E.MAX_ENTRIES
E.MAX_ENTRIES = 2
r = ls(z)
E.MAX_ENTRIES = old
check("more than MAX_ENTRIES entries: kept capped, counted fully, dates from all", r["truncated"] and len(r["entries"]) == 2
      and r["entry_count"] == 5 and r["oldest"] == OLD_FILE and r["newest"] == NEW_FILE, (len(r["entries"]), r["entry_count"]))

# --- encrypted headers: flagged, quickly, no password prompt
t0 = time.time()
er = build("enc.rar", ["rar", "a", "-ma5", "-r", "-idq", "-hpsecret", os.path.join(out, "enc.rar"), "root"])
r = ls(er)
check("rar with encrypted headers is flagged, not failed", r["encrypted"] and r["status"] == "ok" and r["oldest"] is None
      and r["entry_count"] == 0 and r["error"], r)
e7 = build("enc.7z", ["7z", "a", "-psecret", "-mhe=on", os.path.join(out, "enc.7z"), "root"])
r = ls(e7)
check("7z with encrypted headers is flagged", r["encrypted"] and r["status"] == "ok", r)
ez = build("encdata.zip", ["7z", "a", "-tzip", "-psecret", os.path.join(out, "encdata.zip"), "root"])
r = ls(ez)
check("zip with encrypted file data still lists its dates", not r["encrypted"] and r["status"] == "ok" and r["newest"] == NEW_FILE, r)
check("encrypted archives did not hang", time.time() - t0 < 60, time.time() - t0)

# --- damaged archives -> failed (never raises), with a reason
def damaged(src, name, keep):
    dst = os.path.join(out, name)
    with open(src, "rb") as f, open(dst, "wb") as g:
        g.write(f.read(keep))
    return aged(dst)


for src, name, keep in ((z, "cut.zip", 200), (rr, "cut.rar", 600), (s7, "cut.7z", 100), (os.path.join(out, "plain.tar.gz"), "cut.tar.gz", 300),
                        (os.path.join(out, "plain.tar.xz"), "cut.tar.xz", 100)):
    r = ls(damaged(src, name, keep))
    check(f"damaged {name}: failed with a reason", r["status"] == "failed" and r["error"] and not r["encrypted"], (r["status"], r["error"]))
junk = os.path.join(out, "junk.rar")
with open(junk, "wb") as f:
    f.write(b"this is not a rar at all\n" * 10)
r = ls(aged(junk))
check("a text file called .rar -> failed", r["status"] == "failed" and r["error"], r["error"])
r = ls(os.path.join(out, "does-not-exist.zip"))
check("missing file -> failed", r["status"] == "failed")

# --- a tool that never finishes is killed by the timeout
slow = os.path.join(work, "slow.sh")
with open(slow, "w") as f:
    f.write("#!/bin/sh\nsleep 60\n")
os.chmod(slow, stat.S_IRWXU)
saved_tools, saved_to = dict(eng.tools), E.LIST_TIMEOUT
eng.tools.update(unrar=slow, **{"7z": slow})
E.LIST_TIMEOUT = 1
t0 = time.time()
r = ls(rr)
E.LIST_TIMEOUT = saved_to
eng.tools.update(saved_tools)
check("a hanging tool is killed after the timeout", r["status"] == "failed" and "timed out" in r["error"] and time.time() - t0 < 15,
      (r["error"], time.time() - t0))

# --- rar via 7z when unrar is missing
eng.tools["unrar"] = None
r = ls(rr)
eng.tools.update(saved_tools)
check("rar falls back to 7z", r["status"] == "ok" and r["file_count"] == 3 and r["oldest_any"] is not None, r["error"])

# --- cancel / stop raise Halted
eng.stop_event.set()
try:
    ls(z)
    halted = False
except E.Halted:
    halted = True
eng.stop_event.clear()
check("a stopped run raises Halted", halted)

# --- exact local times with historical summer time (TZ America/New_York forced, restored afterwards)
old_tz = os.environ.get("TZ")
os.environ["TZ"] = "America/New_York"
time.tzset()
try:
    DST = (("summer", "2011-06-15 12:00:00"), ("winter", "2011-12-15 12:00:00"), ("new year", "2015-12-31 23:30:00"))
    for tag, when in DST:
        d = os.path.join(tree, f"dst-{tag.replace(' ', '')}")
        os.makedirs(os.path.join(d, "x"))
        fn = os.path.join(d, "x", "f.bin")
        with open(fn, "wb") as f:
            f.write(os.urandom(70000))
        os.utime(fn, (epoch(when), epoch(when)))
        os.utime(os.path.join(d, "x"), (epoch(when), epoch(when)))
        nm = os.path.basename(d)
        sh(["rar", "a", "-ma5", "-r", "-idq", "-v20k", os.path.join(out, f"{nm}v.rar"), "x"], d)
        sh(["7z", "a", os.path.join(out, f"{nm}.7z"), "x"], d)
        sh(["zip", "-qr", os.path.join(out, f"{nm}-cli.zip"), "x"], d)
        with zipfile.ZipFile(os.path.join(out, f"{nm}-dos.zip"), "w") as zf:
            zf.writestr(zipfile.ZipInfo("f.bin", tuple(time.localtime(epoch(when)))[:6]), "x")
        for f in os.listdir(out):
            if f.startswith(nm):
                os.utime(os.path.join(out, f), (ARCHIVE_AGE, ARCHIVE_AGE))

        def only(rec, who):
            dates = {e["date"] for e in rec["entries"] if not e["folder"]}
            check(f"{tag} {who}: file date is exactly {when}", rec["status"] == "ok" and dates == {when},
                  (rec["status"], rec["error"], dates))
        vol1 = os.path.join(out, f"{nm}v.part1.rar")
        r = ls(vol1)
        check(f"{tag} rar set has several volumes", len(r["volumes"]) >= 3, r["volumes"])
        only(r, "RAR5 volume set (unrar)")
        check(f"{tag} rar set: year stays", r["newest"][:4] == when[:4], r["newest"])
        saved_tools = dict(eng.tools)
        eng.tools["7z"] = None                      # unrar alone must be able to read a set
        r = ls(vol1)
        eng.tools.update(saved_tools)
        only(r, "RAR5 volume set without 7z")
        eng.tools["unrar"] = None
        r = ls(vol1)
        eng.tools.update(saved_tools)
        only(r, "RAR5 volume set via 7z fallback")
        only(ls(os.path.join(out, f"{nm}.7z")), "7z")
        only(ls(os.path.join(out, f"{nm}-cli.zip")), "zip (extended timestamp)")
        only(ls(os.path.join(out, f"{nm}-dos.zip")), "zip (DOS time only)")
finally:
    if old_tz is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old_tz
    time.tzset()

shutil.rmtree(work, ignore_errors=True)
print("\nALL PASSED" if not FAILS else f"\nFAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
