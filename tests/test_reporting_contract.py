"""Reporting classification contract verification suite (Phase 1 / K22).

Tests pure classification helpers establishing the unified reporting contract:
  - is_cashflow_income(transaction)
  - is_cashflow_expense(transaction)
  - is_cashflow_excluded(transaction)
  - get_transaction_category(transaction)

Run:  python tests/test_reporting_contract.py
"""

import os
import sys
import sqlite3

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from app import (
    is_cashflow_income,
    is_cashflow_expense,
    is_cashflow_excluded,
    get_transaction_category,
)

PASSED = []
FAILED = []


def check(label, cond, detail=""):
    (PASSED if cond else FAILED).append(label)
    print(("  PASS " if cond else "  FAIL ") + label
          + ("" if cond else "   [" + str(detail)[:500] + "]"))


def banner(text):
    print("\n== " + text)


class DummyTx(object):
    def __init__(self, movement_kind=None, type_=None, category=None):
        self.movement_kind = movement_kind
        self.type = type_
        self.category = category


def run_tests():
    banner("1. exact cashflow classification rules (dict representation)")
    
    # external_in: income=True, expense=False
    tx = {"movement_kind": "external_in", "type": "income"}
    check("external_in -> income", is_cashflow_income(tx) is True)
    check("external_in -> not expense", is_cashflow_expense(tx) is False)
    check("external_in -> not excluded", is_cashflow_excluded(tx) is False)

    # external_out: income=False, expense=True
    tx = {"movement_kind": "external_out", "type": "expense"}
    check("external_out -> expense", is_cashflow_expense(tx) is True)
    check("external_out -> not income", is_cashflow_income(tx) is False)
    check("external_out -> not excluded", is_cashflow_excluded(tx) is False)

    # user_record + income: income=True, expense=False
    tx = {"movement_kind": "user_record", "type": "income"}
    check("user_record income -> income", is_cashflow_income(tx) is True)
    check("user_record income -> not expense", is_cashflow_expense(tx) is False)
    check("user_record income -> not excluded", is_cashflow_excluded(tx) is False)

    # user_record + expense: income=False, expense=True
    tx = {"movement_kind": "user_record", "type": "expense"}
    check("user_record expense -> expense", is_cashflow_expense(tx) is True)
    check("user_record expense -> not income", is_cashflow_income(tx) is False)
    check("user_record expense -> not excluded", is_cashflow_excluded(tx) is False)

    # internal: income=False, expense=False, excluded=True
    tx_int_exp = {"movement_kind": "internal", "type": "expense"}
    tx_int_inc = {"movement_kind": "internal", "type": "income"}
    check("internal expense -> not income", is_cashflow_income(tx_int_exp) is False)
    check("internal expense -> not expense", is_cashflow_expense(tx_int_exp) is False)
    check("internal income -> not income", is_cashflow_income(tx_int_inc) is False)
    check("internal income -> not expense", is_cashflow_expense(tx_int_inc) is False)
    check("internal -> excluded", is_cashflow_excluded(tx_int_exp) is True and is_cashflow_excluded(tx_int_inc) is True)

    # conversion: income=False, expense=False, excluded=True
    tx_conv_exp = {"movement_kind": "conversion", "type": "expense"}
    tx_conv_inc = {"movement_kind": "conversion", "type": "income"}
    check("conversion expense -> not income", is_cashflow_income(tx_conv_exp) is False)
    check("conversion expense -> not expense", is_cashflow_expense(tx_conv_exp) is False)
    check("conversion income -> not income", is_cashflow_income(tx_conv_inc) is False)
    check("conversion income -> not expense", is_cashflow_expense(tx_conv_inc) is False)
    check("conversion -> excluded", is_cashflow_excluded(tx_conv_exp) is True and is_cashflow_excluded(tx_conv_inc) is True)


    banner("2. deterministic fail-closed rules (unknown, invalid, missing)")

    # unknown + income: neither
    tx_unk_inc = {"movement_kind": "unknown", "type": "income"}
    check("unknown income -> not income", is_cashflow_income(tx_unk_inc) is False)
    check("unknown income -> not expense", is_cashflow_expense(tx_unk_inc) is False)
    check("unknown income -> not excluded", is_cashflow_excluded(tx_unk_inc) is False)

    # unknown + expense: neither
    tx_unk_exp = {"movement_kind": "unknown", "type": "expense"}
    check("unknown expense -> not income", is_cashflow_income(tx_unk_exp) is False)
    check("unknown expense -> not expense", is_cashflow_expense(tx_unk_exp) is False)
    check("unknown expense -> not excluded", is_cashflow_excluded(tx_unk_exp) is False)

    # invalid / unrecognized movement_kind: neither
    for bad_kind in ("invalid_kind", "transfer", "deposit_legacy", "mpesa_in", "", None):
        tx_bad = {"movement_kind": bad_kind, "type": "expense"}
        check("invalid movement_kind (%s) -> not expense" % bad_kind, is_cashflow_expense(tx_bad) is False)
        check("invalid movement_kind (%s) -> not income" % bad_kind, is_cashflow_income(tx_bad) is False)
        check("invalid movement_kind (%s) -> not excluded" % bad_kind, is_cashflow_excluded(tx_bad) is False)

    # None and empty structures
    check("None tx -> not income", is_cashflow_income(None) is False)
    check("None tx -> not expense", is_cashflow_expense(None) is False)
    check("None tx -> not excluded", is_cashflow_excluded(None) is False)
    check("empty dict -> not income", is_cashflow_income({}) is False)
    check("empty dict -> not expense", is_cashflow_expense({}) is False)

    banner("3. defense against accidental expense classification (no fallback leaks)")

    # Internal transfers and conversions cannot accidentally become expenses through an else fallback
    for k in ("internal", "conversion"):
        row_dict = {"movement_kind": k, "type": "expense", "amount": 100.0}
        check("%s type=expense CANNOT become cashflow expense" % k,
              is_cashflow_expense(row_dict) is False)
        check("%s type=expense CANNOT become cashflow income" % k,
              is_cashflow_income(row_dict) is False)

    # Legacy unknown row with type='expense' cannot accidentally become cashflow expense
    row_legacy = {"movement_kind": "unknown", "type": "expense", "amount": 250.0}
    check("unknown legacy expense CANNOT become cashflow expense without classification",
          is_cashflow_expense(row_legacy) is False)

    banner("4. category normalization")

    check("category None -> Other", get_transaction_category({"category": None}) == "Other")
    check("category '' -> Other", get_transaction_category({"category": ""}) == "Other")
    check("category whitespace spaces -> Other", get_transaction_category({"category": "   "}) == "Other")
    check("category whitespace tab/newline -> Other", get_transaction_category({"category": "\t  \n"}) == "Other")
    check("category missing key -> Other", get_transaction_category({}) == "Other")
    check("category None tx -> Other", get_transaction_category(None) == "Other")

    check("normal category 'Food' -> 'Food'", get_transaction_category({"category": "Food"}) == "Food")
    check("normal category '  Groceries  ' trimmed -> 'Groceries'",
          get_transaction_category({"category": "  Groceries  "}) == "Groceries")
    check("normal category 'Utilities & Bills' -> 'Utilities & Bills'",
          get_transaction_category({"category": "Utilities & Bills"}) == "Utilities & Bills")


    banner("5. multi-representation interoperability")

    # 10-element tuple/list (full transactions row: id, user_id, amount, currency, type, category, source, description, created_at, movement_kind)
    t_10_inc = (101, 1, 500.0, "KES", "income", "Salary", "Bank", "Salary payment", "2026-03-01", "external_in")
    check("10-tuple external_in -> income", is_cashflow_income(t_10_inc) is True)
    check("10-tuple external_in -> not expense", is_cashflow_expense(t_10_inc) is False)
    check("10-tuple category -> Salary", get_transaction_category(t_10_inc) == "Salary")

    t_10_exp = (102, 1, 75.0, "KES", "expense", "  Food  ", "Cash", "Dinner", "2026-03-01", "external_out")
    check("10-tuple external_out -> expense", is_cashflow_expense(t_10_exp) is True)
    check("10-tuple external_out -> not income", is_cashflow_income(t_10_exp) is False)
    check("10-tuple category trimmed -> Food", get_transaction_category(t_10_exp) == "Food")

    t_10_internal = (103, 1, 200.0, "KES", "expense", None, "Wallet", "Transfer to EUR", "2026-03-01", "internal")
    check("10-tuple internal -> not expense", is_cashflow_expense(t_10_internal) is False)
    check("10-tuple internal -> not income", is_cashflow_income(t_10_internal) is False)
    check("10-tuple internal -> excluded", is_cashflow_excluded(t_10_internal) is True)
    check("10-tuple None category -> Other", get_transaction_category(t_10_internal) == "Other")

    t_10_conv = (104, 1, 150.0, "EUR", "income", "Conversion", "Wallet", "Converted", "2026-03-01", "conversion")
    check("10-tuple conversion -> not expense", is_cashflow_expense(t_10_conv) is False)
    check("10-tuple conversion -> not income", is_cashflow_income(t_10_conv) is False)
    check("10-tuple conversion -> excluded", is_cashflow_excluded(t_10_conv) is True)

    t_10_user_inc = (105, 1, 100.0, "USD", "income", "Side Gig", "Cash", "Consulting", "2026-03-01", "user_record")
    check("10-tuple user_record income -> income", is_cashflow_income(t_10_user_inc) is True)
    check("10-tuple user_record income -> not expense", is_cashflow_expense(t_10_user_inc) is False)

    t_10_user_exp = (106, 1, 40.0, "USD", "expense", "Coffee", "Cash", "Coffee shop", "2026-03-01", "user_record")
    check("10-tuple user_record expense -> expense", is_cashflow_expense(t_10_user_exp) is True)
    check("10-tuple user_record expense -> not income", is_cashflow_income(t_10_user_exp) is False)

    t_10_unk_exp = (107, 1, 30.0, "KES", "expense", "Shopping", "Card", "Old shopping", "2026-01-01", "unknown")
    check("10-tuple unknown expense -> neither", is_cashflow_expense(t_10_unk_exp) is False and is_cashflow_income(t_10_unk_exp) is False)

    # sqlite3.Row representation
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE t_sample (
            id INTEGER PRIMARY KEY,
            user_id INTEGER,
            amount REAL,
            currency TEXT,
            type TEXT,
            category TEXT,
            source TEXT,
            description TEXT,
            created_at TEXT,
            movement_kind TEXT
        )
    """)
    cur.execute("""
        INSERT INTO t_sample VALUES (1, 1, 50.0, 'KES', 'expense', '  Rent  ', 'Bank', 'Rent pay', '2026-01-01', 'external_out')
    """)
    cur.execute("""
        INSERT INTO t_sample VALUES (2, 1, 120.0, 'KES', 'income', 'Salary', 'Bank', 'Bonus', '2026-01-01', 'external_in')
    """)
    cur.execute("""
        INSERT INTO t_sample VALUES (3, 1, 80.0, 'KES', 'expense', '', 'Wallet', 'To USD', '2026-01-01', 'conversion')
    """)
    cur.execute("""
        INSERT INTO t_sample VALUES (4, 1, 25.0, 'KES', 'expense', 'Old', 'Card', 'Legacy', '2026-01-01', 'unknown')
    """)
    conn.commit()

    rows = cur.execute("SELECT * FROM t_sample ORDER BY id").fetchall()
    row_out, row_in, row_conv, row_unk = rows[0], rows[1], rows[2], rows[3]

    check("sqlite3.Row external_out -> expense", is_cashflow_expense(row_out) is True)
    check("sqlite3.Row external_out category trimmed -> Rent", get_transaction_category(row_out) == "Rent")
    check("sqlite3.Row external_in -> income", is_cashflow_income(row_in) is True)
    check("sqlite3.Row conversion -> excluded", is_cashflow_excluded(row_conv) is True)
    check("sqlite3.Row conversion -> not expense", is_cashflow_expense(row_conv) is False)
    check("sqlite3.Row blank category -> Other", get_transaction_category(row_conv) == "Other")
    check("sqlite3.Row unknown expense -> neither", is_cashflow_expense(row_unk) is False and is_cashflow_income(row_unk) is False)

    conn.close()

    # Object with attributes
    obj_tx = DummyTx(movement_kind="external_in", type_="income", category="Freelance")
    check("attribute object external_in -> income", is_cashflow_income(obj_tx) is True)
    check("attribute object category -> Freelance", get_transaction_category(obj_tx) == "Freelance")

    banner("summary")
    print("passed: %d   failed: %d" % (len(PASSED), len(FAILED)))
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    run_tests()

