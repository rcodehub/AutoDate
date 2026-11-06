"""Web app + scheduler for the AutoDate container."""
import hmac
import json
import os
import re
import secrets
import threading
import time
from datetime import timedelta

from flask import Flask, jsonify, redirect, request, send_from_directory, session

import mounts
import settings
import engine as engine_mod
from engine import Engine

CONFIG_DIR = settings.CONFIG_DIR
os.makedirs(CONFIG_DIR, exist_ok=True)

engine = Engine(os.path.join(CONFIG_DIR, "logs"), os.path.join(CONFIG_DIR, "history.json"))
app = Flask(__name__, static_folder="static", static_url_path="/static")

# ---------------------------------------------------------------- login
# Set UI_PASSWORD to require a login. An empty UI_USERNAME falls back to "admin".
BUILD = {"number": os.environ.get("BUILD_NUMBER") or "dev", "sha": (os.environ.get("BUILD_SHA") or "")[:7],
         "date": os.environ.get("BUILD_DATE") or ""}
print(f"[build] AutoDate build {BUILD['number']}" + (f" ({BUILD['sha']}, {BUILD['date']})" if BUILD["sha"] else ""),
      flush=True)
UI_USER = (os.environ.get("UI_USERNAME") or "admin").strip()
UI_PASS = os.environ.get("UI_PASSWORD", "")
AUTH_ON = bool(UI_PASS)


def _secret_key():
    """Session signing key, kept in the app data folder so logins survive a restart."""
    path = os.path.join(CONFIG_DIR, "secret.key")
    try:
        with open(path) as f:
            key = f.read().strip()
        if key:
            return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key)
    except OSError:
        pass
    return key


app.secret_key = _secret_key()
app.config.update(SESSION_COOKIE_NAME="autodate_session", SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE="Lax", PERMANENT_SESSION_LIFETIME=timedelta(days=30))
print(f"[auth] login required, user '{UI_USER}'" if AUTH_ON
      else "[auth] UI_PASSWORD not set - the web UI is open to anyone who can reach it", flush=True)

_fails = {}   # ip -> timestamps of recent failed logins (form and basic auth share this counter)
_fails_lock = threading.Lock()      # waitress runs requests on several threads
FAIL_WINDOW, FAIL_MAX = 300, 8      # 8 failures within 5 minutes lock that address out for the rest of the window
_CTRL = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def _recent(ip, now):
    """Failures from this address inside the window; forget every address that has none left. Hold _fails_lock."""
    for k in [k for k, v in _fails.items() if not v or now - v[-1] >= FAIL_WINDOW]:
        del _fails[k]
    if ip in _fails:
        _fails[ip] = [t for t in _fails[ip] if now - t < FAIL_WINDOW]
    return len(_fails.get(ip, ()))


def _locked(ip):
    with _fails_lock:
        return _recent(ip, time.time()) >= FAIL_MAX


def _failed(ip, user, how):
    """Count a failed login for this address and log it; control characters in the name can't forge log lines."""
    with _fails_lock:
        now = time.time()
        _recent(ip, now)
        _fails.setdefault(ip, []).append(now)
    print(f"[auth] failed {how} for user '{_CTRL.sub('', user or '')[:40]}' from {ip}", flush=True)


def creds_ok(user, password):
    return (hmac.compare_digest((user or "").strip().lower().encode(), UI_USER.lower().encode())
            and hmac.compare_digest((password or "").encode(), UI_PASS.encode()))


@app.before_request
def require_login():
    if not AUTH_ON or request.path in ("/health", "/login", "/static/theme.js"):
        return None
    if session.get("ok"):
        return None
    a = request.authorization                     # scripts may still send HTTP basic auth
    if a:
        ip = request.remote_addr or "?"
        if _locked(ip):
            print(f"[auth] basic auth blocked (too many failures) from {ip}", flush=True)
            return jsonify(error="Too many failed logins - try again in a few minutes", login=True), 429
        if creds_ok(a.username, a.password):
            return None
        _failed(ip, a.username, "basic auth")
    if request.path.startswith("/api/"):
        return jsonify(error="Login required", login=True), 401
    return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not AUTH_ON:
        return redirect("/")
    if request.method == "POST":
        ip = request.remote_addr or "?"
        if _locked(ip):
            print(f"[auth] login blocked (too many failures) from {ip}", flush=True)
            return redirect("/login?e=locked")
        if creds_ok(request.form.get("username"), request.form.get("password")):
            session.clear()
            session.permanent = True
            session["ok"] = True
            return redirect("/")
        _failed(ip, request.form.get("username"), "login")
        return redirect("/login?e=bad")
    if session.get("ok"):
        return redirect("/")
    return send_from_directory(app.static_folder, "login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.after_request
def no_cache(resp):
    if request.path.startswith("/api/") or request.path in ("/", "/login"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------- scheduler
class Scheduler(threading.Thread):
    """Starts a scan every N minutes (counted from the end of the previous one)."""

    def __init__(self):
        super().__init__(daemon=True)
        self.started = time.time()
        self.last_start = None
        self.next_run = None
        self._start_run_done = False

    def run(self):
        while True:
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - never let the scheduler die
                engine.log("error", f"Scheduler error: {e}")
            time.sleep(2)

    def tick(self):
        if engine.running:
            self.next_run = None
            return
        cfg = settings.load()
        if not cfg["schedule_enabled"] or not cfg["sources"]:
            self.next_run = None
            return
        # The interval counts from the end of the last scan (scheduled or manual). Take that time
        # from the engine itself: a scan of empty folders can start and finish between two ticks,
        # so watching for "running" would miss it and start the next scan at once (a scan loop).
        finished = engine.status.get("finished_at") or 0
        last = max(self.started, finished, self.last_start or 0)
        if cfg["run_on_start"] and not self._start_run_done and not finished:
            self.next_run = self.started
        else:
            self.next_run = last + cfg["interval_minutes"] * 60
        if time.time() >= self.next_run:
            self._start_run_done = True
            self.last_start = time.time()
            engine.start(cfg, trigger="schedule")


scheduler = Scheduler()
if not os.environ.get("NO_SCHEDULER"):
    scheduler.start()


# ---------------------------------------------------------------- pages / API
def json_body():
    """(body, None) for a JSON object (an empty body counts as {}), otherwise (None, 400 response)."""
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data, None
    if data is None and not request.get_data().strip():
        return {}, None
    return None, (jsonify(ok=False, error="The request body must be a JSON object."), 400)


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/health")
def health():
    return jsonify(ok=True, build=BUILD["number"])


@app.route("/api/status")
def api_status():
    cfg = settings.load()
    return jsonify(
        engine=engine.snapshot(),
        schedule={"enabled": cfg["schedule_enabled"], "interval": cfg["interval_minutes"],
                  "next_run": scheduler.next_run},
        build=BUILD,
        now=time.time(),
    )


@app.route("/api/log")
def api_log():
    after = request.args.get("after", 0, type=int)
    entries = engine.log_since(after)
    return jsonify(entries=entries, last=engine.log_seq, cleared=engine.log_cleared)


UI_PATH = os.path.join(CONFIG_DIR, "ui.json")
UI_DEFAULTS = {"theme": "default", "radius": 10, "density": "comfortable", "font": "", "shadow": "none",
               "width": "wide", "mode": "auto"}
UI_CHOICES = {"density": ("compact", "comfortable", "spacious"), "font": ("", "system", "rounded", "serif", "mono"),
              "shadow": ("none", "soft", "strong"), "width": ("standard", "wide", "full"),
              "mode": ("auto", "light", "dark")}
UI_THEMES = ("default", "unraid", "midnight", "nord", "dracula", "solarized", "forest", "rose", "glass",
             "contrast", "terminal")


def load_ui():
    try:
        with open(UI_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    return clean_ui(data if isinstance(data, dict) else {})


def clean_ui(data):
    out = dict(UI_DEFAULTS)
    for k, choices in UI_CHOICES.items():
        if data.get(k) in choices:
            out[k] = data[k]
    if data.get("theme") in UI_THEMES:
        out["theme"] = data["theme"]
    try:
        out["radius"] = max(0, min(24, int(data.get("radius", out["radius"]))))
    except (TypeError, ValueError):
        pass
    return out


@app.route("/api/ui", methods=["GET", "POST"])
def api_ui():
    """Look and feel (theme, corners, density, font, shadows, page width, default light/dark mode)."""
    if request.method == "POST":
        body, bad = json_body()
        if bad:
            return bad
        ui = clean_ui({**load_ui(), **body})
        try:
            tmp = UI_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(ui, f, indent=2)
            os.replace(tmp, UI_PATH)
        except OSError as e:
            return jsonify(error=f"Could not save the look: {e}"), 500
        return jsonify(ok=True, ui=ui)
    return jsonify(ui=load_ui())


@app.route("/api/stats")
def api_stats():
    return jsonify(stats=engine.stats.snapshot(), now=time.time())


@app.route("/api/stats/reset", methods=["POST"])
def api_stats_reset():
    engine.stats.reset()
    engine.log("info", "Statistics were reset")
    return jsonify(ok=True)


@app.route("/api/log/clear", methods=["POST"])
def api_log_clear():
    engine.clear_log()
    return jsonify(ok=True)


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "POST":
        data, bad = json_body()
        if bad:
            return bad
        clean, errors = settings.save(data)
        if errors:
            return jsonify(ok=False, errors=errors), 400
        return jsonify(ok=True, settings=clean)
    cfg, problems, source = settings.load_with_report()
    mnt = mounts.list_mounts(CONFIG_DIR)
    storage = dict(settings.storage_info(), source=source,
                   config_mapped=any(m["config"] for m in mnt))
    return jsonify(
        settings=cfg,
        problems=problems,
        storage=storage,
        tools=engine.tools,
        sources_info={p: mounts.path_info(p, mnt) for p in cfg["sources"]},
        move_info=mounts.path_info(cfg["move_dir"], mnt) if cfg["move_dir"] else None,
        recycle=[recycle_report(p, mnt) for p in cfg["sources"]],
        auth_enabled=AUTH_ON,
    )


def recycle_report(path, mnt):
    """Where the Recycle Bin for `path` is, on the server and in the container, and how many items it holds."""
    root, why = mounts.recycle_root(os.path.join(path, "x"), mnt)
    out = {"source": path, "ok": not why, "why": why, "bin": root, "host": None, "exists": False, "items": None}
    if root:
        out["host"] = mounts.path_info(root, mnt)["host"]
        try:
            with os.scandir(root) as it:
                out["items"] = sum(1 for _ in it)
            out["exists"] = True
        except OSError:
            pass
    return out


@app.route("/api/recycle")
def api_recycle():
    path = os.path.normpath(request.args.get("path") or "/")
    return jsonify(info=recycle_report(path, mounts.list_mounts(CONFIG_DIR)))


@app.route("/api/mounts")
def api_mounts():
    """Folders mapped into the container (container paths), for the Settings page."""
    return jsonify(mounts=mounts.list_mounts(CONFIG_DIR), config_dir=CONFIG_DIR)


@app.route("/api/pathinfo")
def api_pathinfo():
    """Live check of a typed path, so the page can warn before saving."""
    p = os.path.normpath(request.args.get("path") or "/")
    return jsonify(info=mounts.path_info(p, mounts.list_mounts(CONFIG_DIR)))


@app.route("/api/run", methods=["POST"])
def api_run():
    body, bad = json_body()
    if bad:
        return bad
    cfg = settings.load()
    if not cfg["sources"]:
        return jsonify(ok=False, error="Add at least one source folder in Settings first."), 400
    if engine.running:
        return jsonify(ok=False, error="A scan is already running."), 409
    engine.start(cfg, trigger="manual")
    return jsonify(ok=True)


@app.route("/api/run-folder", methods=["POST"])
def api_run_folder():
    """One-off job: scan one folder now (with or without its sub-folders), whatever the configured source
    folders and schedule say. Uses the saved settings."""
    body, bad = json_body()
    if bad:
        return bad
    path = os.path.normpath(str(body.get("path") or ""))
    if not os.path.isabs(path) or not os.path.isdir(path):
        return jsonify(ok=False, error="Choose an existing folder (a path inside the container)."), 400
    if settings._within(path, settings.CONFIG_DIR):
        return jsonify(ok=False, error="That is the app data folder - choose a folder such as /downloads."), 400
    if not mounts.inside_roots(path, mounts.browse_roots(CONFIG_DIR)):
        return jsonify(ok=False, error="Choose a folder inside one of the folders mapped into the container "
                                       "(such as /downloads or /mnt/user)."), 400
    if engine.running:
        return jsonify(ok=False, error="A scan is already running. Wait for it to finish or stop it first."), 409
    cfg = settings.load()
    cfg["sources"] = [path]
    cfg["min_age_minutes"] = 0              # the user chose this folder on purpose
    clean, errors = settings.validate(cfg, mapped=True)
    if errors:
        return jsonify(ok=False, error="; ".join(errors)), 400
    engine.start(clean, trigger="one-off",
                 oneoff={"path": path, "recursive": body.get("recursive", True) is not False})
    return jsonify(ok=True)


@app.route("/api/cancel", methods=["POST"])
def api_cancel():
    """Cancel the listing of one archive; the rest of the run carries on."""
    body, bad = json_body()
    if bad:
        return bad
    ok, why = engine.cancel_job(str(body.get("path") or ""))
    if not ok:
        return jsonify(ok=False, error=why), 409
    return jsonify(ok=True)


@app.route("/api/stop", methods=["POST"])
def api_stop():
    return jsonify(ok=engine.stop())


# ---------------------------------------------------------------- archives (the index) and actions
# TODO(web agent): implement these routes on top of the engine (index_query, index_get, delete_archives,
# rename_archives, rescan - the engine agent builds those). Contract: see "API" in the spec. Until then they
# answer 501. Keep: login required (the before_request hook covers them), json_body() for POSTs, `paths` must be
# a list of at most engine_mod.ACTION_MAX_PATHS strings (else 400), non-object JSON is a 400.
NOT_IMPLEMENTED = ({"error": "not implemented"}, 501)


@app.route("/api/archives")
def api_archives():
    """TODO(web agent): GET ?sort=&desc=0|1&q=&outdated=0|1&type=&status=&offset=&limit= -> engine.index_query()."""
    return NOT_IMPLEMENTED


@app.route("/api/archive")
def api_archive():
    """TODO(web agent): GET ?path= -> the full record including entries (404 / refused when not in the index)."""
    return NOT_IMPLEMENTED


@app.route("/api/archives/delete", methods=["POST"])
def api_archives_delete():
    """TODO(web agent): POST {paths, preview} -> engine.delete_archives(paths, settings.load(), preview)."""
    return NOT_IMPLEMENTED


@app.route("/api/archives/rename", methods=["POST"])
def api_archives_rename():
    """TODO(web agent): POST {paths, use: newest|oldest, preview} -> engine.rename_archives(...)."""
    return NOT_IMPLEMENTED


@app.route("/api/archives/rescan", methods=["POST"])
def api_archives_rescan():
    """TODO(web agent): POST {paths} -> engine.rescan(paths, settings.load())."""
    return NOT_IMPLEMENTED


@app.route("/api/history")
def api_history():
    return jsonify(history=list(reversed(engine.history[-100:])))


@app.route("/api/browse")
def api_browse():
    """List sub-folders for the folder picker - only inside mapped folders. The top level ("") lists the
    mapped folders themselves."""
    roots = mounts.browse_roots(CONFIG_DIR)
    path = os.path.normpath(request.args.get("path") or "/")
    if not os.path.isabs(path) or not os.path.isdir(path) or not mounts.inside_roots(path, roots):
        return jsonify(path="", parent=None, top=True, dirs=sorted(roots),
                       error=None if roots else "No folders are mapped into the container yet.")
    try:
        # hidden folders (.name) are listed too; only AutoDate's own work folders are left out
        dirs = sorted((e.name for e in os.scandir(path)
                       if e.is_dir() and not e.name.startswith(engine_mod.WORK_PREFIX)
                       and (not e.is_symlink() or mounts.inside_roots(e.path, roots))),
                      key=lambda n: (n.lstrip(".").lower(), n))[:2000]
    except OSError as e:
        return jsonify(path=path, parent="", dirs=[], error=str(e))
    at_root = any(os.path.realpath(path) == os.path.realpath(r) for r in roots)
    return jsonify(path=path, parent="" if at_root else os.path.dirname(path), dirs=dirs)


_cfg, _problems, _src = settings.load_with_report()
if _src == "defaults":          # first start: write the settings file now so it always exists in the app data folder
    _saved, _errs = settings.save(_cfg)
    if _saved is None:
        print(f"[settings] could not create the settings file: {'; '.join(_errs)}", flush=True)
    else:
        _src = "file (created at first start)"
print(f"[settings] {settings.SETTINGS_PATH}: {_src}"
      + (f" - ignored invalid values: {'; '.join(_problems)}" if _problems else "")
      + f" | {CONFIG_DIR} is {'a mapped folder (settings persist)' if any(m['config'] for m in mounts.list_mounts(CONFIG_DIR)) else 'NOT a mapped folder - settings will be lost when the container is recreated!'}",
      flush=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), threaded=True)
