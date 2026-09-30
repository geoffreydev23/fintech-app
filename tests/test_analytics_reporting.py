"""K22 Phase 2 Analytics reporting-classification suite.

Proves that the Analytics surface classifies ledger movements through the
shared K22 reporting contract instead of ``type == "income"`` /
``else: expenses``:

  * internal transfers, currency conversions and unknown history add nothing to
    headline income and nothing to headline expenses
  * spending categories only ever collect cashflow expenses, with missing or
    blank categories normalized to ``Other``
  * the monthly series plots cashflow income and cashflow expenses only; it
    never defaults "everything else" into the expense series
  * the existing Analytics balance semantics (and the wallet data behind the
    wallet surfaces) are preserved

Safety: every scenario runs against a *copy* of ``app.py`` inside an isolated
temp workdir. The app resolves SQLite through ``__file__``, so the copy
creates and migrates its own throwaway ``database.db``. The repository
database is never opened, never migrated and never modified.

Run:  python tests/test_analytics_reporting.py
"""

import ast
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "app.py")
TEMPLATES_DIR = os.path.join(REPO, "templates")
BASELINE_REV = "68e89f9"

# The economic ledger this suite drives, written straight to the table so the
# classification helpers (not a writer) are what is under test:
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
    (7, 100.0, "KES", "expense", "Conversion", "Wallet", "converted to USD",
     "2026-04-11 10:00:00", "conversion"),
    (8, 100.0, "KES", "income", "Conversion", "Wallet", "converted from USD",
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

# Contract expectations for LEDGER (cashflow income / cashflow expenses only).
CONTRACT_INCOME = 1300.0            # 1000 external_in + 300 user_record income
CONTRACT_EXPENSES = 380.0           # 250 + 40 + 60 + 30 cashflow expenses
CONTRACT_BALANCE = 920.0
CONTRACT_CATEGORIES = {"Food": 250.0, "Coffee": 40.0, "Other": 90.0}
CONTRACT_MONTHS = ["2026-01", "2026-02", "2026-05", "2026-06"]
CONTRACT_MONTHLY_INCOME = [1000.0, 300.0, 0, 0]
CONTRACT_MONTHLY_EXPENSES = [250.0, 40.0, 60.0, 30.0]

# What the pre-migration ``else: expenses += amount`` produced over LEDGER.
BUGGED_INCOME = 1900.0              # every ``type == 'income'`` row
BUGGED_EXPENSES = 2056.0            # every other row
EXCLUDED_AMOUNTS = 1676.0           # internal + conversion + unknown

# Functions this phase is explicitly NOT allowed to touch (reporting surfaces
# other than the declared K22 migrations, plus the wallet and ledger readers
# they share). K22 Phase 3 later migrated the PDF export and K22 Phase 5 the
# Dashboard; those surfaces are declared in K22_MIGRATED_SURFACES below instead
# of being dropped from the guard.
UNTOUCHED_BY_THIS_PHASE = (
    "export_analytics",
    "transactions", "export_transactions", "wallet", "archive",
    "get_wallet_balance", "update_wallet_balance", "convert_currency",
)

# Exactly which surfaces are allowed to carry the K22 helpers. Phase 2
# migrated Analytics, Phase 3 the PDF export, Phase 4 the Chat summary and
# Phase 5 the Dashboard. 0a2 fails the moment any *undeclared* surface picks up
# a helper, or a declared one loses it. The Dashboard migration is pinned in
# tests/test_dashboard_reporting.py.
K22_MIGRATED_SURFACES = ("analytics", "export_analytics_pdf", "chat",
                         "dashboard")

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
    wd = tempfile.mkdtemp(prefix="k22an_" + tag + "_")
    shutil.copyfile(SRC, os.path.join(wd, "app.py"))
    shutil.copytree(TEMPLATES_DIR, os.path.join(wd, "templates"))
    return wd


def baseline_wd(tag):
    """Temp workdir holding the pre-K21/pre-K22 app.py from BASELINE_REV."""
    wd = tempfile.mkdtemp(prefix="k22an_" + tag + "_")
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
    name = "k22an_app_" + tag
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
    "INSERT INTO transactions (id, user_id, amount, currency, type, category,"
    " source, description, created_at, movement_kind)"
    " VALUES (?,?,?,?,?,?,?,?,?,?)")
LEDGER_SQL_PLAIN = (
    "INSERT INTO transactions (id, user_id, amount, currency, type, category,"
    " source, description, created_at) VALUES (?,?,?,?,?,?,?,?,?)")


def seed_users(wd):
    conn = raw(wd)
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO users (id, username, password, email,"
                    " preferred_currency) VALUES (?,?,?,?,?)",
                    (1, "alice", "x", "alice@example.com", "KES"))
        # wallet balances deliberately unrelated to the ledger, so a
        # wallet-derived balance would be trivially distinguishable
        cur.execute("INSERT INTO wallets (user_id, currency, balance)"
                    " VALUES (?,?,?)", (1, "KES", 123456.78))
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


def seed_one(wd, amount, ttype, category, created_at, kind):
    seed_ledger(wd, ((1, amount, "KES", ttype, category, "Card", "probe",
                      created_at, kind),))


# ── reading the rendered Analytics page ─────────────────────────────────────

def get_page(mod, path="/analytics", user_id=1):
    client = mod.app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["username"] = "alice"
        sess["_csrf_token"] = "k22token"
    resp = client.get(path)
    return resp.status_code, resp.get_data(as_text=True)


def money(body, label):
    """Read a KPI card: <p ...>Label</p> <h2 ...>KES 1,234.56</h2>."""
    m = re.search(re.escape(label)
                  + r"</p>\s*<h2[^>]*>\s*KES (-?[\d,]+\.\d{2})", body)
    return None if m is None else float(m.group(1).replace(",", ""))


def score_of(body):
    m = re.search(r'<h2 style="color:#f59e0b;">\s*(\d+)/100', body)
    return None if m is None else int(m.group(1))


def progress_of(body):
    """Read the savings-goal bar only (the page chrome has its own widths)."""
    tail = body.split("Savings Goal", 1)
    if len(tail) < 2:
        return None
    m = re.search(r"width:([\d.]+)%;", tail[1])
    return None if m is None else float(m.group(1))


def js_literal(body, name):
    """Read a ``const <name> = <json>;`` blob out of the chart script."""
    m = re.search(r"const " + name + r" = (\[.*?\]|\{.*?\});", body, re.S)
    return None if m is None else json.loads(m.group(1))


def read_analytics(mod, path="/analytics"):
    status, body = get_page(mod, path)
    return {
        "status": status,
        "body": body,
        "income": money(body, "Income"),
        "expenses": money(body, "Expenses"),
        "balance": money(body, "Balance"),
        "score": score_of(body),
        "progress": progress_of(body),
        "categories": js_literal(body, "categoryData"),
        "months": js_literal(body, "months"),
        "monthly_income": js_literal(body, "monthlyIncome"),
        "monthly_expenses": js_literal(body, "monthlyExpenses"),
    }


# ── independent expectation layer (contract text + SQL, never app.py) ───────

def contract_expectation(wd):
    """Recompute the approved contract straight from the stored rows."""
    recs = rows(wd, "SELECT amount, type, category, movement_kind, created_at"
                    " FROM transactions WHERE user_id=1 ORDER BY id")
    income = 0.0
    expenses = 0.0
    categories = {}
    monthly_income = {}
    monthly_expenses = {}
    for amount, ttype, category, kind, created_at in recs:
        amount = float(amount)
        month = str(created_at)[:7]
        cashflow_in = kind == "external_in" or (
            kind == "user_record" and ttype == "income")
        cashflow_out = kind == "external_out" or (
            kind == "user_record" and ttype == "expense")
        if cashflow_in:
            income += amount
            monthly_income[month] = monthly_income.get(month, 0.0) + amount
        elif cashflow_out:
            expenses += amount
            monthly_expenses[month] = monthly_expenses.get(month, 0.0) + amount
            label = "" if category is None else str(category).strip()
            label = label or "Other"
            categories[label] = categories.get(label, 0.0) + amount
        # internal / conversion / unknown contribute to nothing
    months = sorted(set(list(monthly_income) + list(monthly_expenses)))
    return {
        "income": income,
        "expenses": expenses,
        "balance": income - expenses,
        "categories": categories,
        "months": months,
        "monthly_income": [monthly_income.get(m, 0) for m in months],
        "monthly_expenses": [monthly_expenses.get(m, 0) for m in months],
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


def unguarded_accumulations(path):
    """Every income/expense accumulation must sit inside a K22 cashflow guard.

    This is what structurally prevents ``else: expenses += amount`` from ever
    creeping back into Analytics: an unguarded accumulator is reported here.
    """
    helper = {"income": "is_cashflow_income",
              "expenses": "is_cashflow_expense"}
    bad = []
    fn = [n for n in ast.walk(ast.parse(read_text(path)))
          if isinstance(n, ast.FunctionDef) and n.name == "analytics"][0]
    for word, guard_name in sorted(helper.items()):
        guards = [(n.lineno, n.end_lineno) for n in ast.walk(fn)
                  if isinstance(n, ast.If)
                  and guard_name in ast.unparse(n.test)]
        for node in ast.walk(fn):
            if not isinstance(node, ast.AugAssign):
                continue
            if word not in ast.unparse(node.target):
                continue
            if not any(a <= node.lineno and node.end_lineno <= b
                       for a, b in guards):
                bad.append("line %d: %s" % (node.lineno, ast.unparse(node)))
    return bad


# The K21 phase legitimately added movement_kind assignment to the dashboard
# ledger writer. K22 Phase 5 migrates the Dashboard reporting surface itself, so
# `dashboard` now lives in K22_MIGRATED_SURFACES above (its migrated contract is
# pinned in tests/test_dashboard_reporting.py) and 0a compares it element-wise
# rather than with the K21 artifacts reduced. The reduction is kept for any
# surface that is still only a K21 writer.
K21_WRITERS = ("dashboard",)

KIND_LINE = re.compile(
    r'^\s*"(external_in|external_out|internal|conversion|user_record'
    r'|unknown)",?$')
PLACEHOLDER_LINE = re.compile(
    r"^\s*(?:VALUES\s*)?\((?:[%s?]+,\s*)*[%s?]+\)\s*,?$")
COLUMN_LINE = re.compile(
    r"^\s*\((?:[A-Za-z_]\w*\s*,\s*)*[A-Za-z_]\w*\s*\)\s*,?$")
SELECT_LINE = re.compile(
    r"^\s*SELECT\s+[A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*\s*$")
NOISE_LINE = re.compile(r"^\s*(not in \(|\):)\s*$")


def strip_k21_artifacts(text):
    kept = []
    for line in text.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        if "movement_kind" in line:
            continue
        if (KIND_LINE.match(line) or PLACEHOLDER_LINE.match(line)
                or COLUMN_LINE.match(line) or SELECT_LINE.match(line)
                or NOISE_LINE.match(line)):
            continue
        kept.append(line.rstrip().rstrip(","))
    return "\n".join(kept)


def section_surface_guards(base_wd):
    banner("0. surface guards (only declared K22 surfaces ever change)")
    srcmap = func_sources(SRC)
    base_src = func_sources(os.path.join(base_wd, "app.py"))

    moved = []
    for name in UNTOUCHED_BY_THIS_PHASE:
        current = srcmap.get(name)
        if name in K21_WRITERS:
            same = (strip_k21_artifacts(base_src.get(name) or "")
                    == strip_k21_artifacts(current or ""))
        else:
            same = base_src.get(name) == current
        if not same:
            moved.append(name)
    check("0a every other reporting / wallet surface is unchanged"
          " (K21 artifacts reduced where K21 wrote them)", not moved, moved)

    migrated = sorted(
        n for n in set(UNTOUCHED_BY_THIS_PHASE) | set(K22_MIGRATED_SURFACES)
        if "is_cashflow_income" in srcmap.get(n, "")
        or "is_cashflow_expense" in srcmap.get(n, "")
        or "get_transaction_category" in srcmap.get(n, ""))
    check("0a2 exactly the declared K22 surfaces are migrated",
          migrated == sorted(K22_MIGRATED_SURFACES), migrated)

    analytics_src = srcmap["analytics"]
    check("0b Analytics no longer branches on the raw type field",
          "['type'] ==" not in analytics_src
          and '["type"] ==' not in analytics_src)
    check("0c Analytics classifies through the shared K22 helpers",
          all(h in analytics_src for h in ("is_cashflow_income",
                                           "is_cashflow_expense",
                                           "get_transaction_category")))
    bad = unguarded_accumulations(SRC)
    check("0d every income/expense accumulation sits inside a K22"
          " cashflow guard", not bad, bad)


# ── 1. one reviewed movement at a time ──────────────────────────────────────

_PROBE_N = [0]


def probe(kind, ttype, category="Probe", amount=100.0,
          created_at="2026-01-15 09:00:00"):
    """Run /analytics over a ledger holding exactly one reviewed movement."""
    _PROBE_N[0] += 1
    tag = "%s_%s_%d" % (kind or "none", ttype or "none", _PROBE_N[0])
    wd = make_wd(tag)
    mod = load_app(wd, tag)
    seed_users(wd)
    seed_one(wd, amount, ttype, category, created_at, kind)
    page = read_analytics(mod)
    page["wd"] = wd
    return page


def section_isolated_matrix():
    banner("1. single-movement matrix (one reviewed row per scenario)")
    seen = {}

    # 1. external income -> income only
    p = seen["external_in income"] = probe("external_in", "income")
    check("1a external_in income -> income += 100.00",
          p["income"] == 100.0, p["income"])
    check("1b external_in income -> expenses unchanged",
          p["expenses"] == 0.0, p["expenses"])
    check("1c external_in income -> no spending category",
          p["categories"] == {}, p["categories"])

    # 2. external expense -> expenses only, category recorded
    p = seen["external_out expense"] = probe("external_out", "expense")
    check("1d external_out expense -> expenses += 100.00",
          p["expenses"] == 100.0, p["expenses"])
    check("1e external_out expense -> income unchanged",
          p["income"] == 0.0, p["income"])
    check("1f external_out expense -> category 'Probe' appears in spending",
          p["categories"] == {"Probe": 100.0}, p["categories"])

    # 3. user-record income -> income only
    p = seen["user_record income"] = probe("user_record", "income")
    check("1g user_record income -> income += 100.00",
          p["income"] == 100.0, p["income"])
    check("1h user_record income -> expenses unchanged",
          p["expenses"] == 0.0, p["expenses"])

    # 4. user-record expense -> expenses only, category recorded
    p = seen["user_record expense"] = probe("user_record", "expense")
    check("1i user_record expense -> expenses += 100.00",
          p["expenses"] == 100.0, p["expenses"])
    check("1j user_record expense -> category 'Probe' appears in spending",
          p["categories"] == {"Probe": 100.0}, p["categories"])

    # 5. internal transfer -> neither side, whatever the type says
    p = seen["internal income"] = probe("internal", "income")
    check("1k internal income row -> income unchanged",
          p["income"] == 0.0, p["income"])
    check("1l internal income row -> expenses unchanged",
          p["expenses"] == 0.0, p["expenses"])
    check("1m internal income row -> no spending category",
          p["categories"] == {}, p["categories"])

    p = seen["internal expense"] = probe("internal", "expense")
    check("1n internal expense row -> expenses unchanged",
          p["expenses"] == 0.0, p["expenses"])
    check("1o internal expense row -> income unchanged",
          p["income"] == 0.0, p["income"])
    check("1p internal expense row -> no spending category",
          p["categories"] == {}, p["categories"])

    return seen


# ── 1b. excluded movements and category normalization ───────────────────────

def section_excluded_and_normalized(seen):
    banner("1b. excluded movements and category normalization")

    p = seen["conversion expense"] = probe("conversion", "expense")
    check("1q conversion expense -> expenses unchanged",
          p["expenses"] == 0.0, p["expenses"])
    check("1r conversion expense -> income unchanged",
          p["income"] == 0.0, p["income"])
    check("1s conversion expense -> no spending category",
          p["categories"] == {}, p["categories"])

    p = seen["conversion income"] = probe("conversion", "income")
    check("1t conversion income -> income unchanged",
          p["income"] == 0.0, p["income"])
    check("1u conversion income -> expenses unchanged",
          p["expenses"] == 0.0, p["expenses"])

    p = seen["unknown expense"] = probe("unknown", "expense")
    check("1v unknown expense -> expenses unchanged (fail closed)",
          p["expenses"] == 0.0, p["expenses"])
    check("1w unknown expense -> income unchanged (fail closed)",
          p["income"] == 0.0, p["income"])
    check("1x unknown expense -> no spending category (fail closed)",
          p["categories"] == {}, p["categories"])

    p = probe("external_out", "expense", category=None)
    check("1y cashflow expense with NULL category -> 'Other': 100.00",
          p["categories"] == {"Other": 100.0} and p["expenses"] == 100.0,
          (p["categories"], p["expenses"]))

    p = probe("external_out", "expense", category="   ")
    check("1z cashflow expense with whitespace category -> 'Other': 100.00",
          p["categories"] == {"Other": 100.0} and p["expenses"] == 100.0,
          (p["categories"], p["expenses"]))


# ── 2. the whole reviewed ledger at once ────────────────────────────────────

def section_totals_and_categories(page, wd):
    banner("2. full ledger: headline totals and spending categories")
    exp = contract_expectation(wd)

    check("2a income = external_in + user_record income (1,300.00)",
          page["income"] == CONTRACT_INCOME, page["income"])
    check("2b expenses = cashflow expenses only (380.00)",
          page["expenses"] == CONTRACT_EXPENSES, page["expenses"])
    check("2c the pre-migration bug total (2,056.00) is gone",
          page["expenses"] != BUGGED_EXPENSES, page["expenses"])
    check("2d categories hold spending only, blank normalized and trimmed",
          page["categories"] == CONTRACT_CATEGORIES, page["categories"])
    leaked = ({"Transfer", "Conversion", "Legacy"}
              & set(page["categories"] or {}))
    check("2e internal/conversion/unknown labels never reach categories",
          not leaked, page["categories"])
    check("2f rendered values equal the independent SQL expectation",
          (page["income"], page["expenses"], page["balance"])
          == (exp["income"], exp["expenses"], exp["balance"]), exp)
    check("2g score and grade derive from the migrated totals",
          page["score"] == 100 and "Grade: A+" in page["body"], page["score"])


# ── 3. monthly series ───────────────────────────────────────────────────────

def section_monthly(page, seen):
    banner("3. monthly series classification")

    check("3a months list internal-only / conversion-only months are absent",
          page["months"] == CONTRACT_MONTHS, page["months"])
    check("3b monthly income series unchanged for cashflow income",
          page["monthly_income"] == CONTRACT_MONTHLY_INCOME,
          page["monthly_income"])
    check("3c monthly expense series holds cashflow expenses only",
          page["monthly_expenses"] == CONTRACT_MONTHLY_EXPENSES,
          page["monthly_expenses"])
    check("3d internal-only month 2026-03 is not plotted",
          "2026-03" not in (page["months"] or []))
    check("3e conversion-only month 2026-04 is not plotted",
          "2026-04" not in (page["months"] or []))
    check("3f internal-only month 2026-07 is not plotted",
          "2026-07" not in (page["months"] or []))
    check("3g every plotted month carries at least one cashflow leg",
          all(
              (page["monthly_income"] or [0])[i]
              or (page["monthly_expenses"] or [0])[i]
              for i in range(len(page["months"] or []))
          ))

    for key, label in (
        ("internal expense",
         "3h internal-only ledger plots no monthly series"),
        ("conversion expense", "3i conversion-only ledger plots no series"),
        ("unknown expense", "3j unknown-only ledger plots no series"),
    ):
        p = seen[key]
        check(label,
              p["months"] == [] and p["monthly_income"] == []
              and p["monthly_expenses"] == [],
              (p["months"], p["monthly_income"], p["monthly_expenses"]))

    p = probe("external_out", "expense", created_at=None)
    check("3k legacy NULL created_at still groups as before (no date drift)",
          p["months"] == ["None"] and p["monthly_expenses"] == [100.0],
          (p["months"], p["monthly_expenses"]))


# ── 4. balance semantics preserved ──────────────────────────────────────────

WALLET_SQL = ("SELECT user_id, currency, balance FROM wallets"
              " ORDER BY user_id, currency")
ECON_SQL = ("SELECT id, user_id, amount, currency, type, category, source,"
            " description, created_at FROM transactions ORDER BY id")


def section_balance(page, wd):
    banner("4. balance semantics and read-only behaviour")

    check("4a Balance KPI is still income - expenses (920.00)",
          page["balance"] == CONTRACT_BALANCE, page["balance"])
    check("4b no wallet-derived balance introduced (wallets hold 123,456.78)",
          page["balance"] != 123456.78, page["balance"])
    check("4c savings goal still derives from the same ledger balance",
          page["progress"] == 1.8, page["progress"])

    wallets = rows(wd, WALLET_SQL)
    check("4d /analytics is read-only: wallet rows untouched",
          wallets == [(1, "KES", 123456.78), (1, "USD", 42.5)], wallets)
    ledger = rows(wd, "SELECT id, amount, type, category, movement_kind"
                      " FROM transactions ORDER BY id")
    check("4e /analytics is read-only: ledger rows untouched",
          len(ledger) == len(LEDGER), ledger)


# ── 5. differential against the pre-migration app ───────────────────────────

EXCLUDED_INCOME_AMOUNTS = 600.0     # internal 500 + conversion 100 income legs


def section_baseline(base_wd):
    banner("5. differential vs baseline " + BASELINE_REV
           + " (same economic ledger)")

    wd_new = make_wd("diff_new")
    mod_new = load_app(wd_new, "diff_new")
    seed_users(wd_new)
    seed_ledger(wd_new)
    new = read_analytics(mod_new)

    # 68e89f9 predates movement_kind, so its copy stores no kind at all and
    # classifies purely by the raw type field - exactly the buggy shape.
    wd_old = make_wd("diff_old")
    shutil.copyfile(os.path.join(base_wd, "app.py"),
                    os.path.join(wd_old, "app.py"))
    mod_old = load_app(wd_old, "diff_old")
    seed_users(wd_old)
    seed_ledger(wd_old, with_kind=False)
    old = read_analytics(mod_old)

    check("5a baseline counts every non-income row as expense (2,056.00)",
          old["expenses"] == BUGGED_EXPENSES, old["expenses"])
    check("5b baseline counts every type=='income' row as income (1,900.00)",
          old["income"] == BUGGED_INCOME, old["income"])
    check("5c migration drops exactly the internal/conversion/unknown legs",
          old["expenses"] - new["expenses"] == EXCLUDED_AMOUNTS
          and old["income"] - new["income"] == EXCLUDED_INCOME_AMOUNTS,
          (old["expenses"] - new["expenses"],
           old["income"] - new["income"]))
    check("5d balance expression preserved in both runs (income - expenses)",
          old["balance"] == old["income"] - old["expenses"]
          and new["balance"] == new["income"] - new["expenses"],
          (old["balance"], new["balance"]))
    check("5e baseline also miscounted excluded rows into spending categories",
          {"Transfer", "Conversion"} <= set(old["categories"] or {})
          and not ({"Transfer", "Conversion"}
                   & set(new["categories"] or {})),
          (old["categories"], new["categories"]))
    check("5f wallet rows identical between baseline and migrated runs",
          rows(wd_old, WALLET_SQL) == rows(wd_new, WALLET_SQL),
          (rows(wd_old, WALLET_SQL), rows(wd_new, WALLET_SQL)))
    check("5g economic ledger rows identical between both runs",
          rows(wd_old, ECON_SQL) == rows(wd_new, ECON_SQL))


# ── entry point ─────────────────────────────────────────────────────────────

def main():
    # the app prints emoji; a redirected cp1252 console would raise
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print("K22 Phase 2 analytics reporting-classification suite")
    print("repo:                 " + REPO)
    print("repo database.db is never opened by this suite")

    for name in ("DATABASE_URL", "OPENAI_API_KEY", "SENDGRID_API_KEY",
                 "FROM_EMAIL"):
        os.environ.pop(name, None)

    base_wd = baseline_wd("baseline")
    section_surface_guards(base_wd)

    seen = section_isolated_matrix()
    section_excluded_and_normalized(seen)

    wd_mixed = make_wd("mixed")
    mod_mixed = load_app(wd_mixed, "mixed")
    seed_users(wd_mixed)
    seed_ledger(wd_mixed)
    page = read_analytics(mod_mixed)

    section_totals_and_categories(page, wd_mixed)
    section_monthly(page, seen)
    section_balance(page, wd_mixed)
    section_baseline(base_wd)

    banner("summary")
    print("passed: %d   failed: %d" % (len(PASSED), len(FAILED)))
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print("  - " + name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
