"""K22 Phase 5 Dashboard reporting-classification suite.

Proves the ``/dashboard`` reporting surface classifies ledger movements
through the shared K22 reporting contract instead of the old
``type == "income"`` / ``else: expenses`` shape:

  * internal transfers, currency conversions and unknown history add
    nothing to Total Income, nothing to Total Expenses, nothing to the
    spending-category doughnut and nothing to the AI Budgeting bars
  * spending categories only collect cashflow expenses, with missing or
    blank categories normalized to ``Other`` by the shared helper
  * Total Balance stays wallet-derived (``real_balance``) and is never
    recomputed as ``income - expenses`` - the migration must not silently
    change what the balance card means
  * the aggregation always covers the full filtered history, never just
    the paginated page, and pagination / type filter / search behaviour
    is unchanged
  * type filter and search still narrow *which rows are aggregated*, so
    an internal/conversion/unknown row that a filter selects stays
    excluded from the figures
  * ``generate_insights`` / ``generate_budget`` /
    ``calculate_financial_score`` / ``generate_savings_goal`` stay
    byte-identical: they consume caller-provided figures and classify no
    rows themselves
  * the migration is a pure reporting change: wallets, ledger rows and
    notification rows are untouched, and the repository database is never
    opened

Verification: every expected value is recomputed locally from the stored
rows by ``contract_expectation()`` (approved contract text + SQL only) or
stated literally - never by calling the code under test.

Safety: every scenario runs against a *copy* of ``app.py`` in an isolated
temp workdir with its own throwaway ``database.db``. The repository
database is never used as a database (only SHA-256 hashed).

Run:  python tests/test_dashboard_reporting.py
"""

import ast
import hashlib
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
# shared classification helpers (not a writer) are what is under test:
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

# Approved-contract expectations for LEDGER, stated literally and also
# recomputed independently by contract_expectation().
CONTRACT_INCOME = 1300.0            # 1000 external_in + 300 user_record income
CONTRACT_EXPENSES = 380.0           # 250 + 40 + 60 + 30 cashflow expenses
CONTRACT_CATEGORIES = {"Food": 250.0, "Coffee": 40.0, "Other": 90.0}
# income - expenses, the figure the dashboard must NOT use as its balance.
CONTRACT_LEDGER_NET = 920.0

# What the pre-migration ``type == "income"`` / ``else: expenses`` shape
# produced over LEDGER (documenting that the migration is observable).
BUGGED_INCOME = 1900.0
BUGGED_EXPENSES = 2056.0
EXCLUDED_INCOME = 600.0             # internal 500 + conversion 100
EXCLUDED_EXPENSES = 1676.0          # internal 577 + conv 100 + unknown 999

# LEDGER minus the row without a category. The pre-K21 dashboard cannot even
# render the doughnut for a missing category: it keys ``category_data`` by the
# raw value, and Jinja's ``tojson`` sorts keys, so ``None < "Salary"`` raises
# TypeError. K22's normalization to "Other" is what makes such history
# renderable at all (see check 8h).
BASELINE_LEDGER = tuple(r for r in LEDGER if r[4] is not None)
BASELINE_INCOME = 1300.0            # cashflow income is unaffected
BASELINE_EXPENSES = 320.0           # 380 minus the 60 unlabelled expense
BASELINE_CATEGORIES = {"Food": 250.0, "Coffee": 40.0, "Other": 30.0}
BUGGED_BASELINE_EXPENSES = 1996.0   # 2056 minus the same 60

# Wallet seed deliberately unrelated to the ledger, so a wallet-derived
# balance is trivially distinguishable from income - expenses.
WALLET_KES = 123456.78
WALLET_USD = 42.5
WALLET_SQL = ("SELECT user_id, currency, balance FROM wallets"
              " ORDER BY user_id, currency")
LEDGER_ECON_SQL = ("SELECT id, user_id, amount, currency, type, category,"
                   " source, description, created_at FROM transactions"
                   " ORDER BY id")

# Dashboard page size (hardcoded in app.py).
PAGE_SIZE = 10

# SHA-256 (first 16 hex) of app.py functions recorded at the START of
# Phase 5. The Dashboard surface and the two shared row extractors are the
# functions this phase is allowed to move: `dashboard` keeps the pre-K22 value
# the Phase 3 and Phase 4 suites handed over, and the extractors keep the
# values they had since Phase 1/2. Everything else listed must survive Phase 5
# untouched.
PHASE5_START_SHA256 = {
    "dashboard": "67a463de511b2d3d",
    "analytics": "1d79632a1d377ea4",
    "export_analytics_pdf": "ffa87acb05a77093",
    "chat": "0da23015ba126b86",
    "is_cashflow_income": "642556ad3c707a94",
    "is_cashflow_expense": "9500c143d585350b",
    "is_cashflow_excluded": "566449f49d9bf02b",
    "get_transaction_category": "8b91c0a1c24c9937",
    "_extract_movement_kind_and_type": "351c74f8291bedfa",
    "_extract_category": "1a588f24940421e5",
}

MOVED_BY_THIS_PHASE = ("dashboard", "_extract_movement_kind_and_type",
                       "_extract_category")

# The migrated Dashboard and the extended extractors, frozen from here on.
PHASE5_END_SHA256 = {
    "dashboard": "e333e86ca9b00d22",
    "_extract_movement_kind_and_type": "6431ab7a8df91ab7",
    "_extract_category": "9750f41545dd7634",
}

# The two derived card figures the migration must not disturb: score 90
# ("Excellent") and a savings goal of 920 / 260 capped at 100% (both are pure
# functions of the migrated income and expense totals).
DASHBOARD_SCORE = 90
DASHBOARD_STATUS = "Excellent"
GOAL_SAVED = 920.0
GOAL_TARGET = 260.0
GOAL_PROGRESS = 100.0

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

_WORKDIRS = []


def _register(wd):
    _WORKDIRS.append(wd)
    return wd


def make_wd(tag):
    wd = tempfile.mkdtemp(prefix="k22dash_" + tag + "_")
    shutil.copyfile(SRC, os.path.join(wd, "app.py"))
    shutil.copytree(TEMPLATES_DIR, os.path.join(wd, "templates"))
    return _register(wd)


def baseline_wd(tag):
    """Temp workdir holding the pre-K21/pre-K22 app.py from BASELINE_REV."""
    wd = tempfile.mkdtemp(prefix="k22dash_" + tag + "_")
    blob = subprocess.run(
        ["git", "show", BASELINE_REV + ":app.py"],
        cwd=REPO, capture_output=True, check=True,
    ).stdout
    with open(os.path.join(wd, "app.py"), "wb") as fh:
        fh.write(blob)
    shutil.copytree(TEMPLATES_DIR, os.path.join(wd, "templates"))
    return _register(wd)


_LOADED = []
_IMPORTED = []


def load_app(wd, tag):
    """Import the app copy (init_db() runs on import). Never the repo copy."""
    for name in list(_LOADED):
        sys.modules.pop(name, None)
    _LOADED[:] = []
    name = "k22dash_app_" + tag
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(wd, "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    _LOADED.append(name)
    _IMPORTED.append(wd)
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
                    " VALUES (?,?,?)", (1, "KES", WALLET_KES))
        cur.execute("INSERT INTO wallets (user_id, currency, balance)"
                    " VALUES (?,?,?)", (1, "USD", WALLET_USD))
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


# ── GET /dashboard and reading the rendered figures ─────────────────────────

MONEY = r"-?[\d,]+\.\d{2}"
DESC_RE = re.compile(r'class="transaction-desc">\s*([^<]*?)\s*</div>')
BAR_RE = re.compile(r"<b>([^<]+)</b>:\s*Spent Ksh ([\d.]+)\s*/"
                    r"\s*Recommended Ksh ([\d.]+)")
CHART_RE = re.compile(r"const data = ([^\n]*);")
PAGE_RE = re.compile(r'<a class="active-page">\s*Page (\d+)')
SCORE_RE = re.compile(r'<div class="score-circle">\s*<span>\s*(\d+)\s*</span>')
SCORE_CARD_RE = re.compile(r'(\d+)/100\s*</div>\s*<p>([^<]*)</p>')
GOAL_RE = re.compile(r"Saved: Ksh (-?[\d.]+)")
TARGET_RE = re.compile(r"Target: Ksh (-?[\d.]+)")
PROGRESS_RE = re.compile(r"([\d.]+)% complete")


def dashboard_get(mod, query="", with_user=True):
    client = mod.app.test_client()
    with client.session_transaction() as sess:
        sess["_csrf_token"] = "k22dtoken"
        if with_user:
            sess["user_id"] = 1
            sess["username"] = "alice"
    resp = client.get("/dashboard" + query)
    return resp.status_code, resp.get_data(as_text=True)


def kpi(body, label):
    """KES figure of the hero card labelled ``Total Balance/Income/Expenses``."""
    m = re.search(re.escape(label) + r"\s*</span>\s*<[^>]+>\s*KES ("
                  + MONEY + r")", body)
    return None if m is None else float(m.group(1).replace(",", ""))


def read_dashboard(mod, query=""):
    """GET /dashboard and lift every reported figure out of the HTML."""
    status, body = dashboard_get(mod, query)
    chart = CHART_RE.search(body)
    score_card = SCORE_CARD_RE.search(body)
    goal_saved = GOAL_RE.search(body)
    goal_target = TARGET_RE.search(body)
    progress = PROGRESS_RE.search(body)
    page_no = PAGE_RE.search(body)
    score = SCORE_RE.search(body)
    return {
        "status": status,
        "body": body,
        "income": kpi(body, "Total Income"),
        "expenses": kpi(body, "Total Expenses"),
        "balance": kpi(body, "Total Balance"),
        "categories": None if chart is None else json.loads(chart.group(1)),
        "descriptions": DESC_RE.findall(body),
        "page": None if page_no is None else int(page_no.group(1)),
        "has_next": "Next →" in body,
        "has_prev": "← Previous" in body,
        "bars": {name: {"spent": float(spent), "recommended": float(rec)}
                 for name, spent, rec in BAR_RE.findall(body)},
        "score": None if score is None else int(score.group(1)),
        "score_card": None if score_card is None
        else (int(score_card.group(1)), score_card.group(2).strip()),
        "goal_saved": None if goal_saved is None
        else float(goal_saved.group(1)),
        "goal_target": None if goal_target is None
        else float(goal_target.group(1)),
        "goal_progress": None if progress is None
        else float(progress.group(1)),
    }


# ── independent expectation layer (contract rules, never app.py) ────────────

def contract_expectation(wd):
    """Recompute the approved contract straight from the stored rows."""
    recs = rows(wd, "SELECT amount, type, category, movement_kind"
                    " FROM transactions WHERE user_id=1 ORDER BY id")
    income = 0.0
    expenses = 0.0
    categories = {}
    for amount, ttype, category, kind in recs:
        amount = float(amount)
        cashflow_in = kind == "external_in" or (
            kind == "user_record" and ttype == "income")
        cashflow_out = kind == "external_out" or (
            kind == "user_record" and ttype == "expense")
        if cashflow_in:
            income += amount
        elif cashflow_out:
            expenses += amount
            name = "" if category is None else str(category).strip()
            name = name or "Other"
            categories[name] = categories.get(name, 0.0) + amount
        # internal / conversion / unknown contribute to nothing
    top = max(categories, key=categories.get) if categories else None
    return {
        "income": income,
        "expenses": expenses,
        "net": income - expenses,
        "categories": categories,
        "top": top,
        "top_amount": categories[top] if top else None,
    }


def expected_wallet_balance(mod, wd):
    """Wallet-derived Total Balance, per the pre-existing wallet code."""
    total = 0.0
    for _uid, currency, balance in rows(wd, WALLET_SQL):
        total += mod.convert_currency(float(balance), currency, "KES")
    return total


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


def dashboard_audit():
    """Audit how the Dashboard turns rows into income/expenses/categories.

    Returns the assignments/aug-assignments feeding ``income`` and
    ``expenses``, every ``category_data[...]`` accumulation, the
    ``if not is_cashflow_expense(...): continue`` skip guards and the source
    of the loop that walks the aggregation rows.
    """
    fn = [n for n in ast.walk(ast.parse(read_text(SRC)))
          if isinstance(n, ast.FunctionDef) and n.name == "dashboard"][0]
    sums = {}
    cats = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                name = ast.unparse(tgt)
                if name in ("income", "expenses"):
                    sums.setdefault(name, []).append(ast.unparse(node))
                elif name.startswith("category_data["):
                    cats.append((node.lineno, ast.unparse(node)))
        elif isinstance(node, ast.AugAssign):
            name = ast.unparse(node.target)
            if name in ("income", "expenses"):
                sums.setdefault(name, []).append(ast.unparse(node))
            elif name.startswith("category_data["):
                cats.append((node.lineno, ast.unparse(node)))
    skips = [(n.lineno, n.end_lineno) for n in ast.walk(fn)
             if isinstance(n, ast.If)
             and "is_cashflow_expense" in ast.unparse(n.test)
             and any(isinstance(b, ast.Continue) for b in n.body)]
    loops = [n for n in ast.walk(fn)
             if isinstance(n, ast.For) and ast.unparse(n.iter) ==
             "aggregation_rows"]
    return {
        "sums": sums,
        "cats": cats,
        "skips": skips,
        "loop": ast.unparse(loops[0]) if loops else "",
    }


# ── reporting markup that must keep rendering ───────────────────────────────

DASHBOARD_STATIC_MARKUP = ("Total Balance", "Total Income", "Total Expenses",
                           "Financial Score", "Savings Goal", "AI Insights",
                           "AI Budgeting", "myChart")


def section_source_guards(base_wd):
    banner("0. source guards (contract shape + frozen surfaces)")
    srcmap = func_sources(SRC)
    fn = srcmap["dashboard"]
    base_fn = func_sources(os.path.join(base_wd, "app.py"))["dashboard"]

    check("0a the Dashboard reads movement_kind in exactly one query (its"
          " aggregation SELECT) beside its two writer INSERTs",
          fn.count("movement_kind") == 3
          and "type, category, movement_kind FROM transactions" in fn
          and fn.count("source, description, movement_kind") == 2,
          fn.count("movement_kind"))

    check("0b Dashboard classifies through the shared K22 helpers",
          all(h in fn for h in ("is_cashflow_income", "is_cashflow_expense",
                                "get_transaction_category")))

    check("0c no raw type-field branch left in the Dashboard surface",
          "['type'] ==" not in fn and '["type"] ==' not in fn)

    audit = dashboard_audit()
    sums = audit["sums"]
    check("0d income and expenses are each computed exactly once, both"
          " through a K22 cashflow guard",
          sorted(sums) == ["expenses", "income"]
          and all(len(v) == 1 for v in sums.values())
          and "is_cashflow_income" in sums["income"][0]
          and "is_cashflow_expense" in sums["expenses"][0], sums)

    bad = [src for word, srcs in sums.items() for src in srcs
           if (word == "income" and "is_cashflow_income" not in src)
           or (word == "expenses" and "is_cashflow_expense" not in src)]
    check("0e no unguarded income/expense accumulation can reappear"
          " (an `else: expenses += ...` fallback)", not bad, bad)

    cats, skips, loop = audit["cats"], audit["skips"], audit["loop"]
    check("0f the single category accumulation sits inside the aggregation"
          " loop behind `if not is_cashflow_expense(t): continue` and reads"
          " get_transaction_category",
          len(cats) == 1 and len(skips) == 1 and cats[0][0] > skips[0][1]
          and "is_cashflow_expense" in loop
          and "get_transaction_category" in loop, (cats, skips, loop[:60]))

    agg_block = fn.split("agg_query = ", 1)[1].split(
        "cur.execute(agg_query", 1)[0]
    check("0g the paged list still reads bare `SELECT *` while the"
          " aggregation query carries no LIMIT/OFFSET (full filtered"
          " history)",
          "SELECT * FROM transactions" in fn
          and "ORDER BY id DESC LIMIT ? OFFSET ?" in fn
          and "LIMIT" not in agg_block and "OFFSET" not in agg_block)

    check("0h Total Balance stays wallet-derived (real_balance) and is never"
          " recomputed as income - expenses",
          "balance = real_balance" in fn and "income - expenses" not in fn
          and "FROM wallets" in fn)

    nested = [type(n).__name__ for n in ast.walk(ast.parse(fn))
              if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
    check("0i no Dashboard-specific classifier is defined inside the surface",
          nested == ["FunctionDef"], nested)

    frozen = [name for name, sha in sorted(PHASE5_START_SHA256.items())
              if name not in MOVED_BY_THIS_PHASE
              and func_sha(SRC, name) != sha]
    check("0j Analytics, PDF and Chat surfaces, the four dashboard"
          " calculation helpers and the three untouched K22 helpers are"
          " byte-identical to the Phase 5 starting state", not frozen, frozen)

    moved = sorted(name for name in MOVED_BY_THIS_PHASE
                   if func_sha(SRC, name) != PHASE5_START_SHA256[name])
    check("0k exactly the Dashboard surface and the two shared row extractors"
          " moved in this phase", moved == sorted(MOVED_BY_THIS_PHASE), moved)

    drifted = [name for name, sha in sorted(PHASE5_END_SHA256.items())
               if func_sha(SRC, name) != sha]
    check("0l the migrated Dashboard and extended extractors match the"
          " frozen Phase 5 end state", not drifted, drifted)

    kind_ar = sorted({int(n) for n in re.findall(
        r"len\(transaction\) == (\d+)", srcmap["_extract_movement_kind_and_type"])})
    cat_ar = sorted({int(n) for n in re.findall(
        r"len\(transaction\) == (\d+)", srcmap["_extract_category"])})
    check("0m the extractors keep every pre-existing arity and add exactly"
          " the Dashboard's 5-tuple (purely additive change)",
          kind_ar == [2, 3, 5, 8, 9, 10] and cat_ar == [4, 5, 8, 10],
          (kind_ar, cat_ar))

    check("0n baseline Dashboard still carries the raw type pattern"
          " (migration is observable)",
          'if t[2] == "income"' in base_fn
          and "category_data[t[3]]" in base_fn
          and "aggregation_rows" in base_fn
          and func_sha(SRC, "dashboard") != PHASE5_START_SHA256["dashboard"])



# ── 1. route contract + rendered reporting markup ───────────────────────────

def repo_db_sha():
    with open(os.path.join(REPO, "database.db"), "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def section_route_and_markup():
    banner("1. /dashboard route contract and rendered reporting markup")
    wd = make_wd("route")
    mod = load_app(wd, "route")
    seed_users(wd)

    code, body = dashboard_get(mod, "", with_user=False)
    check("1a existing login guard kept (no session -> redirect to /login)",
          code == 302, code)

    r = read_dashboard(mod)
    check("1b authenticated Dashboard -> HTTP 200", r["status"] == 200,
          r["status"])

    missing = [m for m in DASHBOARD_STATIC_MARKUP if m not in r["body"]]
    check("1c every reporting card the migration touches still renders",
          not missing, missing)

    check("1d the template still carries the K22-reporting markup",
          all(m in read_text(os.path.join(TEMPLATES_DIR, "index.html"))
              for m in DASHBOARD_STATIC_MARKUP))

    check("1e an empty ledger renders 0.00 income/expenses with no"
          " categories, score 0 and the empty state",
          r["income"] == 0.0 and r["expenses"] == 0.0
          and r["categories"] == {} and r["bars"] == {}
          and r["score"] == 0 and "⚠️ No income" in r["body"]
          and "No transactions yet" in r["body"]
          and "KES 0.00" in r["body"], r)

    check("1f empty ledger Total Balance stays wallet-derived",
          r["balance"] == expected_wallet_balance(mod, wd) != 0.0,
          (r["balance"], expected_wallet_balance(mod, wd)))
    return wd, mod
# ── 2. one reviewed movement at a time (isolated ledgers) ───────────────────

_PROBE_N = [0]


def row(ident, amount, ttype, category, kind,
        created_at="2026-01-15 09:00:00", source="Cash", desc=None,
        currency="KES"):
    label = "row" if kind is None else kind + " row"
    return (ident, amount, currency, ttype, category, source,
            label if desc is None else desc, created_at, kind)


def probe(amount, ttype, category, kind, desc=None):
    """One reviewed movement in an otherwise empty ledger."""
    _PROBE_N[0] += 1
    tag = "p%d" % _PROBE_N[0]
    wd = make_wd(tag)
    mod = load_app(wd, tag)
    seed_users(wd)
    seed_ledger(wd, (row(1, amount, ttype, category, kind, desc=desc),))
    return read_dashboard(mod)


def probe_legacy(amount, ttype, category):
    """One row stored the pre-K21 way: no movement_kind column written at all.

    The column then keeps its ``DEFAULT 'unknown'``, exactly like history
    that predates the K21 migration.
    """
    _PROBE_N[0] += 1
    tag = "p%d" % _PROBE_N[0]
    wd = make_wd(tag)
    mod = load_app(wd, tag)
    seed_users(wd)
    seed_ledger(wd, (row(1, amount, ttype, category, "unknown",
                         desc="legacy row"),), with_kind=False)
    return read_dashboard(mod), wd


def section_movement_matrix():
    banner("2. isolated classification matrix (one movement per ledger)")

    r = probe(100.0, "income", "Probe", "external_in")
    check("2a external_in income -> 100 income, no expense, no category",
          (r["income"], r["expenses"], r["categories"])
          == (100.0, 0.0, {}), r["categories"])

    r = probe(100.0, "income", "Probe", "user_record")
    check("2b user_record income -> 100 income",
          (r["income"], r["expenses"]) == (100.0, 0.0), r)

    r = probe(100.0, "expense", "Probe", "external_out")
    check("2c external_out expense -> 100 expenses in Probe",
          (r["income"], r["expenses"], r["categories"])
          == (0.0, 100.0, {"Probe": 100.0}), r["categories"])

    r = probe(100.0, "expense", "Probe", "user_record")
    check("2d user_record expense -> 100 expenses in Probe",
          (r["income"], r["expenses"], r["categories"])
          == (0.0, 100.0, {"Probe": 100.0}), r["categories"])

    r = probe(100.0, "income", "Transfer", "internal")
    check("2e internal income -> invisible (0 income, 0 expenses, no"
          " category)",
          (r["income"], r["expenses"], r["categories"]) == (0.0, 0.0, {}), r)

    r = probe(100.0, "expense", "Transfer", "internal")
    check("2f internal expense -> invisible (used to inflate Total Expenses)",
          (r["income"], r["expenses"], r["categories"]) == (0.0, 0.0, {}), r)

    r = probe(100.0, "income", "Conversion", "conversion")
    check("2g conversion income -> invisible",
          (r["income"], r["expenses"], r["categories"]) == (0.0, 0.0, {}), r)

    r = probe(100.0, "expense", "Conversion", "conversion")
    check("2h conversion expense -> invisible",
          (r["income"], r["expenses"], r["categories"]) == (0.0, 0.0, {}), r)

    r = probe(100.0, "income", "Legacy", "unknown")
    check("2i unknown income -> fails closed to nothing",
          (r["income"], r["expenses"], r["categories"]) == (0.0, 0.0, {}), r)

    r = probe(100.0, "expense", "Legacy", "unknown")
    check("2j unknown expense -> fails closed to nothing",
          (r["income"], r["expenses"], r["categories"]) == (0.0, 0.0, {}), r)

    r = probe(100.0, "expense", None, "external_out")
    check("2k missing category -> normalized to Other",
          r["categories"] == {"Other": 100.0}, r["categories"])

    r = probe(100.0, "expense", "   ", "external_out")
    check("2l blank category -> normalized to Other",
          r["categories"] == {"Other": 100.0}, r["categories"])

    r = probe(100.0, "expense", " Food ", "external_out")
    check("2m padded category -> trimmed to Food",
          r["categories"] == {"Food": 100.0}, r["categories"])

    r = probe(100.0, "income", "Probe", "external_out")
    check("2n movement_kind wins over the legacy type field"
          " (external_out + type income -> expenses)",
          (r["income"], r["expenses"]) == (0.0, 100.0), r)

    r = probe(100.0, "expense", "Probe", "external_in")
    check("2o external_in + type expense -> income",
          (r["income"], r["expenses"]) == (100.0, 0.0), r)

    r, legacy_wd = probe_legacy(100.0, "income", "Probe")
    check("2p a row stored before K21 (column default 'unknown') counts as"
          " nothing",
          (r["income"], r["expenses"], r["categories"]) == (0.0, 0.0, {})
          and rows(legacy_wd, "SELECT movement_kind FROM transactions"
                              " WHERE id=1")[0][0] == "unknown",
          (r["income"], r["expenses"]))


# ── 3. mixed ledger: the full reporting contract ────────────────────────────

def section_mixed_ledger():
    banner("3. mixed ledger (full-reporting contract)")
    wd = make_wd("mixed")
    mod = load_app(wd, "mixed")
    seed_users(wd)
    seed_ledger(wd)

    r = read_dashboard(mod)
    exp = contract_expectation(wd)

    check("3a Total Income counts external_in + user_record income only",
          r["income"] == exp["income"] == CONTRACT_INCOME,
          (r["income"], exp["income"]))

    check("3b Total Expenses counts cashflow expenses only",
          r["expenses"] == exp["expenses"] == CONTRACT_EXPENSES,
          (r["expenses"], exp["expenses"]))

    check("3c the pre-migration totals are gone (no internal transfer,"
          " conversion leg or unknown row is counted)",
          r["income"] != BUGGED_INCOME and r["expenses"] != BUGGED_EXPENSES
          and CONTRACT_INCOME == BUGGED_INCOME - EXCLUDED_INCOME
          and CONTRACT_EXPENSES == BUGGED_EXPENSES - EXCLUDED_EXPENSES,
          (r["income"], r["expenses"]))

    check("3d the doughnut holds exactly the cashflow expense categories",
          r["categories"] == exp["categories"] == CONTRACT_CATEGORIES,
          (r["categories"], exp["categories"]))

    check("3e no internal/conversion/unknown label reaches the doughnut",
          not ({"Transfer", "Conversion", "Legacy"} & set(r["categories"])),
          r["categories"])

    check("3f Total Balance stays wallet-derived and never becomes"
          " income - expenses",
          r["balance"] == expected_wallet_balance(mod, wd)
          and r["balance"] != exp["net"] == CONTRACT_LEDGER_NET,
          (r["balance"], exp["net"]))

    check("3g the AI Budgeting bars mirror the doughnut and hold no excluded"
          " category",
          set(r["bars"]) == set(CONTRACT_CATEGORIES)
          and all(r["bars"][c]["spent"] == CONTRACT_CATEGORIES[c]
                  for c in CONTRACT_CATEGORIES)
          and all(r["bars"][c]["recommended"] == 390.0
                  for c in CONTRACT_CATEGORIES), r["bars"])

    check("3h insights fire once, on the real top cashflow category only",
          r["body"].count("High spending on") == 1
          and "High spending on Food" in r["body"], r["body"].count(
              "High spending on"))

    check("3i no insight from excluded rows (nothing about exceeding income,"
          " reducing or adding)",
          "expenses exceed your income" not in r["body"]
          and "⚠️ Reduce" not in r["body"]
          and "⚠️ Add income" not in r["body"])

    check("3j budget tips describe the cashflow categories",
          "✅ Good Food" in r["body"] and "✅ Good Coffee" in r["body"]
          and "✅ Good Other" in r["body"])

    check("3k Financial Score is the caller-driven 90 / Excellent",
          r["score"] == DASHBOARD_SCORE
          and r["score_card"] == (DASHBOARD_SCORE, "🔥 Excellent"),
          r["score_card"])

    check("3l Savings Goal reacts to the migrated totals only"
          " (920 saved / 260 target / 100% complete)",
          (r["goal_saved"], r["goal_target"], r["goal_progress"])
          == (GOAL_SAVED, GOAL_TARGET, GOAL_PROGRESS),
          (r["goal_saved"], r["goal_target"], r["goal_progress"]))
    return wd


# ── 4. pagination vs aggregation scope ──────────────────────────────────────

def section_pagination_scope():
    banner("4. pagination covers the page, conversion covers the history")
    wd = make_wd("paging")
    mod = load_app(wd, "paging")
    seed_users(wd)
    seed_ledger(wd)
    exp = contract_expectation(wd)

    p1 = read_dashboard(mod)
    p2 = read_dashboard(mod, "?page=2")
    newest_first = [row_[6] for row_ in sorted(LEDGER, key=lambda x: -x[0])]

    check("4a page 1 lists exactly the page size, newest first",
          len(p1["descriptions"]) == PAGE_SIZE
          and p1["descriptions"] == newest_first[:PAGE_SIZE], p1["descriptions"])

    check("4b page 1 offers Next but no Previous",
          p1["page"] == 1 and p1["has_next"] and not p1["has_prev"],
          (p1["page"], p1["has_next"], p1["has_prev"]))

    check("4c page 2 lists the remainder with Previous but no Next",
          p2["page"] == 2 and p2["descriptions"] == newest_first[PAGE_SIZE:]
          and p2["has_prev"] and not p2["has_next"],
          (p2["descriptions"], p2["has_prev"], p2["has_next"]))

    check("4d every ledger row is listed exactly once across the pages",
          sorted(p1["descriptions"] + p2["descriptions"]) ==
          sorted(newest_first),
          (len(p1["descriptions"]), len(p2["descriptions"])))

    check("4e headline totals aggregate the whole filtered history, not the"
          " page",
          (p1["income"], p1["expenses"]) == (p2["income"], p2["expenses"])
          == (exp["income"], exp["expenses"]) == (CONTRACT_INCOME,
                                                  CONTRACT_EXPENSES),
          (p1["income"], p1["expenses"], p2["income"], p2["expenses"]))

    check("4f the doughnut aggregates the whole filtered history too",
          p1["categories"] == p2["categories"] == exp["categories"]
          == CONTRACT_CATEGORIES, (p1["categories"], p2["categories"]))

    past_code, past_body = dashboard_get(mod, "?page=9")
    check("4g a page past the end still aggregates the whole history",
          past_code == 200 and kpi(past_body, "Total Income") == CONTRACT_INCOME
          and kpi(past_body, "Total Expenses") == CONTRACT_EXPENSES
          and "No transactions yet" in past_body, past_code)
    return wd


# ── 5. filters + search narrow rows, classification still applies ───────────

def section_filters_and_search():
    banner("5. type filter / search still classify every selected row")
    wd = make_wd("filters")
    mod = load_app(wd, "filters")
    seed_users(wd)
    seed_ledger(wd)

    r = read_dashboard(mod, "?filter_type=income")
    check("5a filter_type=income reports the two cashflow incomes only",
          r["income"] == CONTRACT_INCOME and r["expenses"] == 0.0
          and r["categories"] == {} and r["bars"] == {},
          (r["income"], r["expenses"], r["categories"]))

    check("5b filter_type=income still lists the internal and conversion rows"
          " it selected",
          "internal in" in r["descriptions"] and "from USD" in r["descriptions"]
          and len(r["descriptions"]) == 4, r["descriptions"])

    r = read_dashboard(mod, "?filter_type=expense")
    check("5c filter_type=expense reports the four cashflow expenses only",
          r["expenses"] == CONTRACT_EXPENSES and r["income"] == 0.0
          and r["categories"] == CONTRACT_CATEGORIES,
          (r["expenses"], r["income"], r["categories"]))

    check("5d filter_type=expense lists internal/conversion/unknown rows that"
          " contribute nothing",
          all(d in r["descriptions"] for d in ("internal out", "to USD",
                                               "legacy spend"))
          and len(r["descriptions"]) == 8, r["descriptions"])

    check("5e the excluded rows leave no trace in the doughnut or the bars",
          not ({"Transfer", "Conversion", "Legacy"} & set(r["categories"]))
          and r["bars"] == {}, (r["categories"], r["bars"]))

    r = read_dashboard(mod, "?search=lunch")
    check("5f search narrows to one cashflow expense",
          (r["income"], r["expenses"], r["categories"], r["descriptions"])
          == (0.0, 250.0, {"Food": 250.0}, ["lunch food"]), r["descriptions"])

    r = read_dashboard(mod, "?search=internal")
    check("5g search over internal transfers reports nothing",
          (r["income"], r["expenses"], r["categories"]) == (0.0, 0.0, {})
          and len(r["descriptions"]) == 3, r["descriptions"])

    r = read_dashboard(mod, "?search=legacy")
    check("5h search over unknown history reports nothing (fail closed)",
          (r["income"], r["expenses"], r["categories"]) == (0.0, 0.0, {})
          and r["descriptions"] == ["legacy spend"], r["descriptions"])

    r = read_dashboard(mod, "?filter_type=income&search=Conversion")
    check("5i movement_kind wins over a filter that matches the legacy type"
          " field (conversion row -> nothing)",
          (r["income"], r["expenses"], r["categories"]) == (0.0, 0.0, {})
          and r["descriptions"] == ["from USD"], r["descriptions"])

    r = read_dashboard(mod, "?filter_type=bogus")
    check("5j an unknown filter value reports nothing and keeps the empty"
          " state",
          (r["income"], r["expenses"], r["categories"]) == (0.0, 0.0, {})
          and r["descriptions"] == []
          and "No transactions yet" in r["body"], r["descriptions"])

    r = read_dashboard(mod)
    check("5k a filtered request leaves later unfiltered requests on the"
          " contract",
          (r["income"], r["expenses"], r["categories"])
          == (CONTRACT_INCOME, CONTRACT_EXPENSES, CONTRACT_CATEGORIES),
          (r["income"], r["expenses"], r["categories"]))
    return wd


# ── 6. wallet-derived balance + read-only reporting ─────────────────────────

def section_balance_and_read_only():
    banner("6. Total Balance source and read-only reporting")
    wd = make_wd("reads")
    mod = load_app(wd, "reads")
    seed_users(wd)
    seed_ledger(wd)

    wallets_before = rows(wd, WALLET_SQL)
    ledger_before = rows(wd, LEDGER_ECON_SQL)
    notes_before = rows(wd, "SELECT COUNT(*) FROM notifications")[0][0]
    users_before = rows(wd, "SELECT id, username, preferred_currency"
                            " FROM users ORDER BY id")
    expected = expected_wallet_balance(mod, wd)

    r = read_dashboard(mod)
    read_dashboard(mod, "?filter_type=expense")
    read_dashboard(mod, "?page=2&search=lunch")

    check("6a each wallet leg is converted and summed independently of the"
          " ledger",
          r["balance"] == expected
          and abs(expected - WALLET_KES) > 1.0 and expected > WALLET_KES,
          (r["balance"], expected))

    check("6b the balance card is not the ledger net (920.00 proves it)",
          r["balance"] != CONTRACT_LEDGER_NET
          and r["balance"] == expected_wallet_balance(mod, wd))

    check("6c wallets are untouched by dashboard reporting",
          rows(wd, WALLET_SQL) == wallets_before
          and wallets_before == [(1, "KES", WALLET_KES), (1, "USD", WALLET_USD)],
          wallets_before)

    check("6d ledger rows are untouched by dashboard reporting",
          rows(wd, LEDGER_ECON_SQL) == ledger_before
          and len(ledger_before) == len(LEDGER), len(ledger_before))

    check("6e users are untouched by dashboard reporting",
          rows(wd, "SELECT id, username, preferred_currency"
                   " FROM users ORDER BY id") == users_before, users_before)

    check("6f no notification is created by reading the dashboard",
          rows(wd, "SELECT COUNT(*) FROM notifications")[0][0] == notes_before
          and notes_before == 0, notes_before)
    return wd



# ── 7. downstream calculators keep consuming caller-provided figures ────────

CALCULATORS = ("generate_insights", "generate_budget",
               "calculate_financial_score", "generate_savings_goal")


def section_downstream_calculators():
    banner("7. dashboard calculation helpers still trust their caller")
    wd = make_wd("calc")
    mod = load_app(wd, "calc")

    check("7a generate_insights consumes the figures it is handed and"
          " classifies nothing itself",
          mod.generate_insights([], CONTRACT_INCOME, CONTRACT_EXPENSES,
                                dict(CONTRACT_CATEGORIES))
          == ["⚠️ High spending on Food"],
          mod.generate_insights([], CONTRACT_INCOME, CONTRACT_EXPENSES,
                                dict(CONTRACT_CATEGORIES)))

    budget, tips = mod.generate_budget({"Transfer": 500.0, "Food": 250.0},
                                       CONTRACT_INCOME, CONTRACT_EXPENSES)
    check("7b generate_budget renders every category it is handed"
          " (classification happens upstream in the route)",
          set(budget) == {"Transfer", "Food"}
          and budget["Transfer"] == {"spent": 500.0, "recommended": 390.0}
          and len(tips) == 2, budget)

    check("7c generate_budget still refuses to budget without income",
          mod.generate_budget({"Food": 250.0}, 0.0, 380.0)
          == ({}, ["⚠️ Add income"]))

    check("7d generate_budget recognises an over-budget category",
          mod.generate_budget({"Food": 500.0}, 1000.0, 500.0)[1]
          == ["⚠️ Reduce Food"])

    check("7e calculate_financial_score is unchanged for the migrated totals",
          mod.calculate_financial_score(CONTRACT_INCOME, CONTRACT_EXPENSES)
          == (DASHBOARD_SCORE, "🔥 Excellent")
          and mod.calculate_financial_score(0.0, 0.0) == (0, "⚠️ No income"))

    check("7f generate_savings_goal is unchanged for the migrated totals",
          mod.generate_savings_goal(CONTRACT_INCOME, CONTRACT_EXPENSES)
          == {"saved": GOAL_SAVED, "target": GOAL_TARGET,
              "progress": GOAL_PROGRESS}
          and mod.generate_savings_goal(0.0, 0.0)
          == {"saved": 0.0, "target": 0.0, "progress": 0.0},
          mod.generate_savings_goal(CONTRACT_INCOME, CONTRACT_EXPENSES))

    calc_src = func_sources(SRC)
    dirty = [name for name in CALCULATORS
             if "movement_kind" in calc_src[name]
             or "['type']" in calc_src[name]
             or "is_cashflow" in calc_src[name]]
    check("7g the calculation helpers contain no row-classifying code",
          not dirty, dirty)

    check("7h the shared extractors still cover every ledger shape other"
          " surfaces produce",
          mod.is_cashflow_income({"movement_kind": "internal",
                                  "type": "income"}) is False
          and mod.is_cashflow_income(
              ("100", "KES", "income", "Food", "external_in")) is True
          and mod.get_transaction_category(
              ("100", "KES", "expense", "Food", "external_out")) == "Food"
          and mod.is_cashflow_expense(
              ("100", "KES", "expense", "Food")) is False
          and mod.get_transaction_category(
              ("100", "KES", "expense", "Food")) == "Food"
          and mod.is_cashflow_expense(
              (1, 1, "100", "KES", "expense", "Food", "s", "d", "t",
               "external_out")) is True)
    return wd




# ── 8. baseline differential (the same ledger on the pre-migration app) ─────

def section_baseline_differential():
    banner("8. differential against the pre-migration app.py")
    wd = baseline_wd("base")
    mod = load_app(wd, "base")
    seed_users(wd)
    seed_ledger(wd, BASELINE_LEDGER, with_kind=False)   # pre-K21: no column
    base = read_dashboard(mod)

    mig_wd = make_wd("basemig")
    mig_mod = load_app(mig_wd, "basemig")
    seed_users(mig_wd)
    seed_ledger(mig_wd, BASELINE_LEDGER)
    mig = read_dashboard(mig_mod)

    check("8a the baseline app.py has no movement_kind column or helper",
          "movement_kind" not in read_text(os.path.join(wd, "app.py"))
          and "is_cashflow_income" not in read_text(
              os.path.join(wd, "app.py")))

    check("8b baseline reports the pre-migration totals (the bug)",
          (base["income"], base["expenses"])
          == (BUGGED_INCOME, BUGGED_BASELINE_EXPENSES)
          and base["categories"] != BASELINE_CATEGORIES,
          (base["income"], base["expenses"]))

    check("8c baseline doughnut labels the internal/conversion/unknown rows",
          {"Transfer", "Conversion", "Legacy"} <= set(base["categories"]),
          sorted(base["categories"]))

    check("8d the migration removes exactly the excluded legs",
          base["income"] - mig["income"] == EXCLUDED_INCOME
          and base["expenses"] - mig["expenses"] == EXCLUDED_EXPENSES
          and (mig["income"], mig["expenses"])
          == (BASELINE_INCOME, BASELINE_EXPENSES)
          and mig["categories"] == BASELINE_CATEGORIES,
          (base["income"] - mig["income"], base["expenses"] - mig["expenses"],
           mig["categories"]))

    check("8e the Total Balance card did not change meaning (wallet-derived"
          " in both runs)",
          base["balance"] == mig["balance"]
          == expected_wallet_balance(mig_mod, mig_wd)
          and base["balance"] != base["income"] - base["expenses"],
          (base["balance"], mig["balance"]))

    check("8f the ledger and wallets are identical in both runs",
          rows(wd, LEDGER_ECON_SQL) == rows(mig_wd, LEDGER_ECON_SQL)
          and rows(wd, WALLET_SQL) == rows(mig_wd, WALLET_SQL))

    check("8g both runs still render the same reporting surface",
          base["status"] == mig["status"] == 200
          and all(m in base["body"] and m in mig["body"]
                  for m in DASHBOARD_STATIC_MARKUP),
          (base["status"], mig["status"]))

    crash_wd = baseline_wd("crash")
    crash_mod = load_app(crash_wd, "crash")
    seed_users(crash_wd)
    seed_ledger(crash_wd, with_kind=False)
    old_code, _ = dashboard_get(crash_mod)

    fixed_wd = make_wd("crashfixed")
    fixed_mod = load_app(fixed_wd, "crashfixed")
    seed_users(fixed_wd)
    seed_ledger(fixed_wd)
    new = read_dashboard(fixed_mod)
    check("8h history with a missing category no longer breaks the reporting"
          " surface (pre-migration 500 -> migrated 200 with Other)",
          old_code == 500 and new["status"] == 200
          and new["categories"] == CONTRACT_CATEGORIES, (old_code,
                                                        new["status"]))
    return wd, mig_wd, crash_wd, fixed_wd


# ── 9. safety: the repository artefacts are never touched ───────────────────

def section_safety(db_sha_before, src_sha_before):
    banner("9. safety (repository artefacts untouched)")
    check("9a the repository database.db was never opened or written",
          repo_db_sha() == db_sha_before,
          (repo_db_sha()[:16], db_sha_before[:16]))
    with open(SRC, "rb") as fh:
        now = hashlib.sha256(fh.read()).hexdigest()
    check("9b the repository app.py was never modified by this suite",
          now == src_sha_before, (now[:16], src_sha_before[:16]))
    check("9c every imported scenario ran in its own workdir with its own"
          " database (never the repository copy)",
          len(_IMPORTED) >= 25 and len(set(_IMPORTED)) == len(_IMPORTED)
          and all(os.path.exists(os.path.join(wd_, "database.db"))
                  for wd_ in _IMPORTED)
          and all(os.path.abspath(wd_) != REPO for wd_ in _IMPORTED)
          and len(_WORKDIRS) >= len(_IMPORTED),
          (len(_IMPORTED), len(_WORKDIRS)))



def main():
    print("K22 Phase 5 Dashboard reporting-classification suite")
    print("repo:                 " + REPO)
    print("repo database.db is never opened by this suite (SHA-256 only)")

    with open(SRC, "rb") as fh:
        src_sha_before = hashlib.sha256(fh.read()).hexdigest()
    db_sha_before = repo_db_sha()

    started = len(_WORKDIRS)
    guard_wd = baseline_wd("guard")
    try:
        section_source_guards(guard_wd)
        section_route_and_markup()
        section_movement_matrix()
        section_mixed_ledger()
        section_pagination_scope()
        section_filters_and_search()
        section_balance_and_read_only()
        section_downstream_calculators()
        section_baseline_differential()
        section_safety(db_sha_before, src_sha_before)
    finally:
        for wd in _WORKDIRS[started:]:
            shutil.rmtree(wd, ignore_errors=True)
        _WORKDIRS[started:] = []

    print("")
    print("checks: %d passed, %d failed" % (len(PASSED), len(FAILED)))
    if FAILED:
        print("")
        for label in FAILED:
            print("  FAILED: " + label)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
