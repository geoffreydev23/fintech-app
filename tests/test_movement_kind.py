"""K21 ``movement_kind`` verification suite (data model + ledger writers only).

This phase introduces an authoritative movement kind on the transaction ledger
and makes every transaction writer assign it explicitly. Reporting is NOT part
of this phase, so this suite also proves that no reporting or wallet code moved.

Safety: every scenario runs against a *copy* of ``app.py`` inside an isolated
temp workdir. The app resolves its database through ``__file__``, so the copy
creates and migrates its own throwaway ``database.db``. The repository database
is never opened, never migrated and never modified.

Run:  python tests/test_movement_kind.py
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

KINDS = (
    "external_in",
    "external_out",
    "internal",
    "conversion",
    "user_record",
    "unknown",
)

# Expected movement kind per writer function, in source order per branch.
EXPECTED_KINDS = {
    "deposit": ["external_in", "external_in"],
    "withdraw": ["external_out", "external_out"],
    "send_money": ["external_out", "external_out", "external_in", "external_in"],
    "mpesa": ["external_in", "external_in"],
    "convert_currency_wallet": ["conversion"] * 4,
    "dashboard": ["user_record", "user_record"],
    "transfer_wallet": ["internal"] * 4,
    "restore": ["archived_movement_kind"] * 2,
}

PASSED = []
FAILED = []


def check(label, cond, detail=""):
    (PASSED if cond else FAILED).append(label)
    print(("  PASS " if cond else "  FAIL ") + label
          + ("" if cond else "   [" + str(detail)[:500] + "]"))


def banner(text):
    print("")
    print("== " + text)


# ── isolated workdir + module loading ────────────────────────────────────────

def make_wd(tag):
    wd = tempfile.mkdtemp(prefix="k21_" + tag + "_")
    shutil.copyfile(SRC, os.path.join(wd, "app.py"))
    # templates are needed so the reporting pages actually render (and can be
    # compared against the baseline app rendering the same pages)
    shutil.copytree(TEMPLATES_DIR, os.path.join(wd, "templates"))
    return wd


def baseline_wd(tag):
    """Temp workdir containing the pre-change app.py from BASELINE_REV."""
    wd = tempfile.mkdtemp(prefix="k21_" + tag + "_")
    blobs = subprocess.run(
        ["git", "show", BASELINE_REV + ":app.py"],
        cwd=REPO, capture_output=True, check=True,
    ).stdout
    with open(os.path.join(wd, "app.py"), "wb") as fh:
        fh.write(blobs)
    shutil.copytree(TEMPLATES_DIR, os.path.join(wd, "templates"))
    return wd


class _FakeResponse(object):
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return json.loads(json.dumps(self._payload))


FX_RATES = {
    "KES": {"KES": 1.0, "USD": 0.0077, "EUR": 0.0092, "GBP": 0.0079},
    "USD": {"USD": 1.0, "KES": 130.0, "EUR": 1.19, "GBP": 1.03},
    "EUR": {"EUR": 1.0, "KES": 108.7, "USD": 0.84, "GBP": 0.86},
    "GBP": {"GBP": 1.0, "KES": 126.6, "USD": 0.97, "EUR": 1.16},
}


def install_fx_stub(mod, rates=None):
    table = rates if rates is not None else FX_RATES

    def fake_get(url, timeout=None, **kwargs):
        base = str(url).rstrip("/").split("/")[-1].upper()
        return _FakeResponse({"rates": table.get(base, {})})

    mod.requests.get = fake_get
    return mod


_LOADED = []


def load_app(wd, tag):
    """Import the app copy (init_db() runs on import). Never the repo copy."""
    for name in list(_LOADED):
        sys.modules.pop(name, None)
    _LOADED[:] = []
    name = "k21_app_" + tag
    spec = importlib.util.spec_from_file_location(name, os.path.join(wd, "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)          # runs init_db()
    install_fx_stub(mod)
    _LOADED.append(name)
    return mod


# ── raw database helpers ─────────────────────────────────────────────────────

def raw(wd):
    conn = sqlite3.connect(os.path.join(wd, "database.db"), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def rows(wd, sql, params=()):
    conn = raw(wd)
    try:
        return [tuple(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def exec_sql(wd, sql, params=()):
    conn = raw(wd)
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def table_ddl(wd, table):
    conn = raw(wd)
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return " ".join(row[0].split()) if row else ""
    finally:
        conn.close()


def flat_ddl(wd, table):
    return "".join(table_ddl(wd, table).split()).lower()


def table_names(wd):
    conn = raw(wd)
    try:
        return sorted(r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"))
    finally:
        conn.close()


def columns(wd, table):
    conn = raw(wd)
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(" + table + ")")]
    finally:
        conn.close()


def seed_users(wd):
    conn = raw(wd)
    try:
        conn.execute(
            "INSERT INTO users (id, username, password, email, preferred_currency)"
            " VALUES (?,?,?,?,?)", (1, "alice", "x", "alice@example.com", "KES"))
        conn.execute(
            "INSERT INTO users (id, username, password, email, preferred_currency)"
            " VALUES (?,?,?,?,?)", (2, "bob", "x", "bob@example.com", "KES"))
        for uid, currency in ((1, "KES"), (1, "USD"), (2, "KES")):
            conn.execute(
                "INSERT INTO wallets (user_id, currency, balance) VALUES (?,?,?)",
                (uid, currency, 0.0))
        conn.commit()
    finally:
        conn.close()


# ── source-level helpers ─────────────────────────────────────────────────────

def read_text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def ast_tree(path):
    return ast.parse(read_text(path))


def func_sources(path):
    text = read_text(path)
    lines = text.splitlines()
    out = {}
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.FunctionDef):
            out[node.name] = "\n".join(lines[node.lineno - 1:node.end_lineno])
    return out


def route_paths(path):
    out = []
    for node in ast.walk(ast_tree(path)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "route" and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant):
                out.append(arg.value)
    return sorted(out)


def parse_insert(sql):
    flat = " ".join(sql.split())
    after = flat.split("INSERT INTO transactions", 1)[1]
    start = after.index("(")
    end = after.index(")", start)
    cols = [c.strip() for c in after[start + 1:end].split(",")]
    values = after.split("VALUES", 1)[1]
    inside = values[values.index("(") + 1:values.index(")")]
    return cols, inside.count(",") + 1


def collect_inserts(path):
    """Every literal INSERT INTO transactions, tagged with its writer function."""
    tree = ast_tree(path)
    funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute"):
            continue
        if not node.args:
            continue
        sql_node = node.args[0]
        if not (isinstance(sql_node, ast.Constant)
                and isinstance(sql_node.value, str)):
            continue
        sql = sql_node.value
        if "INSERT INTO transactions" not in sql:
            continue
        owner = None
        for f in funcs:
            if f.lineno <= node.lineno <= (f.end_lineno or f.lineno):
                if owner is None or f.lineno > owner.lineno:
                    owner = f
        params = []
        if len(node.args) > 1 and isinstance(node.args[1], ast.Tuple):
            for elt in node.args[1].elts:
                params.append(elt.value if isinstance(elt, ast.Constant)
                              else ast.unparse(elt))
        cols, n_values = parse_insert(sql)
        found.append({
            "func": owner.name if owner else "<module>",
            "line": node.lineno,
            "cols": cols,
            "n_values": n_values,
            "params": params,
            "placeholder": "pg" if "%s" in sql else "sqlite",
        })
    return found


KIND_LITERAL_LINE = re.compile(
    r'^\s*"(external_in|external_out|internal|conversion|user_record|unknown)",?$')
SQL_PLACEHOLDER_LINE = re.compile(
    r"^\s*(?:VALUES\s*)?\((?:[%s?]+,\s*)*[%s?]+\)\s*,?$")
COLUMN_LIST_LINE = re.compile(r"^\s*\((?:[A-Za-z_]\w*\s*,\s*)*[A-Za-z_]\w*\s*\)\s*,?$")
SELECT_COLUMNS_LINE = re.compile(
    r"^\s*SELECT\s+[A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*\s*$")
WRITER_NOISE_LINE = re.compile(r"^\s*(not in \(|\):)\s*$")

# Functions this phase is allowed to touch, and why:
#   init_db           -> schema + migration
#   the eight writers -> explicit movement_kind assignment
WRITERS = {
    "deposit", "withdraw", "send_money", "mpesa",
    "convert_currency_wallet", "dashboard", "transfer_wallet", "restore",
}

# K22 adds six purely additive reporting-classification helpers (four public
# classifiers plus their two private extractors). None of them exists in
# 68e89f9, so 0h judges 'added' and 'modified' separately: any other
# new name, and any change to a baseline function, still fails 0h.
REPORTING_HELPERS = {
    "is_cashflow_income", "is_cashflow_expense",
    "is_cashflow_excluded", "get_transaction_category",
    "_extract_movement_kind_and_type", "_extract_category",
}

# K22 Phase 2 migrates ONLY the Analytics reporting surface onto the shared
# K22 classification contract, so `analytics` is the one baseline function this
# phase is allowed to rewrite. Everything else must still be byte-identical
# (7f scopes the matching page-body comparison the same way), and the migrated
# Analytics contract itself is pinned in tests/test_analytics_reporting.py.
ANALYTICS_SURFACE = {"analytics"}

# K22 Phase 3 migrates the PDF reporting surface onto the same contract and
# necessarily fixes the pre-existing `story` lifecycle bug inside it, so
# `export_analytics_pdf` is the one further baseline function this phase may
# rewrite. The migrated PDF contract is pinned in
# tests/test_pdf_reporting.py, and check 7h asserts the fixed surface now
# succeeds where baseline demonstrably failed.
PDF_REPORTING_SURFACE = {"export_analytics_pdf"}

# K22 Phase 4 migrates the Chat/AI financial summary onto the same
# contract, so `chat` is the next declared baseline function a phase may
# rewrite. The migrated Chat contract is pinned in
# tests/test_chat_reporting.py.
CHAT_REPORTING_SURFACE = {"chat"}

# K22 Phase 5 migrates the Dashboard reporting surface (headline totals and the
# spending-category chart) onto the same contract, so `dashboard` is the last
# declared baseline function this phase may rewrite. Its K21 writer duty is
# unchanged - checks 0c/0d/0e still pin both movement_kind inserts - and the
# migrated Dashboard contract is pinned in tests/test_dashboard_reporting.py.
DASHBOARD_REPORTING_SURFACE = {"dashboard"}

# Post-migration hardening (a separate, later change - not part of the K21/K22
# classification migration): the password-reset path turned a successful request
# into a 500 on a cp1252 console because send_email()/request_reset() print
# emoji, and export_analytics() wrote its PDF into the working directory instead
# of streaming it. Those three baseline functions are declared touchable here so
# 0h keeps failing on every *other* baseline function, and their fixed behaviour
# is pinned in tests/test_account_and_data.py.
HARDENING_FIXES = {"send_email", "request_reset", "export_analytics"}

TOUCHABLE = (WRITERS | {"init_db"} | ANALYTICS_SURFACE
             | PDF_REPORTING_SURFACE | CHAT_REPORTING_SURFACE
             | DASHBOARD_REPORTING_SURFACE | HARDENING_FIXES)


def normalized_source(text):
    """Text with comments, movement_kind artifacts and placeholder lists removed.

    Used only to prove that the writer functions changed for no other reason.
    """
    kept = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.strip().startswith("#"):
            continue
        if "movement_kind" in line:
            continue
        if KIND_LITERAL_LINE.match(line):
            continue
        if SQL_PLACEHOLDER_LINE.match(line):
            continue
        if COLUMN_LIST_LINE.match(line):
            continue
        if SELECT_COLUMNS_LINE.match(line):
            continue
        if WRITER_NOISE_LINE.match(line):
            continue
        kept.append(line.rstrip().rstrip(","))
    return "\n".join(kept)


# ── legacy (pre-K21) database ────────────────────────────────────────────────

def create_legacy_db(wd):
    """Pre-K21 shape: no movement_kind, no amount CHECK, no wallet CHECK."""
    path = os.path.join(wd, "database.db")
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password TEXT,
            email TEXT,
            preferred_currency TEXT DEFAULT 'KES'
        )
    """)
    cur.execute("""
        CREATE TABLE transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount REAL,
            currency TEXT DEFAULT 'KES',
            type TEXT,
            category TEXT,
            source TEXT,
            description TEXT,
            created_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE wallets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            currency TEXT,
            balance REAL DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE archived_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount REAL,
            currency TEXT NOT NULL DEFAULT 'KES',
            type TEXT,
            category TEXT,
            source TEXT,
            description TEXT
        )
    """)
    cur.execute(
        "INSERT INTO users (id, username, password, email) VALUES (?,?,?,?)",
        (1, "legacy1", "x", "legacy1@example.com"))
    # (1) unambiguous legacy deposit
    cur.execute(
        "INSERT INTO transactions (id, user_id, amount, currency, type, category,"
        " source, description, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (1, 1, 500.0, "KES", "income", "Deposit", "External",
         "legacy deposit", "2024-01-01 10:00:00"))
    # (2) AMBIGUOUS historical Transfer: /send-money and /transfer-wallet legs are
    #     identical in type/category/source, so the kind must NOT be guessed
    cur.execute(
        "INSERT INTO transactions (id, user_id, amount, currency, type, category,"
        " source, description, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (2, 1, 75.0, "KES", "expense", "Transfer", "Wallet",
         "Sent money to legacy2", "2024-01-02 11:00:00"))
    # (3) another ambiguous Transfer, carrying the internal-transfer wording
    cur.execute(
        "INSERT INTO transactions (id, user_id, amount, currency, type, category,"
        " source, description, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (3, 1, 25.0, "GBP", "income", "Transfer", "Wallet",
         "Received 25.0 GBP from USD", "2024-01-03 12:00:00"))
    cur.execute(
        "INSERT INTO archived_transactions (id, user_id, amount, currency, type,"
        " category, source, description) VALUES (?,?,?,?,?,?,?,?)",
        (1, 1, 75.0, "EUR", "expense", "Food", "Wallet", "legacy archived"))
    cur.execute(
        "INSERT INTO wallets (id, user_id, currency, balance) VALUES (?,?,?,?)",
        (1, 1, "KES", 100.0))
    conn.commit()
    conn.close()


TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?")
CLOCK_RE = re.compile(r"\b\d{2}:\d{2}(?::\d{2})?\b")


def normalize_body(body):
    return CLOCK_RE.sub("<CLK>", TS_RE.sub("<TS>", body))


def run_scenario(wd, tag):
    """Drive every ledger-producing route and snapshot the outcome.

    Returns wallets, ledger rows, HTTP statuses and normalized page bodies so a
    pre-change run and a post-change run can be compared exactly.
    """
    mod = load_app(wd, tag)
    seed_users(wd)
    client = mod.app.test_client()

    with client.session_transaction() as sess:
        sess["user_id"] = 1
        sess["username"] = "alice"
        sess["_csrf_token"] = "k21token"

    def post(path, data):
        payload = dict(data)
        payload["_csrf_token"] = "k21token"          # global CSRF guard
        return client.post(path, data=payload)

    statuses = {}
    for name, path, data in (
        ("deposit", "/deposit", {"amount": "1000"}),
        ("withdraw", "/withdraw", {"amount": "100"}),
        ("mpesa", "/mpesa", {"phone": "0700000000", "amount": "200"}),
        ("send_money", "/send-money", {"receiver": "bob", "amount": "50"}),
        ("convert", "/convert-currency",
         {"from_currency": "KES", "to_currency": "USD", "amount": "100"}),
        ("transfer_wallet", "/transfer-wallet",
         {"from_currency": "USD", "to_currency": "KES", "amount": "0.5"}),
        ("dash_income", "/dashboard",
         {"amount": "300", "type": "income", "category": "Salary",
          "source": "External", "description": "salary credit",
          "currency": "KES"}),
        ("dash_expense", "/dashboard",
         {"amount": "20", "type": "expense", "category": "Food",
          "source": "Mobile", "description": "lunch food", "currency": "KES"}),
        ("chat_spend", "/chat", {"message": "how much did i spend"}),
        ("chat_balance", "/chat", {"message": "what is my balance"}),
    ):
        statuses[name] = post(path, data).status_code

    bodies = {}
    for path in ("/dashboard", "/analytics", "/transactions",
                 "/export-transactions", "/wallet", "/archive"):
        resp = client.get(path)
        statuses["GET " + path] = resp.status_code
        bodies[path] = normalize_body(resp.get_data(as_text=True))

    # the PDF export writes a relative file, so run it inside the temp workdir
    here = os.getcwd()
    os.chdir(wd)
    try:
        pdf = client.get("/export-analytics-pdf")
        pdf_outcome = ("status", pdf.status_code,
                       os.path.exists(os.path.join(wd, "analytics_report.pdf")))
    except Exception as exc:                        # pragma: no cover
        pdf_outcome = ("error", type(exc).__name__)
    finally:
        os.chdir(here)

    return {
        "wallets": rows(wd, "SELECT user_id, currency, balance FROM wallets"
                            " ORDER BY user_id, currency"),
        "ledger": rows(wd, "SELECT id, user_id, amount, currency, type, category,"
                           " source, description FROM transactions ORDER BY id"),
        "archive": rows(wd, "SELECT id, user_id, amount, currency, type, category,"
                            " source, description FROM archived_transactions"
                            " ORDER BY id"),
        "statuses": statuses,
        "bodies": bodies,
        "pdf": pdf_outcome,
    }


# ── 0. source-level guards (writers, branches, no collateral change) ─────────

def section_source_guards(base_wd):
    banner("0. source guards: writers, SQL branches, no collateral change")

    inserts = collect_inserts(SRC)
    by_func = {}
    for ins in inserts:
        by_func.setdefault(ins["func"], []).append(ins)

    check("0a exactly 22 literal INSERT INTO transactions sites (11 writes x 2 branches)",
          len(inserts) == 22, len(inserts))
    check("0b writer function set is exactly the expected eight",
          sorted(by_func) == sorted(EXPECTED_KINDS), sorted(by_func))

    for func, expected in sorted(EXPECTED_KINDS.items()):
        got = [ins["params"][-1] if ins["params"] else None
               for ins in by_func.get(func, [])]
        check("0c %s assigns %s" % (func, expected), got == expected, got)

    shape_bad = []
    for ins in inserts:
        if ins["cols"][-1] != "movement_kind":
            shape_bad.append((ins["func"], ins["cols"][-1]))
        elif len(ins["params"]) != len(ins["cols"]):
            shape_bad.append((ins["func"], "params", len(ins["params"]),
                              len(ins["cols"])))
        elif ins["n_values"] != len(ins["cols"]):
            shape_bad.append((ins["func"], "placeholders", ins["n_values"],
                              len(ins["cols"])))
    check("0d movement_kind is the appended last column with matching arity"
          " in every site", not shape_bad, shape_bad)

    parity_bad = []
    for func, group in sorted(by_func.items()):
        pg = [i for i in group if i["placeholder"] == "pg"]
        lite = [i for i in group if i["placeholder"] == "sqlite"]
        if len(pg) != len(lite):
            parity_bad.append((func, "count", len(pg), len(lite)))
            continue
        if [i["params"][-1] for i in pg] != [i["params"][-1] for i in lite]:
            parity_bad.append((func, "kind", [i["params"][-1] for i in pg],
                               [i["params"][-1] for i in lite]))
        elif [i["cols"] for i in pg] != [i["cols"] for i in lite]:
            parity_bad.append((func, "columns"))
    check("0e PostgreSQL and SQLite branches agree on columns and kind",
          not parity_bad, parity_bad)

    srcmap = func_sources(SRC)
    base_src = func_sources(os.path.join(base_wd, "app.py"))

    untouched_names = (
        "analytics", "export_analytics_pdf", "export_analytics", "chat",
        "transactions", "export_transactions", "generate_insights",
        "generate_budget", "calculate_financial_score", "generate_savings_goal",
        "wallet", "archive", "add_wallet", "get_wallet_balance",
        "update_wallet_balance", "convert_currency", "auto_category",
        "get_live_rates", "create_notification", "login", "register",
    )
    offenders = [n for n in untouched_names if "movement_kind" in srcmap.get(n, "")]
    check("0f no reporting / wallet / auth function mentions movement_kind",
          not offenders, offenders)

    # K22 Phase 5 adds movement_kind to the Dashboard aggregation SELECT (the
    # page needs the stored kind to classify through the shared contract), so
    # the old "tail never mentions movement_kind" pin becomes a positive pin:
    # the field is only ever mentioned by the two K21 writer INSERTs and by the
    # aggregation SELECT - the paginated list read stays a bare ``SELECT *``.
    dash_src = srcmap["dashboard"]
    dash_kind_lines = [line.strip() for line in dash_src.splitlines()
                       if "movement_kind" in line]
    kind_inserts = [l for l in dash_kind_lines
                    if l.startswith("(user_id, amount")]
    kind_selects = [l for l in dash_kind_lines if l.startswith("agg_query =")]
    check("0g dashboard reads movement_kind in exactly one query (the"
          " aggregation SELECT) beside its two writer inserts, and the paged"
          " list read stays a bare SELECT *",
          len(dash_kind_lines) == 3 and len(kind_inserts) == 2
          and len(kind_selects) == 1
          and kind_selects[0].endswith('FROM transactions WHERE user_id="')
          and "SELECT * FROM transactions" in dash_src, dash_kind_lines)

    changed = [n for n, text in srcmap.items()
               if n in base_src and n not in TOUCHABLE
               and base_src.get(n) != text]
    added = [n for n in srcmap
             if n not in base_src and n not in REPORTING_HELPERS]
    check("0h every function outside the declared K21/K22 touch sets is"
          " byte-identical to baseline", not (changed + added), changed + added)

    # dashboard is the one writer that is also a declared K22 reporting surface
    # (Phase 5), so it is excluded here: its movement_kind assignment is still
    # pinned by 0c/0d/0e and its migrated classification by
    # tests/test_dashboard_reporting.py.
    wdiff = [n for n in sorted(WRITERS - DASHBOARD_REPORTING_SURFACE)
             if normalized_source(base_src.get(n, "")) != normalized_source(srcmap[n])]
    check("0i the other seven writer functions differ from baseline only by"
          " movement_kind artifacts", not wdiff, wdiff)

    check("0j route table unchanged",
          route_paths(SRC) == route_paths(os.path.join(base_wd, "app.py")),
          (route_paths(SRC), route_paths(os.path.join(base_wd, "app.py"))))

    # PostgreSQL schema is verified statically (no PostgreSQL server in this env)
    pg_text = srcmap["init_db"]
    pg_block = pg_text.split("else:", 1)[0]        # the DATABASE_URL branch
    check("0k fresh PostgreSQL transactions DDL carries movement_kind"
          " NOT NULL DEFAULT 'unknown'",
          "movement_kind TEXT NOT NULL DEFAULT 'unknown'" in pg_block, "")
    check("0l fresh PostgreSQL DDL uses the six-value vocabulary",
          all(k in pg_block for k in KINDS)
          and pg_block.count("movement_kind TEXT NOT NULL DEFAULT 'unknown'") == 2, "")
    check("0m PostgreSQL migration adds named, idempotently guarded CHECKs",
          "ck_transactions_movement_kind" in pg_block
          and "ck_archived_transactions_movement_kind" in pg_block
          and "SELECT 1 FROM pg_constraint WHERE conname = %s" in pg_block, "")


# ── 1. fresh schema ─────────────────────────────────────────────────────────

def section_fresh_schema():
    banner("1. fresh SQLite schema")
    wd = make_wd("fresh")
    load_app(wd, "fresh")

    cols = columns(wd, "transactions")
    dd = flat_ddl(wd, "transactions")
    check("1a fresh transactions has movement_kind", "movement_kind" in cols, cols)
    check("1b movement_kind is the last column (positional reads unaffected)",
          cols[-1] == "movement_kind", cols)
    check("1c NOT NULL DEFAULT 'unknown'",
          "movement_kindtextnotnulldefault'unknown'" in dd, dd[-300:])
    check("1d six-value vocabulary enforced by CHECK",
          "check(movement_kindin(" in dd and all(k in dd for k in KINDS), dd[-300:])

    arch_cols = columns(wd, "archived_transactions")
    check("1e fresh archived_transactions has movement_kind",
          "movement_kind" in arch_cols and arch_cols[-1] == "movement_kind", arch_cols)

    exec_sql(wd, "INSERT INTO transactions (user_id, amount, type)"
                 " VALUES (?,?,?)", (1, 5.0, "income"))
    check("1f writer-less insert defaults to 'unknown'",
          rows(wd, "SELECT movement_kind FROM transactions") == [("unknown",)])

    rejected = False
    try:
        exec_sql(wd, "INSERT INTO transactions (user_id, amount, type, movement_kind)"
                     " VALUES (?,?,?,?)", (1, 5.0, "income", "bogus_kind"))
    except sqlite3.IntegrityError:
        rejected = True
    check("1g off-vocabulary movement_kind is rejected", rejected)

    for kind in KINDS:
        exec_sql(wd, "INSERT INTO transactions (user_id, amount, type, movement_kind)"
                     " VALUES (?,?,?,?)", (1, 1.0, "income", kind))
    check("1h all six vocabulary values are accepted",
          len(rows(wd, "SELECT id FROM transactions")) == 7)


# ── 2. legacy migration, 3. idempotence, 4. ambiguous rows ──────────────────

def section_legacy_migration():
    banner("2. legacy SQLite migration (data preservation, earlier regimes kept)")
    wd = make_wd("legacy")
    create_legacy_db(wd)

    econ = ("SELECT id, user_id, amount, currency, type, category, source,"
            " description, created_at FROM transactions ORDER BY id")
    before = rows(wd, econ)
    wallets_before = rows(wd, "SELECT id, user_id, currency, balance"
                              " FROM wallets ORDER BY id")
    load_app(wd, "legacy")
    after = rows(wd, econ)

    check("2a every legacy transaction row preserved byte-for-byte",
          before == after, (before, after))
    check("2b ids preserved", [r[0] for r in before] == [r[0] for r in after])
    check("2c amounts preserved", [r[2] for r in before] == [r[2] for r in after])
    check("2d currencies preserved", [r[3] for r in before] == [r[3] for r in after])
    check("2e types/categories/sources/descriptions/created_at preserved",
          [r[4:] for r in before] == [r[4:] for r in after])
    check("2f no legacy row failed the migration merely for lacking a kind",
          len(after) == 3)
    check("2g legacy ledger rows are marked 'unknown'",
          rows(wd, "SELECT movement_kind FROM transactions ORDER BY id")
          == [("unknown",), ("unknown",), ("unknown",)])
    check("2h legacy archived rows are marked 'unknown'",
          rows(wd, "SELECT movement_kind FROM archived_transactions")
          == [("unknown",)])
    check("2i movement_kind appended last, history order intact",
          columns(wd, "transactions")[-1] == "movement_kind",
          columns(wd, "transactions"))

    check("2j K17 kept: transactions enforce CHECK(amount > 0)",
          "check(amount>0)" in flat_ddl(wd, "transactions"))
    check("2k K17 kept: archived_transactions enforce CHECK(amount > 0)",
          "check(amount>0)" in flat_ddl(wd, "archived_transactions"))
    check("2l K13 kept: archived currency NOT NULL DEFAULT 'KES'",
          "currencytextnotnulldefault'kes'" in flat_ddl(wd, "archived_transactions"))
    check("2m K19 kept: wallets enforce CHECK(balance >= 0)",
          "check(balance>=0)" in flat_ddl(wd, "wallets"))
    check("2n K15 kept: uq_wallets_user_currency index present",
          "uq_wallets_user_currency" in [r[0] for r in rows(
              wd, "SELECT name FROM sqlite_master WHERE type='index'")])
    check("2o no rebuild leftovers",
          not [t for t in table_names(wd) if t.endswith("_new")], table_names(wd))
    check("2p wallet rows untouched by the migration",
          rows(wd, "SELECT id, user_id, currency, balance FROM wallets ORDER BY id")
          == wallets_before)

    banner("3. migration idempotence")
    schema_before = table_ddl(wd, "transactions")
    arch_before = table_ddl(wd, "archived_transactions")
    load_app(wd, "legacy_again")
    check("3a second init_db pass leaves the transactions DDL identical",
          table_ddl(wd, "transactions") == schema_before)
    check("3b second init_db pass leaves the archived DDL identical",
          table_ddl(wd, "archived_transactions") == arch_before)
    check("3c second init_db pass leaves ledger rows identical", rows(wd, econ) == after)
    check("3d exactly one movement_kind column",
          columns(wd, "transactions").count("movement_kind") == 1)
    check("3e second pass created no duplicate table or unique index",
          table_names(wd).count("transactions") == 1
          and [r[0] for r in rows(wd, "SELECT name FROM sqlite_master"
                                      " WHERE type='index'")]
          .count("uq_wallets_user_currency") == 1)

    banner("4. ambiguous historical Transfer rows are NOT guessed")
    ambiguous = rows(wd, "SELECT description, movement_kind FROM transactions"
                         " WHERE id IN (2,3) ORDER BY id")
    check("4a 'Sent money to ...' Transfer stays 'unknown'",
          ambiguous[0][1] == "unknown", ambiguous)
    check("4b internal-wording Transfer stays 'unknown'",
          ambiguous[1][1] == "unknown", ambiguous)
    check("4c no description parsing manufactured a kind",
          all(row[1] == "unknown" for row in ambiguous), ambiguous)
    check("4d even the unambiguous legacy deposit stays 'unknown'"
          " (history is never invented)",
          rows(wd, "SELECT movement_kind FROM transactions WHERE id=1")
          == [("unknown",)])
    return wd


# ── 5. writers ──────────────────────────────────────────────────────────────

EXPECTED_LEGS = (
    ("Deposited 1000.0 KES", "income", "external_in"),
    ("Withdrawal of Ksh 100.0", "expense", "external_out"),
    ("M-Pesa deposit of Ksh 200.0", "income", "external_in"),
    ("Sent money to bob", "expense", "external_out"),
    ("Received money from alice", "income", "external_in"),
    ("Converted to USD", "expense", "conversion"),
    ("Converted from KES", "income", "conversion"),
    ("Transferred 0.5 USD to KES", "expense", "internal"),
    ("Received 65.0 KES from USD", "income", "internal"),
    ("salary credit", "income", "user_record"),
    ("lunch food", "expense", "user_record"),
)


def section_writer_kinds():
    banner("5. every writer assigns the authoritative kind (11 ledger legs)")
    wd = make_wd("kinds")
    run_scenario(wd, "kinds")
    ledger = rows(wd, "SELECT description, type, movement_kind FROM transactions"
                      " ORDER BY id")

    check("5a eleven ledger rows written by the scenario", len(ledger) == 11, ledger)
    got = [(r[0], r[1], r[2]) for r in ledger]
    check("5b description, direction and movement_kind per leg",
          got == list(EXPECTED_LEGS), got)
    for description, _type, kind in EXPECTED_LEGS:
        check("5c '%s' -> %s" % (description, kind),
              any(r[0] == description and r[2] == kind for r in ledger))

    wallets = {(r[0], r[1]): r[2] for r in rows(
        wd, "SELECT user_id, currency, balance FROM wallets")}
    check("5d wallet arithmetic unchanged: alice KES 1295",
          wallets.get((1, "KES")) == 1295.0, wallets)
    check("5e wallet arithmetic unchanged: alice USD 0.27",
          abs(wallets.get((1, "USD"), 0) - 0.27) < 1e-9, wallets)
    check("5f wallet arithmetic unchanged: recipient bob KES 50",
          wallets.get((2, "KES")) == 50.0, wallets)


# ── 6. restore ──────────────────────────────────────────────────────────────

def section_restore(legacy_wd):
    banner("6. /restore preserves the archived movement kind")

    wd = make_wd("restore")
    mod = load_app(wd, "restore")
    seed_users(wd)
    exec_sql(
        wd, "INSERT INTO archived_transactions (id, user_id, amount, currency,"
            " type, category, source, description, created_at, movement_kind)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (1, 1, 10.0, "KES", "expense", "Conversion", "Wallet",
         "archived conversion", "2024-05-05 09:00:00", "conversion"))
    exec_sql(
        wd, "INSERT INTO archived_transactions (id, user_id, amount, currency,"
            " type, category, source, description, created_at, movement_kind)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (2, 1, 20.0, "KES", "income", "Transfer", "Wallet",
         "archived internal", "2024-05-06 09:00:00", "internal"))

    client = mod.app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = 1
        sess["_csrf_token"] = "k21token"
    for archived_id in (1, 2):
        client.post("/restore/%d" % archived_id,
                    data={"_csrf_token": "k21token"})

    restored = {r[0]: r[1:3] for r in rows(
        wd, "SELECT description, movement_kind, amount FROM transactions"
            " ORDER BY id")}
    check("6a restore keeps 'conversion'",
          restored.get("archived conversion", (None,))[0] == "conversion", restored)
    check("6b restore keeps 'internal'",
          restored.get("archived internal", (None,))[0] == "internal", restored)
    check("6c restore keeps amounts (10.0 / 20.0)",
          restored.get("archived conversion") == ("conversion", 10.0)
          and restored.get("archived internal") == ("internal", 20.0), restored)
    check("6d restored archive rows were consumed",
          rows(wd, "SELECT id FROM archived_transactions") == [])

    # an archive that never recorded a kind restores as 'unknown'
    mod_legacy = load_app(legacy_wd, "legacy_restore")
    client2 = mod_legacy.app.test_client()
    with client2.session_transaction() as sess:
        sess["user_id"] = 1
        sess["_csrf_token"] = "k21token"
    client2.post("/restore/1", data={"_csrf_token": "k21token"})
    legacy_restored = rows(
        legacy_wd,
        "SELECT description, amount, movement_kind FROM transactions"
        " WHERE description='legacy archived'")
    check("6e pre-K21 archive row restores as 'unknown' (never invented)",
          legacy_restored and legacy_restored[0][2] == "unknown", legacy_restored)
    check("6f pre-K21 archive amount preserved (75.0 EUR)",
          legacy_restored and legacy_restored[0][1] == 75.0, legacy_restored)


# ── 7. equivalence with the baseline app ────────────────────────────────────

# K22 Phase 2 migrated /analytics onto the shared reporting classification
# contract and K22 Phase 5 migrated /dashboard onto it, so those two pages'
# numbers - and the categories, insights and budget bars derived from them -
# legitimately move. Every other page must stay byte-identical, and even the
# migrated pages must keep rendering the same complete surface (the exact
# migrated values are pinned in tests/test_analytics_reporting.py and
# tests/test_dashboard_reporting.py).

ANALYTICS_STATIC_MARKUP = (
    "Analytics Dashboard",
    "Download Analytics Report",
    "Financial overview and AI-powered insights.",
    "Income",
    "Expenses",
    "Balance",
    "Financial Score",
    "Savings Goal",
    "Complete",
    "Spending By Category",
    "Top Spending Categories",
    "AI Insights",
    "Monthly Income vs Expenses",
    "categoryChart",
    "monthlyChart",
    "KES ",
)

# /dashboard renders templates/index.html: the two headline KPI cards, the
# wallet-derived balance card and the derived insight/budget/chart widgets.
DASHBOARD_STATIC_MARKUP = (
    "Total Balance",
    "Total Income",
    "Total Expenses",
    "Financial Score",
    "Savings Goal",
    "AI Insights",
    "AI Budgeting",
    "myChart",
    "KES ",
)

# Page -> the static markup that page must still render in full.
CLASSIFICATION_MIGRATED_PATHS = {
    "/analytics": ANALYTICS_STATIC_MARKUP,
    "/dashboard": DASHBOARD_STATIC_MARKUP,
}


def section_differential(base_wd):
    banner("7. equivalence with baseline " + BASELINE_REV
           + " (wallets, amounts, reporting)")
    wd_mod = make_wd("diff_mod")
    wd_base = baseline_wd("diff_base")
    shutil.copyfile(os.path.join(base_wd, "app.py"),
                    os.path.join(wd_base, "app.py"))

    snap_mod = run_scenario(wd_mod, "diff_mod")
    snap_base = run_scenario(wd_base, "diff_base")

    check("7a wallet balances identical to baseline",
          snap_mod["wallets"] == snap_base["wallets"],
          (snap_mod["wallets"], snap_base["wallets"]))
    check("7b ledger economic columns identical to baseline",
          snap_mod["ledger"] == snap_base["ledger"],
          (snap_mod["ledger"], snap_base["ledger"]))
    check("7c transaction amounts unchanged",
          [r[2] for r in snap_mod["ledger"]] == [r[2] for r in snap_base["ledger"]])
    check("7d archived rows identical to baseline",
          snap_mod["archive"] == snap_base["archive"])
    check("7e route statuses identical to baseline",
          snap_mod["statuses"] == snap_base["statuses"],
          (snap_mod["statuses"], snap_base["statuses"]))
    for path in sorted(snap_base["bodies"]):
        if path in CLASSIFICATION_MIGRATED_PATHS:
            body_mod = snap_mod["bodies"][path]
            body_base = snap_base["bodies"][path]
            markup = CLASSIFICATION_MIGRATED_PATHS[path]
            check("7f %s keeps its full reporting surface and moves only its"
                  " reporting values" % path,
                  all(m in body_mod for m in markup)
                  and all(m in body_base for m in markup)
                  and body_mod != body_base)
            continue
        check("7f %s body identical to baseline" % path,
              snap_mod["bodies"][path] == snap_base["bodies"][path])
    for path, code in sorted(snap_base["statuses"].items()):
        if path.startswith("GET "):
            check("7g %s still returns 200 (body comparison is meaningful)"
                  % path, code == 200, code)
    # The PDF export is a declared K22 surface (PDF_REPORTING_SURFACE): its
    # pre-existing UnboundLocalError ('story' referenced before
    # initialization) is fixed by design, so plain outcome identity no
    # longer applies to this one surface. The assertion gets strictly
    # stronger instead of weaker - baseline must still fail (the defect
    # predates K21/K22) while the migrated surface must succeed.
    check("7h PDF export: baseline still fails, migrated surface returns 200",
          snap_base["pdf"] != ("status", 200, False)
          and snap_mod["pdf"] == ("status", 200, False),
          (snap_mod["pdf"], snap_base["pdf"]))

    kinds = [r[0] for r in rows(wd_mod, "SELECT movement_kind FROM transactions"
                                        " ORDER BY id")]
    check("7i every row written in this phase is classified (no 'unknown')",
          kinds == [leg[2] for leg in EXPECTED_LEGS], kinds)
    check("7j baseline table has no movement_kind column"
          " (the field is the only delta)",
          "movement_kind" not in columns(wd_base, "transactions"))
    check("7k wallet rows unchanged by classification work",
          snap_mod["wallets"] == snap_base["wallets"])


def main():
    # the app prints emoji; a redirected cp1252 console would raise
    # UnicodeEncodeError inside send_email(), which the dashboard POST swallows
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print("K21 movement_kind verification suite")
    print("repo:                 " + REPO)
    print("repo database.db is never opened by this suite")

    for name in ("DATABASE_URL", "OPENAI_API_KEY", "SENDGRID_API_KEY", "FROM_EMAIL"):
        os.environ.pop(name, None)

    base_wd = baseline_wd("baseline")
    section_source_guards(base_wd)
    section_fresh_schema()
    legacy_wd = section_legacy_migration()
    section_restore(legacy_wd)
    section_writer_kinds()
    section_differential(base_wd)

    banner("summary")
    print("passed: %d   failed: %d" % (len(PASSED), len(FAILED)))
    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print("  - " + name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())









