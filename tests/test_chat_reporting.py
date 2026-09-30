"""K22 Phase 4 Chat/AI reporting-classification suite.

Proves the ``/chat`` financial summary classifies ledger movements
through the shared K22 reporting contract instead of
``type == "income"`` / ``else: expenses``:

  * internal transfers, currency conversions and unknown history add
    nothing to headline income, nothing to headline expenses and
    nothing to spending categories
  * spending categories only collect cashflow expenses, with missing or
    blank categories normalized to ``Other`` by the shared helper
  * ``balance`` stays ``income - expenses`` (never wallet-derived)
  * the Chat response wording, branches, CSRF guard and login contract
    are unchanged - only the underlying figures moved
  * ``generate_budget`` / ``calculate_financial_score`` /
    ``generate_savings_goal`` stay byte-identical (pure functions of
    caller-provided figures; they classify no rows themselves)

Verification: ``/chat`` is a fully local rule-based responder (no AI
API call exists in app.py), so tests POST real messages and parse the
figures from the response text. Every expected value is computed
locally from the approved contract - never by calling the functions
under test.

Safety: every scenario runs against a *copy* of ``app.py`` in an
isolated temp workdir with its own throwaway ``database.db``. The
repository database is never used as a database (only SHA-256 hashed).

Run:  python tests/test_chat_reporting.py
"""

import ast
import hashlib
import importlib.util
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

# Approved-contract expectations for LEDGER, stated literally and also
# recomputed independently by contract_expectation().
CONTRACT_INCOME = 1300.0            # 1000 external_in + 300 user_record
CONTRACT_EXPENSES = 380.0           # 250 + 40 + 60 + 30 cashflow rows
CONTRACT_BALANCE = 920.0
CONTRACT_CATEGORIES = {"Food": 250.0, "Coffee": 40.0, "Other": 90.0}

# What the old ``type == "income"`` / ``else: expenses`` shape produced
# over LEDGER (documenting that the migration is observable).
BUGGED_INCOME = 1900.0
BUGGED_EXPENSES = 2056.0
EXCLUDED_INCOME = 600.0             # internal 500 + conversion 100
EXCLUDED_EXPENSES = 1676.0          # internal 577 + conv 100 + unknown 999

# Wallet seed deliberately unrelated to the ledger, so a wallet-derived
# balance would be trivially distinguishable from income - expenses.
WALLET_SEED = 123456.78
LEDGER_TOTAL = 3956.0              # sum of every LEDGER amount, checked in 8c

# SHA-256 (first 16 hex) of app.py functions recorded at the START of
# Phase 4: the migrated function must differ from CHAT_PHASE_START_SHA256,
# everything else here must not move. Anything a later phase legitimately
# moves is handed over to that phase's suite instead of being silently dropped:
# `dashboard` and the two shared row extractors moved in Phase 5
# (tests/test_dashboard_reporting.py pins all three from here on).
CHAT_PHASE_START_SHA256 = "ad8894660872b938"
PHASE_START_SHA256 = {
    "analytics": "1d79632a1d377ea4",
    "export_analytics_pdf": "ffa87acb05a77093",
    "is_cashflow_income": "642556ad3c707a94",
    "is_cashflow_expense": "9500c143d585350b",
    "is_cashflow_excluded": "566449f49d9bf02b",
    "get_transaction_category": "8b91c0a1c24c9937",
    "generate_insights": "e5f0d3dc8e62e679",
    "generate_budget": "707023558d603ee5",
    "calculate_financial_score": "97a7c529d5f387d1",
    "generate_savings_goal": "868fe09f34afdb4a",
}

TOKEN = "k22ctoken"

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
    wd = tempfile.mkdtemp(prefix="k22ct_" + tag + "_")
    shutil.copyfile(SRC, os.path.join(wd, "app.py"))
    shutil.copytree(TEMPLATES_DIR, os.path.join(wd, "templates"))
    return wd


def baseline_wd(tag):
    """Temp workdir holding the pre-K21/pre-K22 app.py from BASELINE_REV."""
    wd = tempfile.mkdtemp(prefix="k22ct_" + tag + "_")
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
    name = "k22ct_app_" + tag
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


# ── posting to /chat and reading the response figures ───────────────────────

def chat_post(mod, message, with_user=True, with_csrf=True):
    client = mod.app.test_client()
    with client.session_transaction() as sess:
        sess["_csrf_token"] = TOKEN
        if with_user:
            sess["user_id"] = 1
            sess["username"] = "alice"
    payload = {"message": message}
    if with_csrf:
        payload["_csrf_token"] = TOKEN
    resp = client.post("/chat", data=payload)
    return resp.status_code, resp.get_data(as_text=True)


KES_NUM = r"-?[\d,]+(?:\.\d+)?"


def figure(text, label):
    """Value of a ``<label>: KES <amount>`` line in a chat response."""
    m = re.search(re.escape(label) + r": KES (" + KES_NUM + r")", text)
    return None if m is None else float(m.group(1).replace(",", ""))


def totals(text):
    """(income, expenses, balance) from a budget/finance response."""
    return (figure(text, "Income"), figure(text, "Expenses"),
            figure(text, "Balance"))


def spending(text):
    """(total, top_category, top_amount) from a spending response."""
    if "No spending data available." in text:
        return (None, None, None)
    total = figure(text, "Total Expenses")
    m = re.search(r"category:\s*(.*?) \(KES (" + KES_NUM + r")\)", text)
    if m is None:
        return (total, None, None)
    return (total, m.group(1).strip(),
            float(m.group(2).replace(",", "")))


def saved_amount(text):
    m = re.search(r"saved KES (" + KES_NUM + r")", text)
    return None if m is None else float(m.group(1).replace(",", ""))


def savings_rate(text):
    m = re.search(r"savings rate is ([\d.]+)%", text)
    return None if m is None else float(m.group(1))


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
        "balance": income - expenses,
        "categories": categories,
        "top": top,
        "top_amount": categories[top] if top else None,
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


def unguarded_chat_accumulations():
    """Every income/expense/category accumulation must sit in its guard."""
    helpers = {"income": "is_cashflow_income",
               "expenses": "is_cashflow_expense"}
    fn = [n for n in ast.walk(ast.parse(read_text(SRC)))
          if isinstance(n, ast.FunctionDef) and n.name == "chat"][0]
    guards = {
        word: [(n.lineno, n.end_lineno) for n in ast.walk(fn)
               if isinstance(n, ast.If) and guard in ast.unparse(n.test)]
        for word, guard in helpers.items()
    }
    bad = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.AugAssign):
            continue
        target = ast.unparse(node.target)
        if "income" in target:
            word = "income"
        elif "expense" in target or target.startswith("category_data["):
            word = "expenses"
        else:
            continue
        if not any(a <= node.lineno and node.end_lineno <= b
                   for a, b in guards[word]):
            bad.append("line %d: %s" % (node.lineno, ast.unparse(node)))
    return bad


def section_source_guards(base_wd):
    banner("0. source guards (contract shape + frozen surfaces)")
    srcmap = func_sources(SRC)
    base_src = func_sources(os.path.join(base_wd, "app.py"))
    fn = srcmap["chat"]
    base_fn = base_src["chat"]

    check("0a Chat classifies through the shared K22 helpers",
          all(h in fn for h in ("is_cashflow_income", "is_cashflow_expense",
                                "get_transaction_category")))
    check("0b no raw type-field branch left in the Chat surface",
          "['type'] ==" not in fn and '["type"] ==' not in fn)
    bad = unguarded_chat_accumulations()
    check("0c every income/expense/category accumulation sits inside a"
          " K22 cashflow guard", not bad, bad)
    check("0d balance stays income - expenses (never wallet-derived)",
          "balance = income - expenses" in fn
          and "FROM wallets" not in fn
          and "get_wallet_balance" not in fn)

    fields = set()
    for node in ast.walk(ast.parse(fn)):
        if (isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id == "t"
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)):
            fields.add(node.slice.value)
    check("0e rows read only for value - no kind inference from"
          " type/category/description/source",
          fields == {"amount"}, sorted(fields))

    nested = [type(n).__name__ for n in ast.walk(ast.parse(fn))
              if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
    check("0f no Chat-specific classifier is defined inside the surface",
          nested == ["FunctionDef"], nested)

    moved = [name for name, sha in sorted(PHASE_START_SHA256.items())
             if func_sha(SRC, name) != sha]
    check("0g analytics, PDF, the four dashboard calculation helpers and the"
          " four public K22 helpers are byte-identical to the Phase 4"
          " starting state", not moved, moved)

    check("0h baseline chat still carries the raw type pattern"
          " (migration is observable)",
          "t['type'] == \"income\"" in base_fn
          and func_sha(SRC, "chat") != CHAT_PHASE_START_SHA256,
          (func_sha(SRC, "chat"), CHAT_PHASE_START_SHA256))


# ── 1. the route contract is unchanged ──────────────────────────────────────

def repo_db_sha():
    with open(os.path.join(REPO, "database.db"), "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def section_route_contract():
    banner("1. /chat route contract (wording, CSRF, login guard)")
    wd = make_wd("route")
    mod = load_app(wd, "route")
    seed_users(wd)

    code, text = chat_post(mod, "what is my budget")
    check("1a authenticated budget question -> HTTP 200",
          code == 200, code)
    check("1b response carries the KES figures",
          bool(text) and "KES" in text, text[:80])
    check("1c budget wording preserved",
          "Try keeping expenses below 80% of income." in text, text)

    code, help_text = chat_post(mod, "zzz unknown question")
    check("1d fallback help response preserved",
          code == 200 and "I can help with:" in help_text
          and "Spending Analysis" in help_text, help_text[:120])

    code, save_text = chat_post(mod, "how much did i save")
    check("1e save branch wording preserved",
          code == 200 and "You have saved KES" in save_text
          and "savings rate is" in save_text, save_text[:120])

    code, login_text = chat_post(mod, "what is my budget",
                                 with_user=False)
    check("1f existing login guard kept (no user -> login message)",
          code == 200 and login_text == "Please login first.",
          (code, login_text))

    code, _ = chat_post(mod, "what is my budget", with_csrf=False)
    check("1g global CSRF guard intact (missing token -> 403)",
          code == 403, code)

    code, zero_text = chat_post(mod, "what is my budget")
    check("1h zero-data ledger still answers with 0.00 figures",
          code == 200 and totals(zero_text) == (0.0, 0.0, 0.0),
          totals(zero_text))
    return wd, mod


# ── 2. one reviewed movement at a time ──────────────────────────────────────

_PROBE_N = [0]


def row(ident, amount, ttype, category, kind,
        created_at="2026-01-15 09:00:00", source="Src", description="d"):
    return (ident, amount, "KES", ttype, category, source, description,
            created_at, kind)


def probe_chat(spec):
    """Run /chat over a ledger holding only the reviewed rows."""
    _PROBE_N[0] += 1
    tag = "probe_%d" % _PROBE_N[0]
    wd = make_wd(tag)
    mod = load_app(wd, tag)
    seed_users(wd)
    seed_ledger(wd, spec)
    _, budget_text = chat_post(mod, "what is my budget")
    _, spend_text = chat_post(mod, "show my spending")
    return {"budget": budget_text, "spending": spend_text, "wd": wd}


def section_isolated_matrix():
    banner("2. single-movement matrix (one reviewed row per scenario)")

    # 1. external income -> income only
    p = probe_chat((row(1, 1000.0, "income", "Salary", "external_in"),))
    check("2a external_in income -> 1,000.00 / 0.00 / 1,000.00",
          totals(p["budget"]) == (1000.0, 0.0, 1000.0),
          totals(p["budget"]))
    check("2b external_in income -> no spending data",
          spending(p["spending"]) == (None, None, None),
          spending(p["spending"]))

    # 2. external expense -> expenses + category
    p = probe_chat((row(1, 300.0, "expense", "Food", "external_out"),))
    check("2c external_out expense -> 0.00 / 300.00 / -300.00",
          totals(p["budget"]) == (0.0, 300.0, -300.0),
          totals(p["budget"]))
    check("2d external_out expense -> Food spending 300.00",
          spending(p["spending"]) == (300.0, "Food", 300.0),
          spending(p["spending"]))

    # 3. user-record income -> income
    p = probe_chat((row(1, 500.0, "income", "Side Gig", "user_record"),))
    check("2e user_record income -> 500.00 / 0.00 / 500.00",
          totals(p["budget"]) == (500.0, 0.0, 500.0),
          totals(p["budget"]))
    check("2f user_record income -> no spending data",
          spending(p["spending"]) == (None, None, None),
          spending(p["spending"]))

    # 4. user-record expense -> expenses + category
    p = probe_chat((row(1, 200.0, "expense", "Rent", "user_record"),))
    check("2g user_record expense -> 0.00 / 200.00 / -200.00",
          totals(p["budget"]) == (0.0, 200.0, -200.0),
          totals(p["budget"]))
    check("2h user_record expense -> Rent spending 200.00",
          spending(p["spending"]) == (200.0, "Rent", 200.0),
          spending(p["spending"]))

    # 5. internal transfers (both type values) -> nothing
    p = probe_chat((row(1, 500.0, "income", "Transfer", "internal"),))
    check("2i internal income row -> 0.00 / 0.00 / 0.00",
          totals(p["budget"]) == (0.0, 0.0, 0.0), totals(p["budget"]))
    check("2j internal income row -> no spending data",
          spending(p["spending"]) == (None, None, None),
          spending(p["spending"]))

    p = probe_chat((row(1, 400.0, "expense", "Transfer", "internal"),))
    check("2k internal expense row -> 0.00 / 0.00 / 0.00",
          totals(p["budget"]) == (0.0, 0.0, 0.0), totals(p["budget"]))
    check("2l internal expense row -> no spending data",
          spending(p["spending"]) == (None, None, None),
          spending(p["spending"]))

    # 6. conversion, both directions -> nothing
    p = probe_chat((row(1, 100.0, "expense", "Conversion", "conversion"),))
    check("2m conversion expense row -> 0.00 / 0.00 / 0.00",
          totals(p["budget"]) == (0.0, 0.0, 0.0), totals(p["budget"]))
    check("2n conversion expense row -> no spending data",
          spending(p["spending"]) == (None, None, None),
          spending(p["spending"]))

    p = probe_chat((row(1, 100.0, "income", "Conversion", "conversion"),))
    check("2o conversion income row -> 0.00 / 0.00 / 0.00",
          totals(p["budget"]) == (0.0, 0.0, 0.0), totals(p["budget"]))
    check("2p conversion income row -> no spending data",
          spending(p["spending"]) == (None, None, None),
          spending(p["spending"]))

    # 7. unknown fails closed (approved K22 contract)
    p = probe_chat((row(1, 999.0, "expense", "Legacy", "unknown"),))
    check("2q unknown expense row -> 0.00 / 0.00 / 0.00 (fail closed)",
          totals(p["budget"]) == (0.0, 0.0, 0.0), totals(p["budget"]))
    check("2r unknown expense row -> no spending data (fail closed)",
          spending(p["spending"]) == (None, None, None),
          spending(p["spending"]))

    # 8. blank categories on a genuine cashflow expense -> Other
    p = probe_chat((row(1, 100.0, "expense", None, "external_out"),))
    check("2s NULL category expense -> 0.00 / 100.00 / -100.00",
          totals(p["budget"]) == (0.0, 100.0, -100.0),
          totals(p["budget"]))
    check("2t NULL category expense -> category 'Other' 100.00",
          spending(p["spending"]) == (100.0, "Other", 100.0),
          spending(p["spending"]))

    p = probe_chat((row(1, 100.0, "expense", "   ", "external_out"),))
    check("2u whitespace category expense -> 0.00 / 100.00 / -100.00",
          totals(p["budget"]) == (0.0, 100.0, -100.0),
          totals(p["budget"]))
    check("2v whitespace category expense -> category 'Other' 100.00",
          spending(p["spending"]) == (100.0, "Other", 100.0),
          spending(p["spending"]))


# ── 3. mixed ledger: every response shape sees contract figures ─────────────

def section_mixed():
    banner("3. mixed ledger (all four message branches)")
    wd = make_wd("mixed")
    mod = load_app(wd, "mixed")
    seed_users(wd)
    seed_ledger(wd)
    exp = contract_expectation(wd)
    check("3a independent expectation matches the stated contract",
          exp["income"] == CONTRACT_INCOME
          and exp["expenses"] == CONTRACT_EXPENSES
          and exp["balance"] == CONTRACT_BALANCE
          and exp["categories"] == CONTRACT_CATEGORIES, exp)

    _, budget_text = chat_post(mod, "what is my budget")
    got = totals(budget_text)
    check("3b budget branch shows contract figures 1,300 / 380 / 920",
          got == (CONTRACT_INCOME, CONTRACT_EXPENSES, CONTRACT_BALANCE),
          got)

    _, finance_text = chat_post(mod, "how are my finances")
    check("3c finance branch shows contract figures with a healthy"
          " verdict",
          totals(finance_text) == (CONTRACT_INCOME, CONTRACT_EXPENSES,
                                   CONTRACT_BALANCE)
          and "✅ You're spending less than you earn." in finance_text,
          totals(finance_text))

    _, spend_text = chat_post(mod, "show my spending")
    got_spend = spending(spend_text)
    check("3d spending branch totals 380.00 with top Food 250.00",
          got_spend == (CONTRACT_EXPENSES, "Food", 250.0), got_spend)
    check("3e excluded rows leave no footprint in spending text",
          all(word not in spend_text
              for word in ("Transfer", "Conversion", "Legacy")),
          spend_text)
    check("3f blank cashflow categories merge into a single 'Other'"
          " bucket of 90.00",
          spending(probe_chat((
              row(10, 60.0, "expense", None, "external_out"),
              row(11, 30.0, "expense", "   ", "external_out"),
          ))["spending"]) == (90.0, "Other", 90.0), spend_text)

    _, save_text = chat_post(mod, "how much did i save")
    check("3g save branch reports balance 920.00 with rate 70.8%",
          saved_amount(save_text) == CONTRACT_BALANCE
          and savings_rate(save_text) == round(
              CONTRACT_BALANCE / CONTRACT_INCOME * 100, 1), save_text)
    check("3h income - expenses == balance holds across responses",
          totals(budget_text)[0] - totals(budget_text)[1]
          == totals(budget_text)[2]
          and totals(finance_text)[0] - totals(finance_text)[1]
          == totals(finance_text)[2]
          and saved_amount(save_text)
          == totals(budget_text)[0] - totals(budget_text)[1])
    check("3i no wallet figure leaks into any Chat money figure",
          all(str(WALLET_SEED)[:6] not in t
              for t in (budget_text, finance_text, spend_text,
                        save_text)))
    check("3j budget branch rejects the old inflated shape",
          "KES 1,900.00" not in budget_text
          and "KES 2,056.00" not in budget_text, budget_text)
    return wd, mod, exp


# ── 4-6. downstream helpers run on corrected inputs ─────────────────────────

def section_downstream(mod):
    banner("4. downstream helpers see corrected inputs")
    cat = dict(CONTRACT_CATEGORIES)

    budget, tips = mod.generate_budget(cat, CONTRACT_INCOME,
                                       CONTRACT_EXPENSES)
    recommended = round(CONTRACT_INCOME * 0.3, 2)
    expected_budget = {name: {"spent": value, "recommended": recommended}
                       for name, value in cat.items()}
    check("4a generate_budget map uses non-inflated income",
          budget == expected_budget, budget)
    check("4b generate_budget tips pinned to over/under wording",
          tips == ["✅ Good Food", "✅ Good Coffee", "✅ Good Other"],
          tips)

    bugged_map = dict(CONTRACT_CATEGORIES)
    bugged_map["Transfer"] = 577.0
    bugged_map["Conversion"] = 100.0
    bugged_map["Legacy"] = 999.0
    _, bugged_tips = mod.generate_budget(bugged_map, BUGGED_INCOME,
                                          BUGGED_EXPENSES)
    check("4c excluded spending would inflate budget advice (the delta"
          " the migration removes)",
          set(tips) != set(bugged_tips)
          and "⚠️ Reduce Legacy" in bugged_tips, bugged_tips)

    b0, t0 = mod.generate_budget({}, 0, 0)
    check("4d generate_budget income==0 edge preserved",
          b0 == {} and t0 == ["⚠️ Add income"], (b0, t0))

    score, status = mod.calculate_financial_score(CONTRACT_INCOME,
                                                  CONTRACT_EXPENSES)
    check("5a financial score with contract inputs is 90 / Excellent",
          (score, status) == (90, "🔥 Excellent"), (score, status))
    bugged_score, _ = mod.calculate_financial_score(BUGGED_INCOME,
                                                    BUGGED_EXPENSES)
    check("5b financial score with old inflated inputs differs"
          " (delta is observable)",
          bugged_score != score, (bugged_score, score))
    s0, st0 = mod.calculate_financial_score(0, 100)
    check("5c financial score income==0 edge preserved",
          (s0, st0) == (0, "⚠️ No income"), (s0, st0))

    goal = mod.generate_savings_goal(CONTRACT_INCOME, CONTRACT_EXPENSES)
    check("6a savings goal with contract inputs: 920 / 260 / 100",
          goal == {"saved": 920.0, "target": 260.0, "progress": 100.0},
          goal)
    goal0 = mod.generate_savings_goal(0, 100)
    check("6b savings goal income==0 edge preserved",
          goal0 == {"saved": -100.0, "target": 0.0, "progress": 0.0},
          goal0)

    src = func_sources(SRC)
    check("6c the three downstream helpers classify no rows",
          all("['amount']" not in src[name]
              and "['type']" not in src[name] and "for t in" not in src[name]
              for name in ("generate_budget", "calculate_financial_score",
                           "generate_savings_goal")))

    insights = mod.generate_insights([], CONTRACT_INCOME, CONTRACT_EXPENSES,
                                       dict(CONTRACT_CATEGORIES))
    check("6d generate_insights runs on contract inputs",
          isinstance(insights, list) and len(insights) > 0, insights)


# ── 7. baseline differential: the old bug existed and moved ─────────────────

def section_baseline(base_wd):
    banner("7. baseline differential (pre-K21/pre-K22 application)")
    mod = load_app(base_wd, "chatbase")
    seed_users(base_wd)
    seed_ledger(base_wd, with_kind=False)
    code, budget_text = chat_post(mod, "what is my budget")
    got = totals(budget_text)
    check("7a baseline route answers HTTP 200",
          code == 200, code)
    check("7b baseline counts every type=='income' row as income",
          got[0] == BUGGED_INCOME, got)
    check("7c baseline counts every other row as expenses",
          got[1] == BUGGED_EXPENSES, got)
    check("7d migration drops exactly the excluded legs: income"
          " 1900 -> 1300, expenses 2056 -> 380",
          BUGGED_INCOME - CONTRACT_INCOME == EXCLUDED_INCOME
          and BUGGED_EXPENSES - CONTRACT_EXPENSES
          == EXCLUDED_EXPENSES)
    _, spend_text = chat_post(mod, "show my spending")
    base_spend = spending(spend_text)
    check("7e baseline top category is excluded 'Legacy' (the bug)",
          base_spend[1] == "Legacy" and base_spend[2] == 999.0,
          base_spend)


# ── 8. database / wallet safety ─────────────────────────────────────────────

def section_safety(wd, sha_before):
    banner("8. safety (repository database, wallets, ledger)")
    check("8a repository database.db byte-identical after the run",
          repo_db_sha() == sha_before, repo_db_sha())
    wallets = rows(wd, "SELECT currency, balance FROM wallets"
                        " WHERE user_id=1 ORDER BY currency")
    check("8b wallets untouched by the Chat surface",
          wallets == [("KES", WALLET_SEED), ("USD", 42.5)], wallets)
    ledger = rows(wd, "SELECT COUNT(*), ROUND(SUM(amount), 2)"
                       " FROM transactions WHERE user_id=1")
    check("8c Chat posts never write ledger rows",
          ledger == [(len(LEDGER), LEDGER_TOTAL)], ledger)


def main():
    os.environ.pop("DATABASE_URL", None)
    sha_before = repo_db_sha()
    base_wd = baseline_wd("chatbase")
    section_source_guards(base_wd)
    section_route_contract()
    section_isolated_matrix()
    wd, mod, _ = section_mixed()
    section_downstream(mod)
    section_baseline(base_wd)
    section_safety(wd, sha_before)
    print("")
    print("passed=%d failed=%d" % (len(PASSED), len(FAILED)))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())






