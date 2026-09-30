"""K22 Phase 3 PDF reporting-classification suite.

Covers both halves of the phase:

Part A - the route works. The pre-existing ``story`` lifecycle defect
(``UnboundLocalError``: the chart/table sections used ``story`` long
before ``story = []`` ran, and Windows then refused to delete the still-
open temp chart files) is gone; every user - populated or empty - gets a
real, parseable PDF: HTTP 200, application/pdf, ``%PDF ... %%EOF``.

Part B - the PDF classifies ledger movements through the shared K22
reporting contract instead of ``type == "income"`` / ``else: expenses``:

  * internal transfers, currency conversions and unknown history add
    nothing to headline income, nothing to headline expenses and
    nothing to spending categories
  * spending categories only collect cashflow expenses, with missing or
    blank categories normalized to ``Other`` by the shared helper
  * the monthly series include cashflow income and cashflow expenses
    only; excluded rows contribute to neither series
  * ``balance`` stays ``income - expenses`` (never wallet-derived)
  * layout, headings, tables, title and the known (unchanged)
    alphabetical month ordering are preserved

Verification method: PDF text is read back with a small *independent*
extractor (ASCII85 + zlib stream decoding, then literal-string scan) -
never via app.py internals - and every expected value is computed
locally from the approved contract, never by calling the functions under
test. Money matching accepts equivalent representations (1,000.00 /
1000.00 / 1,000).

Safety: every scenario runs against a *copy* of ``app.py`` in an
isolated temp workdir with its own throwaway ``database.db``. The
repository database is never opened, never migrated, never modified.

Run:  python tests/test_pdf_reporting.py
"""

import ast
import base64
import hashlib
import importlib.util
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zlib

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "app.py")
TEMPLATES_DIR = os.path.join(REPO, "templates")
BASELINE_REV = "68e89f9"

# The economic ledger this suite drives, written straight to the table so
# the classification helpers (not a writer) are what is under test:
#   (id, amount, currency, type, category, source, description,
#    created_at, kind)
LEDGER = (
    (1, 1000.0, "KES", "income", "Salary", "External", "salary credit",
     "2026-01-05 10:00:00", "external_in"),
    (2, 250.0, "KES", "expense", " Food ", "Mobile", "lunch food",
     "2026-01-06 10:00:00", "external_out"),
    (3, 300.0, "KES", "income", "Side Gig", "Cash", "side gig",
     "2026-02-07 10:00:00", "user_record"),
    (4, 40.0, "KES", "expense", "Coffee", "Mobile", "coffee",
     "2026-02-08 10:00:00", "user_record"),
    (5, 500.0, "KES", "income", "Transfer", "Wallet", "internal in",
     "2026-03-09 10:00:00", "internal"),
    (6, 500.0, "KES", "expense", "Transfer", "Wallet", "internal out",
     "2026-03-10 10:00:00", "internal"),
    (7, 100.0, "KES", "expense", "Conversion", "Wallet", "to USD",
     "2026-04-11 10:00:00", "conversion"),
    (8, 100.0, "KES", "income", "Conversion", "Wallet", "from USD",
     "2026-04-12 10:00:00", "conversion"),
    (9, 999.0, "KES", "expense", "Legacy", "Card", "legacy spend",
     "2026-05-13 10:00:00", "unknown"),
    (10, 60.0, "KES", "expense", None, "Card", "no category",
     "2026-05-14 10:00:00", "external_out"),
    (11, 30.0, "KES", "expense", "   ", "Card", "blank category",
     "2026-06-15 10:00:00", "external_out"),
    (12, 77.0, "KES", "expense", "Transfer", "Wallet", "internal out",
     "2026-07-16 10:00:00", "internal"),
)

# Approved-contract expectations for LEDGER, stated literally here and
# recomputed independently by contract_expectation() below.
CONTRACT_INCOME = 1300.0            # 1000 external_in + 300 user_record
CONTRACT_EXPENSES = 380.0           # 250 + 40 + 60 + 30 cashflow rows
CONTRACT_BALANCE = 920.0
CONTRACT_CATEGORIES = {"Food": 250.0, "Coffee": 40.0, "Other": 90.0}
# Monthly nets (income - expenses) per surviving month label. Mar/Apr/Jul
# hold only excluded rows and must not appear at all.
CONTRACT_MONTH_NETS = {
    "Jan 2026": 750.0, "Feb 2026": 260.0,
    "May 2026": -60.0, "Jun 2026": -30.0,
}
# The PDF sorts month labels alphabetically (known, out-of-scope defect
# this phase must NOT fix); the row order is pinned as-is.
CONTRACT_MONTH_ORDER = ["Feb 2026", "Jan 2026", "Jun 2026", "May 2026"]

# What the old ``type == "income"`` / ``else: expenses`` shape produced
# over LEDGER (documenting that the migration is observable).
BUGGED_INCOME = 1900.0
BUGGED_EXPENSES = 2056.0

# Wallet seed deliberately unrelated to the ledger, so a wallet-derived
# balance would be trivially distinguishable from income - expenses.
WALLET_SEED = 123456.78

# SHA-256 (first 16 hex) of app.py functions recorded at the START of
# Phase 3. Analytics, the other reporting surfaces and the four public K22
# helpers must not move one byte during this phase. Anything a later phase
# legitimately moves is handed over to that phase's suite instead of being
# silently dropped: `chat` moved in Phase 4 (tests/test_chat_reporting.py),
# and `dashboard` plus the two shared row extractors moved in Phase 5
# (tests/test_dashboard_reporting.py pins all three from here on).
PHASE_START_SHA256 = {
    "analytics": "1d79632a1d377ea4",
    "is_cashflow_income": "642556ad3c707a94",
    "is_cashflow_expense": "9500c143d585350b",
    "is_cashflow_excluded": "566449f49d9bf02b",
    "get_transaction_category": "8b91c0a1c24c9937",
}

PASSED = []
FAILED = []


def check(label, cond, detail=""):
    (PASSED if cond else FAILED).append(label)
    print(("  PASS " if cond else "  FAIL ") + label
          + ("" if cond else "   [" + str(detail)[:400] + "]"))


def banner(text):
    print("")
    print("== " + text)

# ── isolated workdir + module loading ───────────────────────────────────────

def make_wd(tag):
    wd = tempfile.mkdtemp(prefix="k22pdf_" + tag + "_")
    shutil.copyfile(SRC, os.path.join(wd, "app.py"))
    shutil.copytree(TEMPLATES_DIR, os.path.join(wd, "templates"))
    return wd


def baseline_wd(tag):
    """Temp workdir holding the pre-K21/pre-K22 app.py from BASELINE_REV."""
    wd = tempfile.mkdtemp(prefix="k22pdf_" + tag + "_")
    blobs = subprocess.run(
        ["git", "show", BASELINE_REV + ":app.py"],
        cwd=REPO, capture_output=True, check=True,
    ).stdout
    with open(os.path.join(wd, "app.py"), "wb") as fh:
        fh.write(blobs)
    shutil.copytree(TEMPLATES_DIR, os.path.join(wd, "templates"))
    return wd


_LOADED = []


def load_app(wd, tag):
    """Import the app copy (init_db() runs on import). Never the repo copy."""
    for name in list(_LOADED):
        sys.modules.pop(name, None)
    _LOADED[:] = []
    name = "k22pdf_app_" + tag
    path = os.path.join(wd, "app.py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    _LOADED.append(name)
    spec.loader.exec_module(mod)
    return mod


def raw(wd):
    return sqlite3.connect(os.path.join(wd, "database.db"))


def rows(wd, sql):
    conn = raw(wd)
    try:
        return [tuple(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


# ── seeding ─────────────────────────────────────────────────────────────────

LEDGER_SQL_KIND = (
    "INSERT INTO transactions (id, user_id, amount, currency, type,"
    " category, source, description, created_at, movement_kind)"
    " VALUES (?,?,?,?,?,?,?,?,?,?)")
LEDGER_SQL_PLAIN = (
    "INSERT INTO transactions (id, user_id, amount, currency, type,"
    " category, source, description, created_at)"
    " VALUES (?,?,?,?,?,?,?,?,?)")


def seed_users(wd):
    conn = raw(wd)
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO users (id, username, password, email,"
                    " preferred_currency) VALUES (?,?,?,?,?)",
                    (1, "alice", "x", "alice@example.com", "KES"))
        cur.execute("INSERT INTO wallets (user_id, currency, balance)"
                    " VALUES (?,?,?)", (1, "KES", WALLET_SEED))
        cur.execute("INSERT INTO wallets (user_id, currency, balance)"
                    " VALUES (?,?,?)", (1, "USD", 42.5))
        conn.commit()
    finally:
        conn.close()


def seed_ledger(wd, ledger=LEDGER, with_kind=True):
    conn = raw(wd)
    try:
        cur = conn.cursor()
        if with_kind:
            cur.executemany(LEDGER_SQL_KIND,
                            [(r[0], 1) + r[1:] for r in ledger])
        else:
            cur.executemany(LEDGER_SQL_PLAIN,
                            [(r[0], 1) + r[1:8] for r in ledger])
        conn.commit()
    finally:
        conn.close()


# ── independent PDF text extraction (ASCII85 + zlib + literal strings) ──────

def _unescape_pdf_string(s):
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt in "nrtbf":
                out.append({"n": "\n", "r": "\r", "t": "\t",
                            "b": "\b", "f": "\f"}[nxt])
                i += 2
                continue
            if nxt in "()\\":
                out.append(nxt)
                i += 2
                continue
            if nxt.isdigit():
                m = re.match(r"[0-7]{1,3}", s[i + 1:])
                out.append(chr(int(m.group(0), 8)))
                i += 1 + len(m.group(0))
                continue
            out.append(nxt)
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _inflate(raw):
    """reportlab chains filters: plain zlib, or ASCII85 then zlib."""
    for attempt in (raw, raw.strip()):
        try:
            return zlib.decompressobj().decompress(attempt)
        except zlib.error:
            pass
        try:
            a85 = attempt[:-2] if attempt.endswith(b"~>") else attempt
            if a85.startswith(b"<~"):
                a85 = a85[2:]
            return zlib.decompressobj().decompress(base64.a85decode(a85))
        except Exception:
            pass
    return None


def pdf_text(data):
    """Extract every literal string from the report's text streams."""
    pieces = []
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", data, re.S):
        content = _inflate(m.group(1))
        if content is None or b"BT" not in content:
            continue
        for sm in re.finditer(rb"\((?:\\.|[^\\()])*\)", content):
            pieces.append(
                _unescape_pdf_string(sm.group(0)[1:-1].decode("latin-1")))
    return " ".join(pieces)


# ── reading the generated report ────────────────────────────────────────────

NUM = r"-?[\d,]+(?:\.\d+)?"          # 1,000.00 / 1000.00 / 1,000 / 1000


def table_value(text, label):
    """Value of a summary-table row: ``<label>  KES  <amount>``."""
    m = re.search(re.escape(label) + r"\s+KES\s+(" + NUM + r")(?![\d.])",
                  text)
    return None if m is None else float(m.group(1).replace(",", ""))


def parse_categories(text):
    """Spending-by-Category table rows -> {name: amount} (order kept)."""
    parts = text.split("Spending by Category", 1)
    if len(parts) < 2:
        return None
    region = parts[1].split("AI Financial Insights", 1)[0]
    region = region.replace("Category Amount", "", 1)
    out = {}
    for m in re.finditer(r"([A-Z][A-Za-z ]*?)\s+KES\s+(" + NUM
                         + r")(?![\d.])", region):
        out[m.group(1).strip()] = float(m.group(2).replace(",", ""))
    return out


def monthly_rows(text):
    """Monthly Net Savings rows as ordered [(label, net), ...]."""
    parts = text.split("Monthly Net Savings", 1)
    if len(parts) < 2:
        return None
    region = parts[1].split("Overall Trend", 1)[0]
    out = []
    for m in re.finditer(r"([A-Z][a-z]{2} \d{4}|Unknown)\s+KES\s+(" + NUM
                         + r")(?![\d.])", region):
        out.append((m.group(1), float(m.group(2).replace(",", ""))))
    return out


def fetch_pdf(mod, user_id=1):
    client = mod.app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["username"] = "alice"
        sess["_csrf_token"] = "k22token"
    resp = client.get("/export-analytics-pdf")
    data = resp.get_data()
    text = pdf_text(data) if data[:5] == b"%PDF-" else ""
    return {
        "status": resp.status_code,
        "mimetype": resp.mimetype,
        "data": data,
        "text": text,
        "income": table_value(text, "Income"),
        "expenses": table_value(text, "Expenses"),
        "balance": table_value(text, "Balance"),
        "categories": parse_categories(text),
        "month_rows": monthly_rows(text),
    }


def fetch_pdf_no_session(mod):
    client = mod.app.test_client()
    resp = client.get("/export-analytics-pdf")
    return resp.status_code, resp.headers.get("Location", "")


# ── independent expectation layer (contract rules, never app.py) ────────────

def contract_expectation(wd):
    """Recompute the approved contract straight from the stored rows."""
    from datetime import datetime
    recs = rows(wd, "SELECT amount, type, category, movement_kind,"
                    " created_at FROM transactions"
                    " WHERE user_id=1 ORDER BY id")
    income = 0.0
    expenses = 0.0
    categories = {}
    monthly = {}
    for amount, ttype, category, kind, created_at in recs:
        amount = float(amount)
        cashflow_in = kind == "external_in" or (
            kind == "user_record" and ttype == "income")
        cashflow_out = kind == "external_out" or (
            kind == "user_record" and ttype == "expense")
        if not (cashflow_in or cashflow_out):
            continue                      # internal / conversion / unknown
        try:
            label = datetime.strptime(str(created_at)[:10],
                                      "%Y-%m-%d").strftime("%b %Y")
        except ValueError:
            label = "Unknown"
        bucket = monthly.setdefault(label, [0.0, 0.0])
        if cashflow_in:
            income += amount
            bucket[0] += amount
        else:
            expenses += amount
            bucket[1] += amount
            name = "" if category is None else str(category).strip()
            name = name or "Other"
            categories[name] = categories.get(name, 0.0) + amount
    nets = {m: inc - exp for m, (inc, exp) in monthly.items()}
    return {
        "income": income,
        "expenses": expenses,
        "balance": income - expenses,
        "categories": categories,
        "nets": nets,
        "months_order": sorted(monthly),
    }


# ── source-level guards ─────────────────────────────────────────────────────

def read_text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def func_sources(path):
    text = read_text(path)
    lines = text.splitlines()
    out = {}
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.FunctionDef):
            out[node.name] = "\n".join(lines[node.lineno - 1:node.end_lineno])
    return out


def func_sha(path, name):
    src = func_sources(path).get(name, "")
    return hashlib.sha256(src.encode("utf-8")).hexdigest()[:16]


def _guard_ranges(fn, guard_name):
    return [(n.lineno, n.end_lineno) for n in ast.walk(fn)
            if isinstance(n, ast.If) and guard_name in ast.unparse(n.test)]


def unguarded_accumulations(path, func_name):
    """Every income/expense accumulation must sit inside its K22 guard.

    Covers both ``income += amount`` and the monthly series'
    ``monthly_income[month] = ...`` assignments plus the category
    accumulator, so a generic ``else: expenses`` (or an unguarded monthly
    else) is structurally impossible to reintroduce unnoticed.
    """
    helpers = {"income": "is_cashflow_income",
               "expenses": "is_cashflow_expense"}
    fn = [n for n in ast.walk(ast.parse(read_text(path)))
          if isinstance(n, ast.FunctionDef) and n.name == func_name][0]
    guards = {w: _guard_ranges(fn, g) for w, g in helpers.items()}
    bad = []
    for node in ast.walk(fn):
        word = None
        if isinstance(node, ast.AugAssign):
            target = ast.unparse(node.target)
            if "income" in target:
                word = "income"
            elif "expense" in target:
                word = "expenses"
            elif target.startswith("category_data["):
                word = "expenses"
        elif isinstance(node, ast.Assign) and node.targets:
            target = ast.unparse(node.targets[0])
            if target.startswith("monthly_income["):
                word = "income"
            elif target.startswith("monthly_expenses["):
                word = "expenses"
        if word is None:
            continue
        if not any(a <= node.lineno and node.end_lineno <= b
                   for a, b in guards[word]):
            bad.append("line %d: %s" % (node.lineno, ast.unparse(node)))
    return bad


def first_line(text, *fragments):
    for i, line in enumerate(text.splitlines(), 1):
        if any(f in line for f in fragments):
            return i
    return None


def section_source_guards(base_wd):
    banner("0. source guards (Part A lifecycle + Part B contract shape)")
    srcmap = func_sources(SRC)
    base_src = func_sources(os.path.join(base_wd, "app.py"))
    fn = srcmap["export_analytics_pdf"]
    base_fn = base_src["export_analytics_pdf"]

    assign = first_line(fn, "story = []")
    use = first_line(fn, "story.append(", "doc.build(story)")
    s_assign = first_line(fn, "styles = getSampleStyleSheet()")
    s_use = first_line(fn, "styles[")
    check("0a story and styles are initialized before any possible use",
          assign is not None and use is not None
          and s_assign is not None and s_use is not None
          and assign < use and s_assign < s_use,
          (assign, use, s_assign, s_use))
    check("0b exactly one story = [] (no late re-init can wipe sections)",
          fn.count("story = []") == 1, fn.count("story = []"))

    b_assign = first_line(base_fn, "story = []")
    b_use = first_line(base_fn, "story.append(", "doc.build(story)")
    check("0c baseline " + BASELINE_REV + " still carries the story defect"
          " (use before init) - it pre-dates K21/K22",
          b_assign is not None and b_use is not None and b_use < b_assign,
          (b_assign, b_use))

    check("0d no raw type-field branch left in the PDF surface",
          "['type'] ==" not in fn and '["type"] ==' not in fn)
    check("0e classifies through the shared K22 helpers",
          all(h in fn for h in ("is_cashflow_income", "is_cashflow_expense",
                                "get_transaction_category")))

    bad = unguarded_accumulations(SRC, "export_analytics_pdf")
    check("0f every income/expense accumulation sits inside a K22"
          " cashflow guard", not bad, bad)

    check("0g balance stays income - expenses (never wallet-derived)",
          "balance = income - expenses" in fn
          and "FROM wallets" not in fn
          and "get_wallet_balance" not in fn)

    check("0h alphabetical month ordering left exactly as-is"
          " (out-of-scope defect NOT fixed)",
          "months = sorted(" in fn
          and "set(monthly_income.keys())" in fn
          and "| set(monthly_expenses.keys())" in fn)

    fields = set()
    for node in ast.walk(ast.parse(fn)):
        if (isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id == "t"
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)):
            fields.add(node.slice.value)
    check("0i rows read only for value/date fields - no kind inference"
          " from description/source/category/type",
          fields == {"amount", "created_at"}, sorted(fields))

    nested = [type(n).__name__ for n in ast.walk(ast.parse(fn))
              if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
    check("0j no PDF-specific classifier is defined inside the surface",
          nested == ["FunctionDef"], nested)

    moved = [name for name, sha in sorted(PHASE_START_SHA256.items())
             if func_sha(SRC, name) != sha]
    check("0k analytics and the four public K22 helpers are"
          " byte-identical to the Phase 3 starting state",
          not moved, moved)


# ── 1. the route actually works (Part A) ────────────────────────────────────

def repo_db_sha():
    with open(os.path.join(REPO, "database.db"), "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def section_route_works():
    banner("1. the PDF route works (Part A)")
    wd = make_wd("route")
    mod = load_app(wd, "route")
    seed_users(wd)
    seed_ledger(wd)
    page = fetch_pdf(mod)

    check("1a HTTP 200", page["status"] == 200, page["status"])
    check("1b content type is application/pdf",
          page["mimetype"] == "application/pdf", page["mimetype"])
    check("1c body starts with the %PDF header",
          page["data"][:5] == b"%PDF-", page["data"][:16])
    check("1d body is non-empty", len(page["data"]) > 1000,
          len(page["data"]))
    check("1e document terminates with %%EOF (parsable)",
          b"%%EOF" in page["data"], page["data"][-40:])
    check("1f independent parser extracts the report text",
          bool(page["text"])
          and "FINANCIAL ANALYTICS REPORT" in page["text"],
          page["text"][:120])
    markers = ("FINANCIAL ANALYTICS REPORT", "EXECUTIVE SUMMARY",
               "Monthly Net Savings", "Spending by Category",
               "AI Financial Insights", "Generated by FinFlow",
               "Prepared For")
    check("1g layout markers all preserved (title/headings/footer)",
          all(m in page["text"] for m in markers),
          [m for m in markers if m not in page["text"]])

    wd_e = make_wd("route_empty")
    mod_e = load_app(wd_e, "route_empty")
    seed_users(wd_e)
    page_e = fetch_pdf(mod_e)
    check("1h empty-ledger user also gets a valid PDF",
          page_e["status"] == 200 and page_e["data"][:5] == b"%PDF-"
          and len(page_e["data"]) > 1000,
          (page_e["status"], len(page_e["data"])))
    check("1i empty ledger reports 0.00 income/expenses/balance",
          page_e["income"] == 0.0 and page_e["expenses"] == 0.0
          and page_e["balance"] == 0.0,
          (page_e["income"], page_e["expenses"], page_e["balance"]))

    code, loc = fetch_pdf_no_session(mod_e)
    check("1j existing unauthenticated contract kept (redirect /login)",
          code == 302 and loc.endswith("/login"), (code, loc))
    return page, page_e


# ── 2. one reviewed movement at a time ──────────────────────────────────────

_PROBE_N = [0]


def row(ident, amount, ttype, category, kind,
        created_at="2026-01-15 09:00:00", source="Src", description="d"):
    return (ident, amount, "KES", ttype, category, source, description,
            created_at, kind)


def probe_pdf(spec):
    """Run /export-analytics-pdf over a ledger of reviewed rows only."""
    _PROBE_N[0] += 1
    tag = "probe_%d" % _PROBE_N[0]
    wd = make_wd(tag)
    mod = load_app(wd, tag)
    seed_users(wd)
    seed_ledger(wd, spec)
    page = fetch_pdf(mod)
    page["wd"] = wd
    return page


def section_isolated_matrix():
    banner("2. single-movement matrix (one reviewed row per scenario)")

    # 1. external income -> income only
    p = probe_pdf((row(1, 1000.0, "income", "Salary", "external_in"),))
    check("2a external_in income -> income 1,000.00",
          p["income"] == 1000.0, p["income"])
    check("2b external_in income -> expenses 0.00",
          p["expenses"] == 0.0, p["expenses"])
    check("2c external_in income -> no spending category",
          p["categories"] == {}, p["categories"])
    check("2d external_in income -> Jan 2026 net 1,000.00",
          p["month_rows"] == [("Jan 2026", 1000.0)], p["month_rows"])

    # 2. external expense -> expenses + category
    p = probe_pdf((row(1, 300.0, "expense", "Food", "external_out"),))
    check("2e external_out expense -> expenses 300.00",
          p["expenses"] == 300.0, p["expenses"])
    check("2f external_out expense -> income 0.00",
          p["income"] == 0.0, p["income"])
    check("2g external_out expense -> Food spending 300.00",
          p["categories"] == {"Food": 300.0}, p["categories"])
    check("2h external_out expense -> Jan 2026 net -300.00",
          p["month_rows"] == [("Jan 2026", -300.0)], p["month_rows"])

    # 3. user-record income -> income
    p = probe_pdf((row(1, 500.0, "income", "Side Gig", "user_record",
                       created_at="2026-02-10 09:00:00"),))
    check("2i user_record income -> income 500.00",
          p["income"] == 500.0, p["income"])
    check("2j user_record income -> expenses 0.00",
          p["expenses"] == 0.0, p["expenses"])
    check("2k user_record income -> no spending category",
          p["categories"] == {}, p["categories"])
    check("2l user_record income -> Feb 2026 net 500.00",
          p["month_rows"] == [("Feb 2026", 500.0)], p["month_rows"])

    # 4. user-record expense -> expenses + category
    p = probe_pdf((row(1, 200.0, "expense", "Rent", "user_record",
                       created_at="2026-02-11 09:00:00"),))
    check("2m user_record expense -> expenses 200.00",
          p["expenses"] == 200.0, p["expenses"])
    check("2n user_record expense -> income 0.00",
          p["income"] == 0.0, p["income"])
    check("2o user_record expense -> Rent spending 200.00",
          p["categories"] == {"Rent": 200.0}, p["categories"])
    check("2p user_record expense -> Feb 2026 net -200.00",
          p["month_rows"] == [("Feb 2026", -200.0)], p["month_rows"])

    # 5. internal transfer, type=income -> contributes to nothing
    p = probe_pdf((row(1, 500.0, "income", "Transfer", "internal"),))
    check("2q internal income row -> income 0.00",
          p["income"] == 0.0, p["income"])
    check("2r internal income row -> expenses 0.00",
          p["expenses"] == 0.0, p["expenses"])
    check("2s internal income row -> no spending category",
          p["categories"] == {}, p["categories"])
    check("2t internal income row -> no monthly series row",
          p["month_rows"] == [], p["month_rows"])

    # 6. internal transfer, type=expense -> contributes to nothing
    p = probe_pdf((row(1, 400.0, "expense", "Transfer", "internal"),))
    check("2u internal expense row -> income 0.00",
          p["income"] == 0.0, p["income"])
    check("2v internal expense row -> expenses 0.00",
          p["expenses"] == 0.0, p["expenses"])
    check("2w internal expense row -> no spending category",
          p["categories"] == {}, p["categories"])
    check("2x internal expense row -> no monthly series row",
          p["month_rows"] == [], p["month_rows"])

    # 7. conversion destination row -> contributes to nothing
    p = probe_pdf((row(1, 100.0, "expense", "Conversion", "conversion"),))
    check("2y conversion expense row -> income 0.00",
          p["income"] == 0.0, p["income"])
    check("2z conversion expense row -> expenses 0.00",
          p["expenses"] == 0.0, p["expenses"])
    check("2aa conversion expense row -> no spending category",
          p["categories"] == {}, p["categories"])
    check("2ab conversion expense row -> no monthly series row",
          p["month_rows"] == [], p["month_rows"])

    # 8. conversion source row -> contributes to nothing
    p = probe_pdf((row(1, 100.0, "income", "Conversion", "conversion"),))
    check("2ac conversion income row -> income 0.00",
          p["income"] == 0.0, p["income"])
    check("2ad conversion income row -> expenses 0.00",
          p["expenses"] == 0.0, p["expenses"])
    check("2ae conversion income row -> no spending category",
          p["categories"] == {}, p["categories"])
    check("2af conversion income row -> no monthly series row",
          p["month_rows"] == [], p["month_rows"])

    # 9. unknown history fails closed (approved K22 contract)
    p = probe_pdf((row(1, 999.0, "expense", "Legacy", "unknown"),))
    check("2ag unknown expense row -> income 0.00 (fail closed)",
          p["income"] == 0.0, p["income"])
    check("2ah unknown expense row -> expenses 0.00 (fail closed)",
          p["expenses"] == 0.0, p["expenses"])
    check("2ai unknown expense row -> no spending category (fail closed)",
          p["categories"] == {}, p["categories"])
    check("2aj unknown expense row -> no monthly series row (fail closed)",
          p["month_rows"] == [], p["month_rows"])

    # 10/11. blank categories on a genuine cashflow expense -> Other
    p = probe_pdf((row(1, 100.0, "expense", None, "external_out"),))
    check("2ak NULL category expense -> expenses 100.00",
          p["expenses"] == 100.0, p["expenses"])
    check("2al NULL category expense -> category 'Other' 100.00",
          p["categories"] == {"Other": 100.0}, p["categories"])

    p = probe_pdf((row(1, 100.0, "expense", "   ", "external_out"),))
    check("2am whitespace category expense -> expenses 100.00",
          p["expenses"] == 100.0, p["expenses"])
    check("2an whitespace category expense -> category 'Other' 100.00",
          p["categories"] == {"Other": 100.0}, p["categories"])


# ── 3. the whole reviewed ledger at once ─────────────────────────────────────

def section_mixed_ledger(mod, wd):
    banner("3. mixed ledger: the full approved contract in one PDF")
    page = fetch_pdf(mod)
    exp = contract_expectation(wd)

    check("3a income = external_in + user_record income (1,300.00)",
          page["income"] == CONTRACT_INCOME, page["income"])
    check("3b expenses = cashflow expenses only (380.00)",
          page["expenses"] == CONTRACT_EXPENSES, page["expenses"])
    check("3c balance = income - expenses (920.00), not wallet-derived",
          page["balance"] == CONTRACT_BALANCE
          and page["balance"] == page["income"] - page["expenses"]
          and page["balance"] != WALLET_SEED, page["balance"])
    check("3d categories: Food/Coffee/Other only, blanks normalized",
          page["categories"] == CONTRACT_CATEGORIES, page["categories"])
    leaked = {"Transfer", "Conversion", "Legacy"} & set(
        page["categories"] or {})
    check("3e internal/conversion/unknown labels never reach the table",
          not leaked, page["categories"])
    check("3f monthly nets equal the independent expectation",
          dict(page["month_rows"] or []) == exp["nets"]
          == CONTRACT_MONTH_NETS,
          (dict(page["month_rows"] or []), exp["nets"]))
    check("3g excluded-only months (Mar/Apr/Jul) are absent",
          not ({"Mar 2026", "Apr 2026", "Jul 2026"}
               & {m for m, _ in page["month_rows"] or []}),
          page["month_rows"])
    check("3h month row order unchanged (alphabetical known defect)",
          [m for m, _ in page["month_rows"] or []]
          == CONTRACT_MONTH_ORDER,
          [m for m, _ in page["month_rows"] or []])
    check("3i pre-migration bug totals (1,900.00 / 2,056.00) are gone",
          page["income"] != BUGGED_INCOME
          and page["expenses"] != BUGGED_EXPENSES,
          (page["income"], page["expenses"]))
    check("3j rendered values equal the independent SQL expectation",
          (page["income"], page["expenses"], page["balance"])
          == (exp["income"], exp["expenses"], exp["balance"]), exp)
    check("3k both category tables render (table + pie chart)",
          page["text"].count("Spending by Category") == 2
          and "Monthly Income vs Expenses" in page["text"],
          (page["text"].count("Spending by Category"),
           "Monthly Income vs Expenses" in page["text"]))
    return page


# ── 4. differential vs baseline "68e89f9" ───────────────────────────────────

def section_baseline(base_wd, page, page_e):
    banner("4. differential vs baseline " + BASELINE_REV)

    # Baseline route, same economic data (baseline predates movement_kind,
    # so rows are stored without the column): the defect pre-dates K21/K22.
    wd_b = make_wd("base_pdf")
    shutil.copyfile(os.path.join(base_wd, "app.py"),
                    os.path.join(wd_b, "app.py"))
    mod_b = load_app(wd_b, "base_pdf")
    seed_users(wd_b)
    seed_ledger(wd_b, with_kind=False)
    b_mixed = fetch_pdf(mod_b)

    wd_be = make_wd("base_pdf_empty")
    shutil.copyfile(os.path.join(base_wd, "app.py"),
                    os.path.join(wd_be, "app.py"))
    mod_be = load_app(wd_be, "base_pdf_empty")
    seed_users(wd_be)
    b_empty = fetch_pdf(mod_be)

    check("4a baseline PDF export fails on a populated ledger"
          " (defect pre-dates K21/K22)",
          b_mixed["status"] == 500, b_mixed["status"])
    check("4b baseline PDF export fails on an empty ledger too",
          b_empty["status"] == 500, b_empty["status"])
    check("4c the fix turns both baseline failures into valid PDFs",
          b_mixed["status"] != page["status"]
          and b_empty["status"] != page_e["status"]
          and page["status"] == page_e["status"] == 200,
          (b_mixed["status"], b_empty["status"],
           page["status"], page_e["status"]))


# ── 5. read-only safety ─────────────────────────────────────────────────────

def section_safety(wd, repo_sha_before):
    banner("5. read-only: wallets, ledger and the repo database")
    wallets = rows(wd, "SELECT user_id, currency, balance FROM wallets"
                       " ORDER BY currency")
    check("5a wallet rows untouched by PDF generation",
          wallets == [(1, "KES", WALLET_SEED), (1, "USD", 42.5)], wallets)
    ledger_rows = rows(wd, "SELECT id, amount, type, category"
                           " FROM transactions ORDER BY id")
    check("5b ledger rows untouched by PDF generation",
          ledger_rows
          == [(r[0], r[1], r[3], r[4]) for r in LEDGER],
          ledger_rows)
    check("5c repo database.db SHA-256 unchanged by this suite",
          repo_db_sha() == repo_sha_before,
          (repo_sha_before, repo_db_sha()))


# ── entry point ─────────────────────────────────────────────────────────────

def main():
    # the app prints emoji; a redirected cp1252 console would raise
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print("K22 Phase 3 PDF reporting-classification suite")
    print("repo:                 " + REPO)
    print("repo database.db is only SHA-256 hashed, never used as a DB")

    for name in ("DATABASE_URL", "OPENAI_API_KEY", "SENDGRID_API_KEY",
                 "FROM_EMAIL"):
        os.environ.pop(name, None)

    repo_sha_before = repo_db_sha()
    base_wd = baseline_wd("baseline")
    section_source_guards(base_wd)

    page, page_e = section_route_works()
    section_isolated_matrix()

    wd_mixed = make_wd("mixed")
    mod_mixed = load_app(wd_mixed, "mixed")
    seed_users(wd_mixed)
    seed_ledger(wd_mixed)
    page_mixed = section_mixed_ledger(mod_mixed, wd_mixed)

    section_baseline(base_wd, page_mixed, page_e)
    section_safety(wd_mixed, repo_sha_before)

    banner("summary")
    print("passed: %d   failed: %d" % (len(PASSED), len(FAILED)))
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print("  - " + name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())








