#!/usr/bin/env python3
"""Hardening regression suite: reset, PDF export, account and data routes.

Pins the post-migration hardening change:

  * a password-reset request must never turn into a 500 just because the
    process prints emoji on a cp1252 console (the Windows default), and
  * ``/export-analytics`` must stream its PDF instead of writing
    ``analytics_report.pdf`` into the working directory.

It also exercises the account, settings, notification and data-lifecycle
routes that no other suite touches, so the hardening change is measured
against a wider slice of the app than the reporting contract it followed.

Every scenario runs in its own temp workdir with its own ``database.db`` and
its own ``app.py`` copy. The repository ``database.db``, ``app.py`` and
``templates/`` are never opened for writing.

Run:  python tests/test_account_and_data.py
"""
import ast
import hashlib
import http.cookiejar
import importlib.util
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "app.py")
TEMPLATES_DIR = os.path.join(REPO, "templates")
STATIC_DIR = os.path.join(REPO, "static")
GITIGNORE = os.path.join(REPO, ".gitignore")

# The commit the hardening change lands on top of: its app.py is the
# pre-hardening baseline these fixes are measured against.
PRE_HARDENING_REV = "ceb9e42"

# Functions the hardening change is allowed to move. Everything else in the
# baseline app.py must still be byte-identical (check 0j).
HARDENED_FUNCTIONS = ("send_email", "request_reset", "export_analytics")

# Dead files removed from the repository by this change (they were referenced
# by nothing: /dashboard renders index.html, and the reset flow renders
# reset.html).
REMOVED_TEMPLATES = ("dashboard.html", "reset_token.html")

# Kept on disk but deliberately uncommitted (see .gitignore): the suite must
# not treat them as unreachable templates.
IGNORED_TEMPLATES = {"index_backup.html"}

# Templates that exist only to be extended / included, never rendered directly.
LAYOUT_TEMPLATES = {"base.html", "app_base.html"}

# Every POST route in the app: all of them must reject a token-less request.
POST_ROUTES = (
    "/register", "/login", "/deposit", "/withdraw", "/send-money", "/mpesa",
    "/convert-currency", "/transfer-wallet", "/dashboard", "/clear",
    "/restore/1", "/delete/1", "/change-password", "/update-currency",
    "/save-settings", "/read-notifications", "/chat", "/request-reset",
    "/reset/sometoken",
)

PROTECTED_GETS = ("/dashboard", "/transactions", "/analytics", "/wallet",
                  "/archive", "/settings", "/ai-assistant",
                  "/export-transactions", "/export-analytics",
                  "/export-analytics-pdf")

TOKEN = "k23token"
USER = "geoffrey"
EMAIL = "geoffrey@example.com"
PASSWORD = "Finflow2026"

OUTCOME_RE = re.compile(r"your message has been sent|check your email|"
                        r"reset link|sent", re.I)

PASSED = []
FAILED = []


def check(label, cond, detail=""):
    (PASSED if cond else FAILED).append(label)
    print(("  PASS " if cond else "  FAIL ") + label
          + ("" if cond else "   [" + str(detail)[:400] + "]"))


def banner(text):
    print("")
    print("== " + text)


def read_text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()

# ── isolated workdirs + module loading ──────────────────────────────────────

_WORKDIRS = []


def make_wd(tag):
    wd = tempfile.mkdtemp(prefix="k23acct_" + tag + "_")
    shutil.copyfile(SRC, os.path.join(wd, "app.py"))
    shutil.copytree(TEMPLATES_DIR, os.path.join(wd, "templates"))
    shutil.copytree(STATIC_DIR, os.path.join(wd, "static"))
    _WORKDIRS.append(wd)
    return wd


def revision_wd(tag, rev):
    """Temp workdir holding app.py exactly as it was at ``rev``."""
    wd = tempfile.mkdtemp(prefix="k23acct_" + tag + "_")
    blob = subprocess.run(["git", "show", rev + ":app.py"], cwd=REPO,
                          capture_output=True, check=True).stdout
    with open(os.path.join(wd, "app.py"), "wb") as fh:
        fh.write(blob)
    shutil.copytree(TEMPLATES_DIR, os.path.join(wd, "templates"))
    _WORKDIRS.append(wd)
    return wd


_LOADED = []


def load_app(wd, tag):
    """Import a copy of the app (init_db() runs). Never the repository copy."""
    for name in list(_LOADED):
        sys.modules.pop(name, None)
    _LOADED[:] = []
    name = "k23acct_app_" + tag
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(wd, "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    _LOADED.append(name)
    spec.loader.exec_module(mod)
    return mod


def rows(wd, sql, args=()):
    conn = sqlite3.connect(os.path.join(wd, "database.db"))
    try:
        return [tuple(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def one(wd, sql, args=()):
    got = rows(wd, sql, args)
    return got[0][0] if got else None


def qmarks(amount, value):
    """INSERT helper that uses '?' placeholders like the SQLite branch."""
    return (amount, value)


def func_sources(path):
    text = read_text(path)
    lines = text.splitlines()
    out = {}
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.FunctionDef):
            out[node.name] = "\n".join(lines[node.lineno - 1:node.end_lineno])
    return out


def func_sha(path, name):
    return hashlib.sha256(
        func_sources(path).get(name, "").encode("utf-8")).hexdigest()[:16]


def sha256_file(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def route_paths(path):
    return sorted(set(re.findall(r"@app\.route\('([^']+)'", read_text(path))))


def csrf_client(mod, user=None):
    client = mod.app.test_client()
    with client.session_transaction() as sess:
        sess["_csrf_token"] = TOKEN
        if user:
            sess["user_id"] = user
            sess["username"] = USER
    return client


def post(client, path, data=None, follow=False):
    payload = {"_csrf_token": TOKEN}
    payload.update(data or {})
    return client.post(path, data=payload, follow_redirects=follow)


def register(client, username=USER, email=EMAIL, password=PASSWORD):
    return post(client, "/register", {"username": username, "email": email,
                                      "password": password})


def login(client, username=USER, password=PASSWORD, follow=False):
    """Log in, then reseed the session CSRF token.

    A successful /login calls session.clear(), which also drops the CSRF token
    the client was seeded with, so every later POST in the scenario would be
    refused with 403 unless the token is put back.
    """
    resp = post(client, "/login", {"username": username, "password": password},
                follow=follow)
    if resp.status_code == 302 or b"Total Balance" in resp.data:
        with client.session_transaction() as sess:
            sess["_csrf_token"] = TOKEN
    return resp


def body_of(response):
    return response.get_data(as_text=True)


def snapshot(root):
    """File list + sizes, to prove a scenario wrote nothing unexpected."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            full = os.path.join(dirpath, name)
            out[os.path.relpath(full, root)] = os.path.getsize(full)
    return out


def seed_ledger(wd, user_id, entries, start_id=100):
    """Insert ledger rows directly (writer paths are covered elsewhere)."""
    conn = sqlite3.connect(os.path.join(wd, "database.db"))
    try:
        for offset, entry in enumerate(entries):
            amount, ttype, category, kind = entry
            conn.execute(
                "INSERT INTO transactions (id, user_id, amount, currency,"
                " type, category, source, description, created_at,"
                " movement_kind) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (start_id + offset, user_id, amount, "KES", ttype, category,
                 "Cash", "seeded %d" % offset, "2026-02-0%d 09:00:00"
                 % (offset + 1), kind))
        conn.commit()
    finally:
        conn.close()


# ── section 0: source and asset guards ─────────────────────────────────────

def module_head(text):
    """Text outside every function/class body (the module-level block)."""
    tree = ast.parse(text)
    lines = text.splitlines()
    inside = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            inside.update(range(node.lineno, node.end_lineno + 1))
    return [l for i, l in enumerate(lines, 1) if i not in inside]


def section_source_guards(base_wd):
    banner("0. source and asset guards (the hardening change is the only diff)")
    src = read_text(SRC)
    srcmap = func_sources(SRC)
    base_map = func_sources(os.path.join(base_wd, "app.py"))

    check("0a app.py imports sys", bool(re.search(r"^import sys$", src, re.M)))

    check("0b stdout/stderr are reconfigured once, inside a guard",
          src.count("sys.stdout.reconfigure(") == 1
          and src.count("sys.stderr.reconfigure(") == 1
          and 'errors="replace"' in src
          and "except Exception:\n    pass" in src)

    bad_prints = [i + 1 for i, line in enumerate(src.splitlines())
                  if "print(" in line
                  and any(ord(ch) > 127 for ch in line)]
    check("0c no print statement in app.py carries non-ASCII (the reset 500"
          " cannot come back through a log line)", not bad_prints, bad_prints)

    email_src = srcmap["send_email"]
    check("0d send_email reports problems with ASCII and still returns False",
          email_src.count("return False") == 5
          and "print(\"[!] Missing SENDGRID_API_KEY\")" in email_src
          and "print(\"[!] Missing FROM_EMAIL\")" in email_src)

    export_src = srcmap["export_analytics"]
    check("0e export_analytics builds the report into a buffer",
          "SimpleDocTemplate(buf)" in export_src
          and "buf = BytesIO()" in export_src
          and "buf.seek(0)" in export_src)
    check("0f export_analytics never names a file on disk",
          "filename" not in export_src
          and 'SimpleDocTemplate("' not in export_src
          and ".build(" in export_src)
    check("0g export_analytics streams a PDF attachment",
          "send_file(\n        buf," in export_src
          and 'download_name="analytics_report.pdf"' in export_src
          and 'mimetype="application/pdf"' in export_src)

    missing = [t for t in REMOVED_TEMPLATES
               if os.path.exists(os.path.join(TEMPLATES_DIR, t))]
    check("0h the two unreferenced templates are gone", not missing, missing)

    rendered = set(re.findall(r'render_template\(\s*[\'"]([\w.\-]+)[\'"]', src))
    extended = set()
    for name in os.listdir(TEMPLATES_DIR):
        if name.endswith(".html"):
            extended.update(re.findall(
                r'\{%\s*(?:extends|include)\s*[\'"]([\w.\-]+)[\'"]',
                read_text(os.path.join(TEMPLATES_DIR, name))))
    orphans = [f for f in sorted(os.listdir(TEMPLATES_DIR))
               if f.endswith(".html") and f not in rendered
               and f not in extended and f not in LAYOUT_TEMPLATES
               and f not in IGNORED_TEMPLATES]
    check("0i every remaining template is reachable (rendered or extended)",
          not orphans, orphans)

    manifest = json.loads(read_text(os.path.join(STATIC_DIR, "manifest.json")))
    icons = manifest.get("icons", [])
    icon_ok = bool(icons) and all(
        os.path.exists(os.path.join(STATIC_DIR, i.get("src", "")))
        for i in icons)
    check("0j the manifest icon it advertises actually exists", icon_ok,
          [i.get("src") for i in icons])

    sw = read_text(os.path.join(STATIC_DIR, "service-worker.js"))
    check("0k the service worker is a real, versioned, page-safe cache",
          all(token in sw for token in (
              "CACHE_VERSION", "PRECACHE", "caches.open",
              'url.pathname.startsWith("/static/")',
              'request.method !== "GET"', "skipWaiting")), "")

    index = read_text(os.path.join(TEMPLATES_DIR, "index.html"))
    check("0l the dashboard still wires the manifest and the worker",
          "/static/manifest.json" in index
          and "/static/service-worker.js" in index)

    changed = sorted(n for n, text in srcmap.items()
                     if n in base_map and base_map[n] != text)
    added = sorted(n for n in srcmap if n not in base_map)
    dropped = sorted(n for n in base_map if n not in srcmap)
    check("0m exactly the three declared functions moved",
          changed == sorted(HARDENED_FUNCTIONS) and not added and not dropped,
          {"changed": changed, "added": added, "dropped": dropped})

    check("0n no route was added or removed",
          route_paths(SRC) == route_paths(os.path.join(base_wd, "app.py")),
          (route_paths(SRC), route_paths(os.path.join(base_wd, "app.py"))))

    base_head = [l for l in module_head(
        read_text(os.path.join(base_wd, "app.py"))) if l.strip()]
    cur_head = [l for l in module_head(src) if l.strip()]
    added_head = [l for l in cur_head if l not in base_head]
    removed_head = [l for l in base_head if l not in cur_head]
    allowed = all(
        l.strip() == "import sys" or "reconfigure" in l
        or l.strip() in ("try:", "pass") or "except Exception:" in l
        or l.lstrip().startswith("#") for l in added_head)
    check("0o the only module-level change is the guarded stream reconfigure",
          not removed_head and allowed and len(added_head) <= 12,
          {"added": added_head, "removed": removed_head})


# ── section 1: the reset 500 must not come back on a cp1252 console ────────

RESET_MESSAGE = "If the email exists, a reset link has been sent."


def server_env(encoding):
    env = dict(os.environ)
    for name in ("DATABASE_URL", "OPENAI_API_KEY", "SENDGRID_API_KEY",
                 "FROM_EMAIL"):
        env.pop(name, None)
    if encoding:
        env["PYTHONIOENCODING"] = encoding
    else:
        env.pop("PYTHONIOENCODING", None)
    return env


def boot(wd, env):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    runner = os.path.join(wd, "_server.py")
    with open(runner, "w", encoding="utf-8") as fh:
        fh.write("import sys\n"
                 "sys.path.insert(0, r'%s')\n"
                 "import app\n"
                 "app.app.run(host='127.0.0.1', port=%d, debug=False,"
                 " use_reloader=False)\n" % (wd, port))
    log_path = os.path.join(wd, "server.log")
    log = open(log_path, "w+b")
    proc = subprocess.Popen([sys.executable, runner], cwd=wd, env=env,
                            stdout=log, stderr=log)
    base = "http://127.0.0.1:%d" % port
    for _ in range(120):
        try:
            socket.create_connection(("127.0.0.1", port), 0.5).close()
            return proc, log, log_path, base
        except OSError:
            time.sleep(0.5)
    raise RuntimeError("server did not start in " + wd)


def stop(proc, log):
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
    log.close()


def web(base):
    """Minimal cookie-aware browser returning (status, body)."""
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(path, data=None):
        payload = (urllib.parse.urlencode(data).encode()
                   if data is not None else None)
        req = urllib.request.Request(base + path, data=payload)
        try:
            resp = opener.open(req)
            return resp.getcode(), resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    return request


def token_from(html):
    found = re.search(r'<input[^>]*_csrf_token[^>]*value="([^"]+)"', html)
    return found.group(1) if found else None


def reset_scenario(wd, env):
    """Register, log in, then ask for a reset exactly like the browser does."""
    proc, log, log_path, base = boot(wd, env)
    try:
        req = web(base)
        req("/register", {"username": USER, "email": EMAIL,
                          "password": PASSWORD,
                          "_csrf_token": token_from(req("/register")[1])})
        req("/login", {"username": USER, "password": PASSWORD,
                       "_csrf_token": token_from(req("/login")[1])})
        page = req("/dashboard")[1]
        status, body = req("/request-reset",
                           {"email": EMAIL, "_csrf_token": token_from(page)})
        unknown_page = req("/dashboard")[1]
        unknown_status, unknown_body = req(
            "/request-reset", {"email": "nobody@example.com",
                               "_csrf_token": token_from(unknown_page)})
        return {
            "status": status,
            "body": body,
            "unknown_status": unknown_status,
            "unknown_body": unknown_body,
            "otp": one(wd, "SELECT otp FROM users WHERE username=?", (USER,)),
            "token": one(wd, "SELECT reset_token FROM users WHERE username=?",
                         (USER,)),
            "trace": open(log_path, "rb").read().decode("utf-8", "replace"),
        }
    finally:
        stop(proc, log)


def section_reset_on_cp1252():
    banner("1. password reset under a cp1252 console (the Windows default)")
    env = server_env("cp1252")

    base_wd = revision_wd("resetbase", PRE_HARDENING_REV)
    before = reset_scenario(base_wd, env)
    check("1a the pre-hardening build still 500s on the reset request,"
          " so the regression is real",
          before["status"] == 500 and "UnicodeEncodeError" in before["trace"],
          (before["status"], before["trace"].strip().splitlines()[-1:]))
    check("1b the pre-hardening build had already stored the OTP before dying",
          bool(before["otp"]))

    wd = make_wd("resetfixed")
    after = reset_scenario(wd, env)
    check("1c the hardened build answers 200 on the same cp1252 console",
          after["status"] == 200, (after["status"], after["body"][:120]))
    check("1d the hardened server log has no traceback",
          "Traceback" not in after["trace"])
    check("1e the reset token and OTP are still stored",
          bool(after["otp"]) and bool(after["token"]))
    check("1f known and unknown addresses get the same generic answer",
          after["unknown_status"] == 200
          and RESET_MESSAGE in after["body"]
          and RESET_MESSAGE in after["unknown_body"],
          (after["unknown_status"], RESET_MESSAGE in after["body"]))

    # The whole flow, in-process, on the workdir the subprocess just used.
    mod = load_app(wd, "resetflow")
    client = csrf_client(mod)
    post(client, "/request-reset", {"email": EMAIL})
    token, otp = rows(wd, "SELECT reset_token, otp FROM users"
                          " WHERE username=?", (USER,))[0]
    page = client.get("/reset/%s" % token)
    check("1g the reset link renders the OTP form",
          page.status_code == 200 and b"otp" in page.data.lower(),
          page.status_code)
    done = post(client, "/reset/%s" % token,
                {"otp": otp, "password": "Finflow2027"})
    check("1h submitting the OTP + a new password succeeds",
          done.status_code in (200, 302), done.status_code)
    check("1i the reset token is consumed",
          one(wd, "SELECT reset_token FROM users WHERE username=?",
              (USER,)) is None)
    check("1j the new password logs in",
          b"Total Balance" in login(csrf_client(mod), password="Finflow2027",
                                    follow=True).data)
    check("1k the old password no longer logs in",
          b"Invalid login" in login(csrf_client(mod), password=PASSWORD).data)
    return wd


# ── section 2: /export-analytics must stream, never write into the cwd ─────

EXPORT_FILE = "analytics_report.pdf"


def export_scenario(wd, tag):
    """Run GET /export-analytics with the process cwd inside ``wd``."""
    mod = load_app(wd, tag)
    client = csrf_client(mod)
    register(client, USER, EMAIL, PASSWORD)
    login(client)
    user_id = one(wd, "SELECT id FROM users WHERE username=?", (USER,))
    seed_ledger(wd, user_id, [(1000.0, "income", "Salary", "external_in"),
                              (250.0, "expense", "Food", "external_out")])
    here = os.getcwd()
    os.chdir(wd)
    try:
        first = client.get("/export-analytics")
        left_after_first = sorted(f for f in os.listdir(wd)
                                  if f.endswith(".pdf"))
        second = client.get("/export-analytics")
        left_after_second = sorted(f for f in os.listdir(wd)
                                   if f.endswith(".pdf"))
        return {
            "mod": mod,
            "status": first.status_code,
            "body": first.data,
            "headers": dict(first.headers),
            "second_status": second.status_code,
            "second_size": len(second.data),
            "left_after_first": left_after_first,
            "left_after_second": left_after_second,
            "logged_out": client.get("/logout").status_code,
        }
    finally:
        os.chdir(here)


def section_export_streams():
    banner("2. /export-analytics streams its PDF (no file left behind)")

    base_wd = revision_wd("exportbase", PRE_HARDENING_REV)
    before = export_scenario(base_wd, "exportbase")
    check("2a pre-hardening: the endpoint returned the report",
          before["status"] == 200 and before["body"][:4] == b"%PDF",
          (before["status"], before["body"][:8]))
    check("2b pre-hardening: it dropped the PDF into the working directory",
          EXPORT_FILE in before["left_after_first"],
          before["left_after_first"])

    wd = make_wd("exportfixed")
    after = export_scenario(wd, "exportfixed")
    check("2c hardened: the endpoint still returns a PDF",
          after["status"] == 200 and after["body"][:4] == b"%PDF",
          (after["status"], after["body"][:8]))
    check("2d hardened: it is sent as a PDF attachment",
          "application/pdf" in after["headers"].get("Content-Type", "")
          and "attachment" in after["headers"].get("Content-Disposition", "")
          and EXPORT_FILE in after["headers"].get("Content-Disposition", ""),
          after["headers"].get("Content-Disposition"))
    check("2e hardened: the report is not empty",
          len(after["body"]) > 800, len(after["body"]))
    check("2f hardened: nothing is left in the working directory",
          not after["left_after_first"], after["left_after_first"])
    check("2g hardened: a second call works and still leaves nothing",
          after["second_status"] == 200 and not after["left_after_second"]
          and after["second_size"] > 800,
          (after["second_status"], after["left_after_second"]))
    check("2h the repository root never receives the file",
          not os.path.exists(os.path.join(REPO, EXPORT_FILE)))
    return wd


# ── section 3: account, currency and notification routes ───────────────────

def section_account_routes():
    banner("3. account, currency and notification routes")
    wd = make_wd("account")
    mod = load_app(wd, "account")
    client = csrf_client(mod)
    register(client)
    login(client)
    uid = one(wd, "SELECT id FROM users WHERE username=?", (USER,))

    page = client.get("/settings")
    check("3a /settings renders for the session user",
          page.status_code == 200 and b"currency" in page.data.lower(),
          page.status_code)

    post(client, "/update-currency", {"currency": "USD"})
    check("3b /update-currency stores the preferred currency",
          one(wd, "SELECT preferred_currency FROM users WHERE id=?",
              (uid,)) == "USD")
    post(client, "/save-settings", {"currency": "KES"})
    check("3c /save-settings stores the preferred currency",
          one(wd, "SELECT preferred_currency FROM users WHERE id=?",
              (uid,)) == "KES")

    mod.create_notification(uid, "seeded notice")
    unread = one(wd, "SELECT COUNT(*) FROM notifications"
                     " WHERE user_id=? AND is_read=0", (uid,))
    read = post(client, "/read-notifications")
    check("3d /read-notifications answers 204", read.status_code == 204,
          read.status_code)
    check("3e it marked the seeded notice read",
          unread == 1 and one(wd, "SELECT COUNT(*) FROM notifications"
                                  " WHERE user_id=? AND is_read=0",
                              (uid,)) == 0)

    wrong = post(client, "/change-password",
                 {"current_password": "NotMyPassword1",
                  "new_password": "Finflow2027",
                  "confirm_password": "Finflow2027"})
    # A rejected rotation answers with a redirect and never touches the hash:
    # the pre-rotation password still logs in afterwards.
    check("3f a wrong current password changes nothing",
          wrong.status_code in (200, 302)
          and b"Total Balance" in login(csrf_client(mod), follow=True).data,
          wrong.status_code)

    good = post(client, "/change-password",
                {"current_password": PASSWORD,
                 "new_password": "Finflow2027",
                 "confirm_password": "Finflow2027"})
    check("3g a correct current password rotates it",
          good.status_code in (200, 302)
          and b"Total Balance" in login(csrf_client(mod),
                                        password="Finflow2027",
                                        follow=True).data,
          good.status_code)
    check("3h the previous password stops working",
          b"Invalid login" in login(csrf_client(mod)).data)
    return wd


# ── section 4: data lifecycle (/clear, /delete, /archive, /restore) ───────

def seed_archived(wd, row_id, user_id, amount, kind="external_out",
                  category="Other"):
    conn = sqlite3.connect(os.path.join(wd, "database.db"))
    try:
        conn.execute(
            "INSERT INTO archived_transactions (id, user_id, amount,"
            " currency, type, category, source, description, created_at,"
            " movement_kind) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (row_id, user_id, amount, "KES", "expense", category, "Cash",
             "archived row", "2026-01-31 07:00:00", kind))
        conn.commit()
    finally:
        conn.close()


def section_data_lifecycle():
    banner("4. data lifecycle (/clear, /delete, /archive, /restore)")
    wd = make_wd("data")
    mod = load_app(wd, "data")
    mine = csrf_client(mod)
    register(mine)
    login(mine)
    uid = one(wd, "SELECT id FROM users WHERE username=?", (USER,))

    theirs = csrf_client(mod)
    register(theirs, "wanjiru", "wanjiru@example.com")
    login(theirs, "wanjiru")
    oid = one(wd, "SELECT id FROM users WHERE username=?", ("wanjiru",))

    mine.get("/add-wallet/USD")
    post(mine, "/deposit", {"amount": "500"})
    seed_ledger(wd, uid, [(120.0, "expense", "Food", "external_out"),
                          (60.0, "expense", "Transport", "external_out")])
    seed_ledger(wd, oid, [(900.0, "income", "Salary", "external_in")],
                start_id=900)
    mod.create_notification(uid, "notice for the clear test")
    my_wallet = one(wd, "SELECT SUM(balance) FROM wallets WHERE user_id=?",
                    (uid,))
    their_wallet = one(wd, "SELECT SUM(balance) FROM wallets WHERE user_id=?",
                       (oid,))

    check("4a /archive renders for the session user",
          mine.get("/archive").status_code == 200)
    seed_archived(wd, 777, uid, 77.0)
    page = body_of(mine.get("/archive"))
    # archive.html renders "{{ t.currency }} {{ t.amount }}" side by side, so
    # the rendered figure is whitespace-split ("KES 77.0"); match the pieces
    # rather than a locale-formatted number.
    check("4b archived rows are listed",
          "KES" in page and "77" in page and "archived row" in page,
          page[:200])

    ledger_before = one(wd, "SELECT COUNT(*) FROM transactions"
                            " WHERE user_id=?", (uid,))
    post(mine, "/restore/777")
    check("4c /restore moves the row back into the ledger",
          one(wd, "SELECT COUNT(*) FROM transactions WHERE user_id=?",
              (uid,)) == ledger_before + 1)
    check("4d the archived copy is gone",
          one(wd, "SELECT COUNT(*) FROM archived_transactions WHERE id=?",
              (777,)) == 0)
    check("4e restore leaves the wallet balances alone",
          one(wd, "SELECT SUM(balance) FROM wallets WHERE user_id=?",
              (uid,)) == my_wallet)

    txid = one(wd, "SELECT id FROM transactions WHERE user_id=?"
                   " ORDER BY id LIMIT 1", (uid,))
    notices = one(wd, "SELECT COUNT(*) FROM notifications WHERE user_id=?",
                  (uid,))
    post(mine, "/delete/%d" % txid)
    check("4f /delete removes the row outright (no archive copy)",
          one(wd, "SELECT COUNT(*) FROM transactions WHERE id=?",
              (txid,)) == 0
          and one(wd, "SELECT COUNT(*) FROM archived_transactions"
                      " WHERE user_id=?", (uid,)) == 0)
    check("4g /delete notifies the owner",
          one(wd, "SELECT COUNT(*) FROM notifications WHERE user_id=?",
              (uid,)) == notices + 1)
    check("4h /delete leaves the wallet balances alone",
          one(wd, "SELECT SUM(balance) FROM wallets WHERE user_id=?",
              (uid,)) == my_wallet)

    check("4i /clear is refused without a CSRF token",
          mine.post("/clear").status_code == 403,
          mine.post("/clear").status_code)
    post(mine, "/clear")
    check("4j /clear empties the caller's ledger",
          one(wd, "SELECT COUNT(*) FROM transactions WHERE user_id=?",
              (uid,)) == 0)
    check("4k /clear empties the caller's archive",
          one(wd, "SELECT COUNT(*) FROM archived_transactions"
                  " WHERE user_id=?", (uid,)) == 0)
    check("4l /clear empties the caller's notifications",
          one(wd, "SELECT COUNT(*) FROM notifications WHERE user_id=?",
              (uid,)) == 0)
    check("4m /clear zeroes the caller's wallets",
          one(wd, "SELECT SUM(balance) FROM wallets WHERE user_id=?",
              (uid,)) == 0)
    check("4n /clear leaves the other user's data untouched",
          one(wd, "SELECT COUNT(*) FROM transactions WHERE user_id=?",
              (oid,)) == 1
          and one(wd, "SELECT SUM(balance) FROM wallets WHERE user_id=?",
                  (oid,)) == their_wallet)
    return wd


# ── section 5: auth guards, CSRF and registration rules ───────────────────

def section_auth_guards():
    banner("5. auth guards, CSRF coverage and registration rules")
    wd = make_wd("guards")
    mod = load_app(wd, "guards")
    anon = mod.app.test_client()

    unguarded = []
    for path in PROTECTED_GETS:
        resp = anon.get(path)
        if resp.status_code != 302 or "/login" not in resp.headers.get(
                "Location", ""):
            unguarded.append((path, resp.status_code))
    check("5a every protected GET sends an anonymous visitor to /login",
          not unguarded, unguarded)

    refused = []
    filler = {"username": "x", "email": "x@example.com",
              "password": "Finflow2026", "amount": "1", "message": "hi",
              "otp": "000000", "currency": "KES",
              "current_password": "x", "new_password": "y",
              "confirm_password": "y", "email_address": "x@example.com"}
    for path in POST_ROUTES:
        resp = anon.post(path, data=filler)
        if resp.status_code != 403:
            refused.append((path, resp.status_code))
    check("5b every POST route refuses a token-less request with 403",
          not refused, refused)

    client = csrf_client(mod)
    weak = post(client, "/register", {"username": "weak",
                                      "email": "weak@example.com",
                                      "password": "weakpass"})
    check("5c a weak password is refused", b"Weak password" in weak.data)

    first = register(client, "dupe", "dupe@example.com")
    check("5d the first registration succeeds",
          first.status_code == 302, first.status_code)
    dup_user = register(client, "dupe", "other@example.com")
    check("5e a duplicate username is refused",
          b"already exists" in dup_user.data)
    dup_mail = register(client, "dupe2", "dupe@example.com")
    check("5f a duplicate email is refused", b"already exists" in dup_mail.data)

    wrong = login(client, "dupe", "WrongPassword1")
    check("5g a wrong password is refused", b"Invalid login" in wrong.data)
    right = login(client, "dupe", PASSWORD)
    check("5h valid credentials reach the dashboard",
          b"Total Balance" in right.data or right.status_code == 302,
          right.status_code)
    client.get("/logout")
    check("5i /logout leaves the visitor anonymous again",
          client.get("/dashboard").status_code == 302)
    return wd


# ── section 6: repository safety ──────────────────────────────────────────

def section_safety(repo_state):
    banner("6. safety (the repository artefacts are untouched)")
    check("6a the repository database.db is byte-identical",
          sha256_file(os.path.join(REPO, "database.db")) == repo_state["db"])
    check("6b the repository app.py is byte-identical",
          sha256_file(SRC) == repo_state["app"])
    check("6c the repository templates are byte-identical",
          snapshot(TEMPLATES_DIR) == repo_state["templates"])
    check("6d every scenario ran in its own workdir",
          len(set(_WORKDIRS)) == len(_WORKDIRS)
          and all(os.path.dirname(w) == tempfile.gettempdir()
                  for w in _WORKDIRS), len(_WORKDIRS))
    check("6e no scenario ever used the repository as a workdir",
          REPO not in _WORKDIRS)
    check("6f the repository received no exported report",
          not os.path.exists(os.path.join(REPO, EXPORT_FILE)))
    strays = [f for f in os.listdir(REPO)
              if f.endswith((".pdf", ".csv", ".xlsx"))]
    check("6g the repository root has no analysis leftovers", not strays,
          strays)



# ── runner ────────────────────────────────────────────────────────────────

def main():
    # the app prints emoji; a redirected cp1252 console must not break us
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print("FinFlow hardening regression suite")
    print("repo:                " + REPO)
    print("baseline revision:   " + PRE_HARDENING_REV)
    print("repo database.db is never opened by this suite")

    for name in ("DATABASE_URL", "OPENAI_API_KEY", "SENDGRID_API_KEY",
                 "FROM_EMAIL"):
        os.environ.pop(name, None)

    repo_state = {
        "db": sha256_file(os.path.join(REPO, "database.db")),
        "app": sha256_file(SRC),
        "templates": snapshot(TEMPLATES_DIR),
    }

    base_wd = revision_wd("baseline", PRE_HARDENING_REV)
    section_source_guards(base_wd)
    section_reset_on_cp1252()
    section_export_streams()
    section_account_routes()
    section_data_lifecycle()
    section_auth_guards()
    section_safety(repo_state)

    for wd in _WORKDIRS:
        shutil.rmtree(wd, ignore_errors=True)

    print("")
    print("passed: %d   failed: %d" % (len(PASSED), len(FAILED)))
    for label in FAILED:
        print("  FAILED: " + label)
    print("ALL CHECKS PASSED" if not FAILED else "CHECKS FAILED")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())

