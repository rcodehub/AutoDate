"""Archives API checks with a fake engine. Run:  python3 -I tests/test_api.py"""
import base64
import importlib
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))
FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


for k in ("UI_USERNAME", "UI_PASSWORD"):
    os.environ.pop(k, None)
os.environ.update(NO_SCHEDULER="1", DATA_DIR=tempfile.mkdtemp(prefix="ad-api-"), UI_USERNAME="u", UI_PASSWORD="pw")
for m in ("settings", "engine", "main"):
    sys.modules.pop(m, None)
main = importlib.import_module("main")
engine_mod = importlib.import_module("engine")

# --- fake engine: records the calls
calls = []
EXPECTS = []
EX = {"/downloads/a.rar": "/t"}
INDEX = {"/downloads/a.rar": {"path": "/downloads/a.rar", "entries": [{"name": "x", "folder": False, "size": 1, "date": 1.0}]}}


def fake_query(**kw):
    calls.append(("query", kw))
    return {"total": 1, "items": [{"path": "/downloads/a.rar", "outdated": True, "volumes": 1}],
            "summary": {"archives": 1, "outdated": 1, "failed": 0, "bytes": 5}}


main.engine.index_query = lambda **kw: fake_query(**kw)
main.engine.index_get = lambda path: INDEX.get(path)
main.engine.delete_archives = lambda paths, cfg, preview=True, expect=None: (
    EXPECTS.append(expect) or calls.append(("delete", paths, preview)) or {"results": [{"path": p, "ok": True, "action": "recycle", "target": "/t", "bytes": 1} for p in paths], "count": len(paths), "bytes": len(paths)})
main.engine.rename_archives = lambda paths, use, cfg, preview=True, expect=None: (
    EXPECTS.append(expect) or calls.append(("rename", paths, use, preview)) or {"results": [{"path": p, "ok": True, "new_path": p + "2", "renamed": [[p, p + "2"]], "year": 2016} for p in paths], "count": len(paths)})
main.engine.rescan = lambda paths, cfg: (calls.append(("rescan", paths)) or {"results": [{"path": p, "ok": True} for p in paths]})

ROUTES_GET = ("/api/archives", "/api/archive?path=/downloads/a.rar")
ROUTES_POST = ("/api/archives/delete", "/api/archives/rename", "/api/archives/rescan")

# --- login required for every new route
anon = main.app.test_client()
for u in ROUTES_GET:
    check(f"{u}: login required", anon.get(u).status_code == 401)
for u in ROUTES_POST:
    check(f"{u}: login required", anon.post(u, json={"paths": ["/downloads/a.rar"], "preview": True}).status_code == 401)
check("nothing reached the engine without login", not calls, calls)

c = main.app.test_client()
r = c.post("/login", data={"username": "u", "password": "pw"})
check("logged in", r.status_code == 302 and c.get("/api/status").status_code == 200)

# --- GET /api/archives: defaults, pass-through, bounds
r = c.get("/api/archives")
check("archives: ok", r.status_code == 200 and r.get_json()["total"] == 1 and r.get_json()["items"][0]["outdated"] is True)
kw = calls[-1][1]
check("archives: defaults", kw["sort"] == "name" and kw["desc"] is False and kw["q"] == "" and kw["outdated"] is False
      and kw["type"] == "" and kw["status"] == "" and kw["offset"] == 0 and kw["limit"] == 200, kw)
check("archives: engine gets the settings", isinstance(kw["cfg"], dict) and "outdated_years" in kw["cfg"])
r = c.get("/api/archives?sort=size&desc=1&q=%20holiday%20&outdated=1&type=rar&status=failed&offset=400&limit=50")
kw = calls[-1][1]
check("archives: params passed through", r.status_code == 200 and kw["sort"] == "size" and kw["desc"] is True and kw["q"] == "holiday"
      and kw["outdated"] is True and kw["type"] == "rar" and kw["status"] == "failed" and kw["offset"] == 400 and kw["limit"] == 50, kw)
for f in ("name", "folder", "archive_date", "oldest", "newest", "age_years", "files", "size"):
    check(f"archives: sort={f} accepted", c.get("/api/archives?sort=" + f).status_code == 200)
n = len(calls)
for qs in ("sort=bogus", "sort=", "desc=2", "desc=yes", "outdated=x", "type=exe", "status=weird", "offset=-1", "offset=abc",
           "offset=10000001", "limit=0", "limit=501", "limit=-5", "limit=1.5", "q=" + "x" * 201):
    r = c.get("/api/archives?" + qs)
    check(f"archives: {qs[:30]} -> 400", r.status_code == 400 and r.get_json().get("error"), r.status_code)
check("archives: limit 500 allowed", c.get("/api/archives?limit=500").status_code == 200 and calls[-1][1]["limit"] == 500)
bad_calls = len(calls)
c.get("/api/archives?limit=9999")
check("archives: rejected limit does not call the engine", len(calls) == bad_calls)

# --- GET /api/archive
r = c.get("/api/archive?path=/downloads/a.rar")
check("archive: found with entries", r.status_code == 200 and r.get_json()["entries"][0]["name"] == "x")
r = c.get("/api/archive?path=/downloads/missing.rar")
check("archive: not in the index -> 404", r.status_code == 404 and r.get_json().get("error"), r.status_code)
check("archive: no path -> 400", c.get("/api/archive").status_code == 400 and c.get("/api/archive?path=").status_code == 400)

# --- POST validation (all three)
good = ["/downloads/a.rar"]
for url in ROUTES_POST:
    n = len(calls)
    for body in ("[1, 2]", '"text"', "42", "null", "{broken"):
        r = c.post(url, data=body, content_type="application/json")
        check(f"{url}: body {body} -> 400", r.status_code == 400 and r.get_json().get("error"), r.status_code)
    for name, body in (("no paths", {}), ("empty paths", {"paths": []}), ("paths not a list", {"paths": "/downloads/a.rar"}),
                       ("paths is an object", {"paths": {"a": 1}}), ("non-string path", {"paths": ["/x", 5]}),
                       ("null path", {"paths": [None]}), ("empty path", {"paths": [""]}), ("nested list", {"paths": [["/x"]]}),
                       ("too many", {"paths": ["/p%d" % i for i in range(5001)]})):
        r = c.post(url, json=body)
        check(f"{url}: {name} -> 400", r.status_code == 400 and r.get_json().get("error"), (r.status_code, r.get_data(as_text=True)[:80]))
    check(f"{url}: bad requests never reached the engine", len(calls) == n, calls[n:])
    r = c.post(url, json={"paths": ["/p%d" % i for i in range(5000)]})
    check(f"{url}: exactly 5000 paths accepted", r.status_code == 200, r.status_code)

# --- preview vs real routing
calls.clear()
r = c.post("/api/archives/delete", json={"paths": good})
check("delete: preview is the default (safe)", r.status_code == 200 and calls[-1] == ("delete", good, True) and r.get_json()["preview"] is True, calls)
r = c.post("/api/archives/delete", json={"paths": good, "preview": True})
check("delete: preview true", calls[-1][2] is True and r.get_json()["results"][0]["target"] == "/t")
r = c.post("/api/archives/delete", json={"paths": good, "preview": False, "expect": EX})
check("delete: preview false does the real thing", r.status_code == 200 and calls[-1] == ("delete", good, False) and r.get_json()["preview"] is False, calls[-1])
for bad in ("false", "0", 0, 1, None, "no"):
    n = len(calls)
    r = c.post("/api/archives/delete", json={"paths": good, "expect": EX, "preview": bad})
    check(f"delete: preview={bad!r} (not a boolean) -> 400, nothing deleted", r.status_code == 400 and len(calls) == n, (r.status_code, calls[n:]))
r = c.post("/api/archives/delete", json={"paths": good + good + ["/downloads/b.rar"], "preview": True})
check("delete: duplicate paths are dropped", calls[-1][1] == ["/downloads/a.rar", "/downloads/b.rar"], calls[-1])

# a real run needs the expect of the preview that was shown
for url, extra in (("/api/archives/delete", {}), ("/api/archives/rename", {"use": "newest"})):
    for name, ex in (("missing", None), ("a list", ["x"]), ("a string", "x"), ("non-string target", {"/downloads/a.rar": 5}), ("null", None)):
        n = len(calls)
        body = dict(extra, paths=good, preview=False)
        if name != "missing":
            body["expect"] = ex
        r = c.post(url, json=body)
        check(f"{url}: real run with expect {name} -> 400, engine untouched", r.status_code == 400 and "expect" in r.get_json()["error"] and len(calls) == n, (r.status_code, calls[n:]))
    n = len(calls)
    r = c.post(url, json=dict(extra, paths=good, preview=True))
    check(f"{url}: preview needs no expect and passes none", r.status_code == 200 and EXPECTS[-1] is None)
    r = c.post(url, json=dict(extra, paths=good, preview=False, expect=EX))
    check(f"{url}: expect is handed to the engine", r.status_code == 200 and EXPECTS[-1] == EX, EXPECTS[-1])

r = c.post("/api/archives/rename", json={"paths": good, "use": "oldest"})
check("rename: preview by default", r.status_code == 200 and calls[-1] == ("rename", good, "oldest", True), calls[-1])
r = c.post("/api/archives/rename", json={"paths": good, "use": "newest", "preview": False, "expect": EX})
check("rename: real run", calls[-1] == ("rename", good, "newest", False) and r.get_json()["results"][0]["new_path"] == "/downloads/a.rar2")
r = c.post("/api/archives/rename", json={"paths": good, "preview": False, "expect": EX})
check("rename: use defaults to the setting (newest)", calls[-1][2] == "newest", calls[-1])
import settings as _s  # noqa: E402
cur = _s.load()
cur_save = dict(cur, rename_use="oldest")
_s.save(cur_save)
c.post("/api/archives/rename", json={"paths": good})
check("rename: use follows Settings rename_use", calls[-1][2] == "oldest", calls[-1])
for use in ("middle", "", "NEWEST ", 1, True, ["newest"], {"a": 1}):
    n = len(calls)
    r = c.post("/api/archives/rename", json={"paths": good, "use": use, "preview": False, "expect": EX})
    check(f"rename: use={use!r} -> 400", r.status_code == 400 and "newest or oldest" in r.get_json()["error"] and len(calls) == n, (r.status_code, calls[n:]))
r = c.post("/api/archives/rename", json={"paths": good, "use": "newest", "preview": "yes"})
check("rename: preview must be boolean", r.status_code == 400)

r = c.post("/api/archives/rescan", json={"paths": good})
check("rescan: routes to the engine", r.status_code == 200 and calls[-1] == ("rescan", good) and r.get_json()["ok"] is True)

main.engine.rescan = lambda paths, cfg: {"results": [{"path": p, "ok": False, "reason": "not in the index"} for p in paths]}
r = c.post("/api/archives/rescan", json={"paths": good})
check("rescan: nothing accepted -> HTTP 200 with top-level ok false", r.status_code == 200 and r.get_json()["ok"] is False
      and r.get_json()["results"][0]["reason"] == "not in the index", r.get_json())
main.engine.rescan = lambda paths, cfg: {"results": [{"path": paths[0], "ok": True}, {"path": "/x", "ok": False, "reason": "no"}]}
check("rescan: some accepted -> ok true", c.post("/api/archives/rescan", json={"paths": good + ["/x"]}).get_json()["ok"] is True)

# --- actions refused by the engine are passed on (e.g. a scan is running)
main.engine.delete_archives = lambda paths, cfg, preview=True, expect=None: {"results": [{"path": p, "ok": False, "reason": "a scan is running"} for p in paths], "count": 0, "bytes": 0}
r = c.post("/api/archives/delete", json={"paths": good, "preview": False, "expect": EX})
check("refusals reach the client", r.status_code == 200 and r.get_json()["results"][0]["reason"] == "a scan is running" and r.get_json()["count"] == 0)

# --- busy: engine.start() says no while a delete / rename / rescan runs -> 409, nothing recorded
starts = []
main.engine.start = lambda cfg, trigger="manual", oneoff=None: starts.append(trigger) and False
cur = _s.load()
_s.save(dict(cur, sources=["/downloads"], schedule_enabled=True, interval_minutes=1, run_on_start=True))
r = c.post("/api/run")
check("run: 409 while an action runs", r.status_code == 409 and "An action is running" in r.get_json()["error"], (r.status_code, r.get_data(as_text=True)))
tmpf = tempfile.mkdtemp(prefix="ad-api-folder-")
main.mounts.inside_roots = lambda *a, **k: True
r = c.post("/api/run-folder", json={"path": tmpf})
check("run-folder: 409 while an action runs", r.status_code == 409 and "An action is running" in r.get_json()["error"], (r.status_code, r.get_data(as_text=True)))
check("both routes did try to start", starts == ["manual", "one-off"], starts)
_real_load = main.settings.load
main.settings.load = lambda: dict(_real_load(), sources=["/downloads"], schedule_enabled=True, interval_minutes=1, run_on_start=False)
sch = main.Scheduler()
sch.started -= 3600
del starts[:]
sch.tick()
check("scheduler: tries to start", starts == ["schedule"], starts)
check("scheduler: a refused start is not recorded as a run", sch.last_start is None and not sch._start_run_done, (sch.last_start, sch._start_run_done))
sch.tick()
check("scheduler: retries on the next tick", starts == ["schedule", "schedule"], starts)
main.engine.start = lambda cfg, trigger="manual", oneoff=None: starts.append(trigger) or True
sch.tick()
check("scheduler: records the run once it starts", sch.last_start is not None and sch._start_run_done)
r = c.post("/api/run")
check("run: ok when it starts", r.status_code == 200 and r.get_json()["ok"] is True)
main.settings.load = _real_load

# --- an engine that is not finished answers 501 json, never a stack trace
def nope(*a, **k):
    raise NotImplementedError("later")


main.engine.index_query = nope
r = c.get("/api/archives")
check("not implemented -> 501 json", r.status_code == 501 and r.get_json()["error"] == "later")

# --- Clear results
main.engine.clear_results = lambda under=None: (True, 7)
r = c.post("/api/archives/clear")
check("clear: ok with an empty body", r.status_code == 200 and r.get_json() == {"ok": True, "cleared": 7}, r.get_json())
r = c.post("/api/archives/clear", json=[1])
check("clear: non-object JSON -> 400", r.status_code == 400)
main.engine.clear_results = lambda under=None: (False, "A scan is running - stop it or wait until it finishes")
r = c.post("/api/archives/clear", json={})
check("clear: busy -> 409 with the reason", r.status_code == 409 and "scan is running" in r.get_json()["error"])
check("clear: needs login", anon.post("/api/archives/clear").status_code == 401)

# --- basic auth (scripts) also works on the new routes
h = {"Authorization": "Basic " + base64.b64encode(b"u:pw").decode()}
check("basic auth works", main.app.test_client().get("/api/archive?path=/downloads/a.rar", headers=h).status_code == 200)

print("\nALL PASSED" if not FAILS else f"\nFAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
