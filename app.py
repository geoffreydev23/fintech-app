from flask import (
    Flask,
    render_template,
    request,
    redirect,
    session,
    url_for,
    abort,
    flash,
    Response,
    send_file
)

import sqlite3
import os
import secrets
import random
import smtplib
import csv
from io import BytesIO, StringIO
import matplotlib.pyplot as plt
import tempfile

from email.mime.text import MIMEText
from reportlab.platypus import Image
from datetime import datetime, timedelta, timezone
from werkzeug.security import (
    generate_password_hash,
    check_password_hash
)

# PDF REPORTS
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle
)

from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_CENTER
from reportlab.pdfbase import pdfmetrics

from io import BytesIO
from flask import send_file


# 🐘 PostgreSQL
import psycopg2
from psycopg2 import extras
from urllib.parse import urlparse

# 🆕 DATABASE CONFIG (ADD THIS SECTION)
DATABASE_URL = os.getenv("DATABASE_URL")

def get_db_connection():
    if DATABASE_URL:
        # 🐘 PostgreSQL
        url = urlparse(DATABASE_URL)
        conn = psycopg2.connect(
            host=url.hostname,
            database=url.path[1:],
            user=url.username,
            password=url.password,
            port=url.port,
            cursor_factory=extras.DictCursor
        )
        return conn
    else:
        # 🪶 SQLite (local)
        conn = sqlite3.connect(
            os.path.join(os.path.dirname(__file__), 'database.db'),
            timeout=30
        )

        conn.row_factory = sqlite3.Row

        return conn
    
# 🆕 LOAD ENV VARIABLES (PERMANENT FIX)
from dotenv import load_dotenv
load_dotenv()

# ✅ SAFE OPENAI IMPORT
try:
    from openai import OpenAI
except:
    OpenAI = None

app = Flask(__name__)

app.secret_key = "super_secret_key_change_this"

# 🔐 SESSION SECURITY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=False,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=15)
)

# 🔐 EMAIL CONFIG (SENDGRID)
import os
import requests

SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")
FROM_EMAIL = os.getenv("FROM_EMAIL")  # must match verified sender

# 📧 SEND EMAIL FUNCTION (SENDGRID API - SAFE + IMPROVED)
def send_email(to_email, subject, message):
    if not SENDGRID_API_KEY:
        print("❌ Missing SENDGRID_API_KEY")
        return False

    if not FROM_EMAIL:
        print("❌ Missing FROM_EMAIL")
        return False

    try:
        url = "https://api.sendgrid.com/v3/mail/send"

        headers = {
            "Authorization": f"Bearer {SENDGRID_API_KEY}",
            "Content-Type": "application/json"
        }

        data = {
            "personalizations": [
                {
                    "to": [{"email": to_email}],
                    "subject": subject
                }
            ],
            "from": {
                "email": FROM_EMAIL,
                "name": "Fintech App"  # ✅ optional branding (safe)
            },
            "content": [
                {
                    "type": "text/plain",
                    "value": message
                }
            ]
        }

        # ⏱️ Timeout prevents server hanging
        response = requests.post(url, headers=headers, json=data, timeout=10)

        # ✅ SUCCESS
        if response.status_code in [200, 202]:
            print("✅ Email sent successfully")
            return True

        # ❌ FAILURE (detailed log)
        print("❌ SendGrid error:")
        print("Status Code:", response.status_code)
        print("Response Body:", response.text)
        return False

    except requests.exceptions.Timeout:
        print("❌ SendGrid timeout (network issue)")
        return False

    except Exception as e:
        print("❌ Email exception:", str(e))
        return False  # 🚨 NEVER crash your app

# 🔐 SAFE API KEY
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY and OpenAI else None

# 📁 DATABASE
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
db_path = os.path.join(BASE_DIR, 'database.db')

# 🔐 CSRF TOKEN
def generate_csrf_token():
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_hex(16)
    return session["_csrf_token"]

@app.before_request
def csrf_protect():
    if request.method == "POST":
        token = session.get("_csrf_token")
        form_token = request.form.get("_csrf_token")
        if not token or token != form_token:
            abort(403)

app.jinja_env.globals['csrf_token'] = generate_csrf_token

# 🔐 SECURITY HEADERS
@app.after_request
def set_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    return response

# 🔐 PASSWORD STRENGTH
def is_strong_password(password):
    return (
        len(password) >= 8 and
        any(c.isupper() for c in password) and
        any(c.isdigit() for c in password)
    )

# 💳 GET OR CREATE WALLET
def get_wallet_balance(user_id, currency, conn=None):

    # 🔌 TRANSACTION-AWARE: use the supplied connection when given,
    # otherwise open (and own) a standalone connection as before
    if conn is None:
        own_conn = get_db_connection()
    else:
        own_conn = conn

    cur = own_conn.cursor()

    if DATABASE_URL:

        cur.execute(
            """
            SELECT balance
            FROM wallets
            WHERE user_id=%s
            AND currency=%s
            """,
            (user_id, currency)
        )

    else:

        cur.execute(
            """
            SELECT balance
            FROM wallets
            WHERE user_id=?
            AND currency=?
            """,
            (user_id, currency)
        )

    wallet = cur.fetchone()

    # ✅ CREATE WALLET IF MISSING
    if not wallet:

        if DATABASE_URL:

            cur.execute(
                """
                INSERT INTO wallets
                (user_id, currency, balance)
                VALUES (%s,%s,%s)
                """,
                (user_id, currency, 0)
            )

        else:

            cur.execute(
                """
                INSERT INTO wallets
                (user_id, currency, balance)
                VALUES (?,?,?)
                """,
                (user_id, currency, 0)
            )

        # 💾 Only the connection owner commits (standalone mode)
        if conn is None:
            own_conn.commit()

        balance = 0

    else:

        balance = wallet[0]

    cur.close()

    # 🔒 Only the connection owner closes (standalone mode)
    if conn is None:
        own_conn.close()

    return balance

# 💳 UPDATE WALLET BALANCE
def update_wallet_balance(user_id, currency, amount, action, conn=None):

    # 🔌 TRANSACTION-AWARE: use the supplied connection when given,
    # otherwise open (and own) a standalone connection as before
    if conn is None:
        own_conn = get_db_connection()
    else:
        own_conn = conn

    cur = own_conn.cursor()

    # ✅ CREATE WALLET IF MISSING (always shares this connection)
    get_wallet_balance(user_id, currency, conn=own_conn)

    success = True

    # ➕ ADD MONEY
    if action == "add":

        if DATABASE_URL:
            cur.execute(
                """
                UPDATE wallets
                SET balance = balance + %s
                WHERE user_id=%s
                AND currency=%s
                """,
                (amount, user_id, currency)
            )

        else:
            cur.execute(
                """
                UPDATE wallets
                SET balance = balance + ?
                WHERE user_id=?
                AND currency=?
                """,
                (amount, user_id, currency)
            )

    # ➖ REMOVE MONEY (atomic sufficient-funds guard)
    elif action == "subtract":

        if DATABASE_URL:
            cur.execute(
                """
                UPDATE wallets
                SET balance = balance - %s
                WHERE user_id=%s
                AND currency=%s
                AND balance >= %s
                """,
                (amount, user_id, currency, amount)
            )

        else:
            cur.execute(
                """
                UPDATE wallets
                SET balance = balance - ?
                WHERE user_id=?
                AND currency=?
                AND balance >= ?
                """,
                (amount, user_id, currency, amount)
            )

        # ❌ rowcount != 1 → insufficient funds or missing wallet
        if cur.rowcount != 1:
            success = False

    # 💾 Only the connection owner commits (standalone mode)
    if conn is None:
        own_conn.commit()

    cur.close()

    # 🔒 Only the connection owner closes (standalone mode)
    if conn is None:
        own_conn.close()

    return success

# 🧠 AUTO CATEGORY
def auto_category(desc):
    desc = desc.lower()
    if "food" in desc or "restaurant" in desc:
        return "Food"
    elif "uber" in desc or "bolt" in desc or "matatu" in desc:
        return "Transport"
    elif "rent" in desc:
        return "Housing"
    elif "crypto" in desc or "bitcoin" in desc:
        return "Crypto"
    elif "stock" in desc:
        return "Stocks"
    else:
        return "Other"
    
# 💱 LIVE CURRENCY CONVERSION
def convert_currency(amount, from_currency, to_currency):

    # ✅ SAME CURRENCY
    if from_currency == to_currency:
        return amount

    try:

        url = f"https://open.er-api.com/v6/latest/{from_currency}"

        response = requests.get(url, timeout=10)

        data = response.json()

        rates = data.get("rates", {})

        rate = rates.get(to_currency)

        if not rate:
            return amount

        converted = amount * rate

        return round(converted, 2)

    except Exception as e:

        print("Conversion error:", e)

        return amount

# 📈 LIVE FOREX RATES
def get_live_rates():

    pairs = [
        ("USD", "KES"),
        ("EUR", "USD"),
        ("GBP", "KES"),
        ("USD", "EUR"),
        ("KES", "USD")
    ]

    forex_data = []

    for from_currency, to_currency in pairs:

        try:

            url = f"https://open.er-api.com/v6/latest/{from_currency}"

            response = requests.get(url, timeout=10)

            data = response.json()

            rate = data["rates"].get(to_currency)

            if rate:

                forex_data.append({
                    "from": from_currency,
                    "to": to_currency,
                    "rate": round(rate, 2)
                })

        except Exception as e:

            print("Forex error:", e)

    return forex_data

# 🤖 AI INSIGHTS
def generate_insights(transactions, income, expenses, category_data):
    insights = []
    if expenses > income:
        insights.append("⚠️ Your expenses exceed your income")

    for category, amount in category_data.items():
        if expenses > 0 and amount > (expenses * 0.4):
            insights.append(f"⚠️ High spending on {category}")
    return insights

# 🧠 BUDGETING
def generate_budget(category_data, income, expenses):
    budget = {}
    tips = []

    if income == 0:
        return {}, ["⚠️ Add income"]

    for category, amount in category_data.items():
        recommended = income * 0.3
        budget[category] = {
            "spent": amount,
            "recommended": round(recommended, 2)
        }

        if amount > recommended:
            tips.append(f"⚠️ Reduce {category}")
        else:
            tips.append(f"✅ Good {category}")

    return budget, tips

# 💯 SCORE
def calculate_financial_score(income, expenses):
    if income == 0:
        return 0, "⚠️ No income"

    score = 50
    ratio = expenses / income

    if ratio < 0.5:
        score += 25
    elif ratio < 0.8:
        score += 10
    else:
        score -= 20

    savings = income - expenses
    if savings > 0:
        score += 15

    score = max(0, min(100, score))
    status = "🔥 Excellent" if score >= 80 else "👍 Good" if score >= 60 else "⚠️ Average"
    return score, status

# 🔔 CREATE NOTIFICATION
def create_notification(user_id, message):

    conn = get_db_connection()
    cur = conn.cursor()

    if DATABASE_URL:
        cur.execute(
            """
            INSERT INTO notifications
            (user_id, message)
            VALUES (%s, %s)
            """,
            (user_id, message)
        )
    else:
        cur.execute(
            """
            INSERT INTO notifications
            (user_id, message)
            VALUES (?, ?)
            """,
            (user_id, message)
        )

    conn.commit()

    cur.close()
    conn.close()

# 🎯 SAVINGS
def generate_savings_goal(income, expenses):
    savings = income - expenses
    target = income * 0.2 if income > 0 else 0
    progress = (savings / target * 100) if target > 0 else 0
    progress = min(progress, 100)

    return {
        "saved": round(savings, 2),
        "target": round(target, 2),
        "progress": round(progress, 2)
    }

# 🗄️ INIT DB
def init_db():
    conn = get_db_connection()
    cur = conn.cursor()

    if DATABASE_URL:
        # 🐘 PostgreSQL version
        cur.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE,
                email TEXT UNIQUE,
                password TEXT,
                reset_token TEXT,
                token_expiry TEXT,
                otp TEXT,
                otp_expiry TEXT
            )
        ''')

        # 🔧 PostgreSQL migration: remove deprecated users.balance column (idempotent)
        try:
            cur.execute("ALTER TABLE users DROP COLUMN IF EXISTS balance")
        except Exception:
            pass

        # ✅ ADD PREFERRED CURRENCY COLUMN SAFELY
        try:
            cur.execute(
                "ALTER TABLE users ADD COLUMN preferred_currency TEXT DEFAULT 'KES'"
            )
        except:
            pass

        cur.execute('''
            CREATE TABLE IF NOT EXISTS transactions (
                id SERIAL PRIMARY KEY,
                user_id INTEGER,
                amount REAL,
                currency TEXT DEFAULT 'KES',
                type TEXT,
                category TEXT,
                source TEXT,
                description TEXT
            )
        ''')

        cur.execute('''
            CREATE TABLE IF NOT EXISTS wallets (
                id SERIAL PRIMARY KEY,
                user_id INTEGER,
                currency TEXT,
                balance REAL DEFAULT 0
            )
        ''')

        cur.execute('''
            CREATE TABLE IF NOT EXISTS archived_transactions (
                id SERIAL PRIMARY KEY,
                user_id INTEGER,
                amount REAL,
                type TEXT,
                category TEXT,
                source TEXT,
                description TEXT
            )
        ''')

        cur.execute('''
            CREATE TABLE IF NOT EXISTS notifications (
                id SERIAL PRIMARY KEY,
                user_id INTEGER,
                message TEXT,
                is_read BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

    else:
        # 🪶 SQLite version

        # 🔧 SQLite migration: remove deprecated users.balance column (idempotent)
        cur.execute("PRAGMA table_info(users)")
        columns_info = cur.fetchall()
        if columns_info:
            existing_columns = [col[1] for col in columns_info]
            if "balance" in existing_columns:
                expected_columns = [
                    "id", "username", "password", "reset_token", "token_expiry",
                    "otp", "otp_expiry", "balance", "email", "preferred_currency"
                ]
                if existing_columns == expected_columns:
                    # Safe to migrate: rebuild users table without balance,
                    # preserving all other user data exactly
                    cur.execute("""
                        CREATE TABLE users_new (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            username TEXT UNIQUE,
                            password TEXT,
                            reset_token TEXT,
                            token_expiry TEXT,
                            otp TEXT,
                            otp_expiry TEXT,
                            email TEXT,
                            preferred_currency TEXT DEFAULT 'KES'
                        )
                    """)
                    cur.execute("""
                        INSERT INTO users_new (id, username, password, reset_token, token_expiry, otp, otp_expiry, email, preferred_currency)
                        SELECT id, username, password, reset_token, token_expiry, otp, otp_expiry, email, preferred_currency FROM users
                    """)
                    cur.execute("DROP TABLE users")
                    cur.execute("ALTER TABLE users_new RENAME TO users")
                else:
                    raise RuntimeError(
                        "users table schema is unexpected: got "
                        + str(existing_columns)
                        + "; expected "
                        + str(expected_columns)
                        + ". Aborting migration to prevent data loss."
                    )
            # else: balance already absent — no action needed
        # else: users table does not exist yet — CREATE TABLE below creates it without balance

        cur.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE,
                password TEXT,
                reset_token TEXT,
                token_expiry TEXT,
                otp TEXT,
                otp_expiry TEXT
            )
        ''')

        # ✅ ADD EMAIL COLUMN SAFELY
        try:
            cur.execute(
                "ALTER TABLE users ADD COLUMN email TEXT"
            )
        except:
            pass

        # ✅ ADD PREFERRED CURRENCY COLUMN SAFELY
        try:
            cur.execute(
                "ALTER TABLE users ADD COLUMN preferred_currency TEXT DEFAULT 'KES'"
            )
        except:
            pass

        cur.execute('''
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                currency TEXT DEFAULT 'KES',
                type TEXT,
                category TEXT,
                source TEXT,
                description TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cur.execute('''
            CREATE TABLE IF NOT EXISTS wallets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                currency TEXT,
                balance REAL DEFAULT 0
            )
        ''')

        # ✅ ADD CURRENCY COLUMN SAFELY
        try:
            cur.execute(
                "ALTER TABLE transactions ADD COLUMN currency TEXT DEFAULT 'KES'"
            )
        except:
            pass

        # ✅ ADD CREATED_AT COLUMN SAFELY
        try:
            cur.execute(
                "ALTER TABLE transactions ADD COLUMN created_at TEXT DEFAULT CURRENT_TIMESTAMP"
            )
        except:
            pass

        cur.execute('''
            CREATE TABLE IF NOT EXISTS archived_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                type TEXT,
                category TEXT,
                source TEXT,
                description TEXT,
                created_at TEXT
            )
        ''')

        cur.execute('''
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                message TEXT,
                is_read INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')

    conn.commit()
    cur.close()
    conn.close()

# 📝 REGISTER
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        email = request.form['email']
        password = request.form['password']

        if not is_strong_password(password):
            return render_template("register.html", error="Weak password")

        hashed = generate_password_hash(password)

        try:
            conn = get_db_connection()
            cur = conn.cursor()

            # 🔍 CHECK IF USERNAME OR EMAIL ALREADY EXISTS
            if DATABASE_URL:
                # PostgreSQL ✅
                cur.execute(
                    "SELECT * FROM users WHERE username=%s OR email=%s",
                    (username, email)
                )
            else:
                # SQLite ✅ FIXED
                cur.execute(
                    "SELECT * FROM users WHERE username=? OR email=?",
                    (username, email)
                )

            existing_user = cur.fetchone()

            if existing_user:
                cur.close()
                conn.close()
                return render_template("register.html", error="Username or email already exists")

            # ✅ INSERT NEW USER
            if DATABASE_URL:
                # PostgreSQL
                cur.execute(
                    "INSERT INTO users (username, email, password) VALUES (%s, %s, %s)",
                    (username, email, hashed)
                )
            else:
                # SQLite
                cur.execute(
                    "INSERT INTO users (username, email, password) VALUES (?, ?, ?)",
                    (username, email, hashed)
                )

            conn.commit()
            cur.close()
            conn.close()

            return redirect('/login')

        except Exception as e:
            print("Register error:", e)
            return render_template("register.html", error="Something went wrong")

    return render_template('register.html')

# 🔐 LOGIN
login_attempts = {}

@app.route('/login', methods=['GET', 'POST'])
def login():
    ip = request.remote_addr

    if login_attempts.get(ip, 0) >= 5:
        return "Too many attempts. Try later."

    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        conn = get_db_connection()
        cur = conn.cursor()

        if DATABASE_URL:
            cur.execute(
                "SELECT id, username, email, password FROM users WHERE username=%s",
                (username,)
            )
        else:
            cur.execute(
                "SELECT id, username, email, password FROM users WHERE username=?",
        (username,)
            )

        user = cur.fetchone()

        cur.close()
        conn.close()

        if user and check_password_hash(user[3], password):

            session.clear()

            session['user_id'] = user[0]

            # NEW
            session['username'] = user[1]

            try:
                session['email'] = user[2]
            except:
                session['email'] = "Not Available"

            session.permanent = True

            login_attempts[ip] = 0

            return redirect('/dashboard')
        else:
            login_attempts[ip] = login_attempts.get(ip, 0) + 1
            return render_template("login.html", error="Invalid login")

    return render_template('login.html')

# 💰 DEPOSIT
@app.route('/deposit', methods=['POST'])
def deposit():
    if 'user_id' not in session:
        return redirect('/login')

    amount_text = request.form.get('amount', '').strip()

    if not amount_text:
        return redirect('/dashboard')
    
    try:
        amount = float(amount_text)
    except:
        return redirect('/dashboard')

    if amount <= 0:
        return redirect('/dashboard')

    user_id = session['user_id']

    # ── financial transaction (wallet credit + ledger, ONE connection, ONE commit) ──
    conn = get_db_connection()
    cur = conn.cursor()

    try:

        # 💳 Credit the KES wallet (users.balance is NOT touched, same transaction)
        ok = update_wallet_balance(
            user_id,
            "KES",
            amount,
            "add",
            conn=conn
        )

        if not ok:
            raise RuntimeError("Deposit wallet credit failed")

        # 📝 Record deposit in transaction ledger (same connection)
        if DATABASE_URL:

            cur.execute(
                """
                INSERT INTO transactions
                (user_id, amount, currency, type, category, source, description)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    user_id,
                    amount,
                    "KES",
                    "income",
                    "Deposit",
                    "External",
                    f"Deposited {amount} KES"
                )
            )

        else:

            cur.execute(
                """
                INSERT INTO transactions
                (user_id, amount, currency, type, category, source, description)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    amount,
                    "KES",
                    "income",
                    "Deposit",
                    "External",
                    f"Deposited {amount} KES"
                )
            )

        # 💾 Commit wallet credit + ledger row together (exactly once)
        conn.commit()

    except Exception as e:

        conn.rollback()

        print("Deposit Error:", e)

        return redirect('/dashboard')

    finally:

        cur.close()
        conn.close()

    return redirect('/dashboard')

# 💸 WITHDRAW
@app.route('/withdraw', methods=['POST'])
def withdraw():
    if 'user_id' not in session:
        return redirect('/login')

    amount_text = request.form.get('amount', '').strip()

    if not amount_text:
        return redirect('/dashboard')
    try:
        amount = float(amount_text)
    except:
        return redirect('/dashboard')
    if amount <= 0:
        return redirect('/dashboard')

    conn = get_db_connection()
    cur = conn.cursor()

    try:

        # 🔍 Validate against the user's KES wallet (source of truth)
        wallet_balance = get_wallet_balance(session['user_id'], "KES", conn=conn)

        if amount > wallet_balance:
            conn.rollback()
            return redirect('/dashboard')

        # 💸 Deduct from the KES wallet (guarded, users.balance is NOT touched)
        debit_ok = update_wallet_balance(
            session['user_id'],
            "KES",
            amount,
            "subtract",
            conn=conn
        )

        if not debit_ok:
            conn.rollback()
            return redirect('/dashboard')

        # 📝 Record withdrawal in transaction ledger
        if DATABASE_URL:

            cur.execute(
                """
                INSERT INTO transactions
                (user_id, amount, currency, type, category, source, description)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    session['user_id'],
                    amount,
                    "KES",
                    "expense",
                    "Withdrawal",
                    "External",
                    f"Withdrawal of Ksh {amount}"
                )
            )

        else:

            cur.execute(
                """
                INSERT INTO transactions
                (user_id, amount, currency, type, category, source, description)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session['user_id'],
                    amount,
                    "KES",
                    "expense",
                    "Withdrawal",
                    "External",
                    f"Withdrawal of Ksh {amount}"
                )
            )

        conn.commit()

    except Exception:

        conn.rollback()

        return redirect('/dashboard')

    finally:

        cur.close()
        conn.close()

    create_notification(
        session['user_id'],
        f"💸 Withdrawal of Ksh {amount} successful"
    )

    return redirect('/dashboard?success=withdraw')

# 💸 SEND MONEY
@app.route('/send-money', methods=['POST'])
def send_money():

    if 'user_id' not in session:
        return redirect('/login')

    receiver_username = request.form.get('receiver', '').strip()
    amount_text = request.form.get('amount', '').strip()

    # ✅ VALIDATION
    if not receiver_username or not amount_text:
        return redirect('/dashboard')

    try:
        amount = float(amount_text)
    except:
        return redirect('/dashboard')

    if amount <= 0:
        return redirect('/dashboard')

    conn = get_db_connection()
    cur = conn.cursor()

    try:

        # 🔍 GET SENDER USERNAME
        if DATABASE_URL:
            cur.execute(
                "SELECT username FROM users WHERE id=%s",
                (session['user_id'],)
            )
        else:
            cur.execute(
                "SELECT username FROM users WHERE id=?",
                (session['user_id'],)
            )

        sender = cur.fetchone()

        if not sender:
            conn.rollback()
            return redirect('/dashboard')

        sender_username = sender[0]

        # 🔍 GET SENDER KES WALLET BALANCE (source of truth)
        sender_balance = get_wallet_balance(session['user_id'], "KES", conn=conn)

        # ❌ INSUFFICIENT FUNDS
        if amount > sender_balance:
            conn.rollback()
            return redirect('/dashboard')

        # 🔍 FIND RECEIVER
        if DATABASE_URL:
            cur.execute(
                "SELECT id FROM users WHERE username=%s",
                (receiver_username,)
            )
        else:
            cur.execute(
                "SELECT id FROM users WHERE username=?",
                (receiver_username,)
            )

        receiver = cur.fetchone()

        # ❌ USER NOT FOUND
        if not receiver:
            conn.rollback()
            return redirect('/dashboard')

        receiver_id = receiver[0]

        # ❌ BLOCK SELF SEND
        if receiver_id == session['user_id']:
            conn.rollback()
            return redirect('/dashboard')

        # 💸 REMOVE FROM SENDER KES WALLET (guarded, users.balance is NOT touched)
        debit_ok = update_wallet_balance(
            session['user_id'],
            "KES",
            amount,
            "subtract",
            conn=conn
        )

        if not debit_ok:
            conn.rollback()
            return redirect('/dashboard')

        # 💰 ADD TO RECEIVER KES WALLET (users.balance is NOT touched)
        update_wallet_balance(
            receiver_id,
            "KES",
            amount,
            "add",
            conn=conn
        )

        # 📝 SENDER TRANSACTION
        if DATABASE_URL:
            cur.execute(
                """
                INSERT INTO transactions
                (user_id, amount, type, category, source, description)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    session['user_id'],
                    amount,
                    "expense",
                    "Transfer",
                    "Wallet",
                    f"Sent money to {receiver_username}"
                )
            )
        else:
            cur.execute(
                """
                INSERT INTO transactions
                (user_id, amount, type, category, source, description)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session['user_id'],
                    amount,
                    "expense",
                    "Transfer",
                    "Wallet",
                    f"Sent money to {receiver_username}"
                )
            )

        # 📝 RECEIVER TRANSACTION
        if DATABASE_URL:
            cur.execute(
                """
                INSERT INTO transactions
                (user_id, amount, type, category, source, description)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    receiver_id,
                    amount,
                    "income",
                    "Transfer",
                    "Wallet",
                    f"Received money from {sender_username}"
                )
            )
        else:
            cur.execute(
                """
                INSERT INTO transactions
                (user_id, amount, type, category, source, description)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    receiver_id,
                    amount,
                    "income",
                    "Transfer",
                    "Wallet",
                    f"Received money from {sender_username}"
                )
            )

        conn.commit()

    except Exception:

        conn.rollback()

        return redirect('/dashboard')

    finally:

        cur.close()
        conn.close()

    create_notification(
        session['user_id'],
        f"💸 You sent Ksh {amount} to {receiver_username}"
    )

    create_notification(
        receiver_id,
        f"💰 You received Ksh {amount} from {sender_username}"
    )

    return redirect('/dashboard?success=sent')

# 💳 M-PESA DEPOSIT
@app.route('/mpesa', methods=['POST'])
def mpesa():

    if 'user_id' not in session:
        return redirect('/login')

    # ── validation (preserves existing parse-error → failure redirect) ──
    try:
        phone = request.form['phone']
        amount = float(request.form['amount'])
    except Exception:
        return redirect('/dashboard')

    # ✅ Simple validation
    if amount <= 0:
        return redirect('/dashboard')

    user_id = session['user_id']

    # ── financial transaction (wallet credit + ledger, ONE connection, ONE commit) ──
    conn = get_db_connection()
    try:

        ok = update_wallet_balance(
            user_id,
            "KES",
            amount,
            "add",
            conn=conn
        )
        if not ok:
            raise RuntimeError("M-Pesa wallet credit failed")

        if DATABASE_URL:
            conn.cursor().execute(
                """
                INSERT INTO transactions
                (user_id, amount, currency, type, category, source, description)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    user_id,
                    amount,
                    "KES",
                    "income",
                    "M-Pesa",
                    "M-Pesa",
                    f"M-Pesa deposit of Ksh {amount}",
                ),
            )
        else:
            conn.cursor().execute(
                """
                INSERT INTO transactions
                (user_id, amount, currency, type, category, source, description)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    amount,
                    "KES",
                    "income",
                    "M-Pesa",
                    "M-Pesa",
                    f"M-Pesa deposit of Ksh {amount}",
                ),
            )

        conn.commit()

    except Exception as e:
        conn.rollback()
        print("M-Pesa Error:", e)
        return redirect('/dashboard')
    finally:
        conn.close()

    # ── best-effort notification (after financial commit) ──
    try:
        create_notification(
            user_id,
            f"💳 M-Pesa deposit of Ksh {amount} successful"
        )
    except Exception as e:
        print("M-Pesa notification error:", e)

    return redirect('/dashboard?success=mpesa')

# 🔑 REQUEST RESET (FIXED TO USE EMAIL)
@app.route('/request-reset', methods=['GET', 'POST'])
def request_reset():
    if request.method == 'POST':
        print("EMAIL FROM FORM:", request.form)
        
        email = request.form['email']  # 🆕 GET EMAIL

        conn = get_db_connection()
        cur = conn.cursor()

        # 🔍 CHECK USER
        if DATABASE_URL:
            cur.execute("SELECT * FROM users WHERE email=%s", (email,))
        else:
            cur.execute("SELECT * FROM users WHERE email=?", (email,))

        user = cur.fetchone()

        if user:
            token = secrets.token_urlsafe(32)
            otp = str(random.randint(100000, 999999))
            expiry = datetime.now(timezone.utc) + timedelta(minutes=10)

            # 🔄 UPDATE USER (same connection, DO NOT close before this)
            if DATABASE_URL:
                cur.execute(
                    "UPDATE users SET reset_token=%s, token_expiry=%s, otp=%s, otp_expiry=%s WHERE email=%s",
                    (token, expiry.isoformat(), otp, expiry.isoformat(), email)
                )
            else:
                cur.execute(
                    "UPDATE users SET reset_token=?, token_expiry=?, otp=?, otp_expiry=? WHERE email=?",
                    (token, expiry.isoformat(), otp, expiry.isoformat(), email)
                )

            conn.commit()

            link = url_for('reset_with_token', token=token, _external=True)

            # 📧 SEND EMAIL
            message = f"""
Password Reset Request

Click the link below:
{link}

Your OTP Code:
{otp}

This will expire in 10 minutes.
"""

            email_sent = send_email(email, "Password Reset - Fintech App", message)

            if not email_sent:
                print("⚠️ Email failed, but continuing...")

        # ✅ CLOSE CONNECTION (only once, outside condition)
        cur.close()
        conn.close()

        # 🔒 ALWAYS SAME RESPONSE (SECURITY BEST PRACTICE)
        return render_template(
            "reset_request.html",
            message="If the email exists, a reset link has been sent."
        )

    return render_template("reset_request.html")

# 🔒 RESET WITH TOKEN + OTP (FULLY FIXED & SAFE)
@app.route('/reset/<token>', methods=['GET', 'POST'])
def reset_with_token(token):
    conn = get_db_connection()
    cur = conn.cursor()

    # 🔍 Find user by token
    if DATABASE_URL:
        cur.execute(
            "SELECT id, token_expiry, otp, otp_expiry FROM users WHERE reset_token=%s",
            (token,),
        )
    else:
        cur.execute(
            "SELECT id, token_expiry, otp, otp_expiry FROM users WHERE reset_token=?",
            (token,),
        )

    user = cur.fetchone()

    if not user:
        cur.close()
        conn.close()
        return "Invalid token"

    # 🧠 SAFE expiry parsing (prevents crashes)
    try:
        expiry = datetime.fromisoformat(user["token_expiry"]) if user["token_expiry"] else None
        otp_expiry = datetime.fromisoformat(user["otp_expiry"]) if user["otp_expiry"] else None
    except Exception as e:
        print("Expiry parse error:", e)
        cur.close()
        conn.close()
        return "Invalid or corrupted reset data"

    # 🚫 Validate presence
    if not expiry or not otp_expiry:
        cur.close()
        conn.close()
        return "Invalid reset data"

    # ⏰ Check expiration
    now = datetime.now(timezone.utc)

    if now > expiry:
        cur.close()
        conn.close()
        return "Reset link expired"

    if now > otp_expiry:
        cur.close()
        conn.close()
        return "OTP expired"

    # 🔁 HANDLE FORM SUBMISSION
    if request.method == 'POST':
        otp_input = request.form.get('otp', '').strip()

        # ✅ FIXED: correct OTP index
        if not otp_input or otp_input != str(user["otp"]):
            cur.close()
            conn.close()
            return "Wrong OTP"

        password = request.form['password']

        if not is_strong_password(password):
            cur.close()
            conn.close()
            return "Weak password"

        hashed = generate_password_hash(password)

        # 🔄 Update password + clear reset fields
        if DATABASE_URL:
            cur.execute(
                "UPDATE users SET password=%s, reset_token=NULL, otp=NULL, token_expiry=NULL, otp_expiry=NULL WHERE id=%s",
                (hashed, user["id"])
            )
        else:
            cur.execute(
                "UPDATE users SET password=?, reset_token=NULL, otp=NULL, token_expiry=NULL, otp_expiry=NULL WHERE id=?",
                (hashed, user["id"])
            )

        conn.commit()
        cur.close()
        conn.close()

        return redirect('/login')

    # 📄 Show reset page
    cur.close()
    conn.close()
    return render_template("reset.html")

# 📂 ARCHIVE PAGE
@app.route('/archive')
def archive():

    if 'user_id' not in session:
        return redirect('/login')

    conn = get_db_connection()

    if not DATABASE_URL:
        conn.row_factory = sqlite3.Row

    cur = conn.cursor()

    if DATABASE_URL:

        cur.execute("""
            SELECT *
            FROM archived_transactions
            WHERE user_id=%s
            ORDER BY id DESC
        """, (session['user_id'],))

    else:

        cur.execute("""
            SELECT *
            FROM archived_transactions
            WHERE user_id=?
            ORDER BY id DESC
        """, (session['user_id'],))

    archived_transactions = cur.fetchall()

    cur.close()
    conn.close()

    return render_template(
        'archive.html',
        archived_transactions=archived_transactions
    )

# ===========================
# CLEAR AI CHAT HISTORY
# ===========================
@app.route('/clear', methods=['POST'])
def clear():

    if 'user_id' not in session:
        return redirect('/login')

    user_id = session['user_id']

    conn = get_db_connection()

    # 🧵 Bound before try so finally can never hit an unbound cursor
    cur = None

    try:

        # =========================
        # CLEAR USER FINANCIAL DATA
        # =========================

        cur = conn.cursor()

        # 📝 Delete transactions
        if DATABASE_URL:

            cur.execute("""
                DELETE FROM transactions
                WHERE user_id=%s
            """, (user_id,))

        else:

            cur.execute("""
                DELETE FROM transactions
                WHERE user_id=?
            """, (user_id,))

        # 📂 Delete archived transactions
        if DATABASE_URL:

            cur.execute("""
                DELETE FROM archived_transactions
                WHERE user_id=%s
            """, (user_id,))

        else:

            cur.execute("""
                DELETE FROM archived_transactions
                WHERE user_id=?
            """, (user_id,))

        # 🔔 Delete notifications
        if DATABASE_URL:

            cur.execute("""
                DELETE FROM notifications
                WHERE user_id=%s
            """, (user_id,))

        else:

            cur.execute("""
                DELETE FROM notifications
                WHERE user_id=?
            """, (user_id,))

        # 💳 Reset wallet balances
        if DATABASE_URL:

            cur.execute("""
                UPDATE wallets
                SET balance=0
                WHERE user_id=%s
            """, (user_id,))

        else:

            cur.execute("""
                UPDATE wallets
                SET balance=0
                WHERE user_id=?
            """, (user_id,))

        # 💾 Save everything
        conn.commit()

        return redirect('/dashboard?success=cleared')

    except Exception as e:

        conn.rollback()

        print("CLEAR DATA ERROR:", e)

        return redirect('/dashboard?success=clear_error')

    finally:

        if cur is not None:
            cur.close()

        conn.close()

# 🔄 RESTORE
@app.route('/restore/<int:id>', methods=['POST'])
def restore(id):
    if 'user_id' not in session:
        return redirect('/login')

    conn = get_db_connection()
    cur = conn.cursor()

    # 🔍 GET ARCHIVED TRANSACTION
    if DATABASE_URL:
        cur.execute(
            "SELECT * FROM archived_transactions WHERE id=%s AND user_id=%s",
            (id, session['user_id'])
        )
    else:
        cur.execute(
            "SELECT * FROM archived_transactions WHERE id=? AND user_id=?",
            (id, session['user_id'])
        )

    t = cur.fetchone()

    if t:

        if DATABASE_URL:

            cur.execute(
                """
                INSERT INTO transactions
                (
                    user_id,
                    amount,
                    type,
                    category,
                    source,
                    description,
                    created_at
                )
                VALUES
                (%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    t[1],
                    t[2],
                    t[3],
                    t[4],
                    t[5],
                    t[6],
                    t[7]
                )
            )

            cur.execute(
                "DELETE FROM archived_transactions WHERE id=%s",
                (id,)
            )

        else:

            cur.execute(
                """
                INSERT INTO transactions
                (
                    user_id,
                    amount,
                    type,
                    category,
                    source,
                    description,
                    created_at
                )
                VALUES
                (?,?,?,?,?,?,?)
                """,
                (
                    t[1],
                    t[2],
                    t[3],
                    t[4],
                    t[5],
                    t[6],
                    t[7]
                )
            )

            cur.execute(
                "DELETE FROM archived_transactions WHERE id=?",
                (id,)
            )

        conn.commit()

    cur.close()
    conn.close()

    return redirect('/archive')

# 🗑️ DELETE TRANSACTION
@app.route('/delete/<int:id>', methods=['POST'])
def delete_transaction(id):

    if 'user_id' not in session:
        return redirect('/login')

    conn = get_db_connection()
    cur = conn.cursor()

    if DATABASE_URL:
        cur.execute(
            "DELETE FROM transactions WHERE id=%s AND user_id=%s",
            (id, session['user_id'])
        )
    else:
        cur.execute(
            "DELETE FROM transactions WHERE id=? AND user_id=?",
            (id, session['user_id'])
        )

    conn.commit()
    create_notification(
        session['user_id'],
        "🗑️ A transaction was deleted"
    )

    cur.close()
    conn.close()

    return redirect('/transactions?success=deleted')

# 💱 CURRENCY CONVERSION
@app.route('/convert-currency', methods=['POST'])
def convert_currency_wallet():

    if 'user_id' not in session:
        return redirect('/login')

    from_currency = request.form.get('from_currency')
    to_currency = request.form.get('to_currency')

    amount_text = request.form.get('amount', '').strip()

    if not amount_text:
        return redirect('/dashboard')

    try:
        amount = float(amount_text)
    except:
        return redirect('/dashboard')

    if amount <= 0:
        return redirect('/dashboard')

    # ❌ BLOCK SAME CURRENCY
    if from_currency == to_currency:
        return redirect('/dashboard')

    # 💱 CONVERT
    converted_amount = convert_currency(
        amount,
        from_currency,
        to_currency
    )

    conn = get_db_connection()
    cur = conn.cursor()

    try:

        # 🔍 CHECK SOURCE WALLET BALANCE

        wallet_balance = get_wallet_balance(
            session['user_id'],
            from_currency,
            conn=conn
        )

        # ❌ INSUFFICIENT BALANCE
        if amount > wallet_balance:
            conn.rollback()
            return redirect('/dashboard')

        # ➖ REMOVE FROM OLD CURRENCY (ledger)
        if DATABASE_URL:
            cur.execute(
                """
                INSERT INTO transactions
                (user_id, amount, currency, type, category, source, description)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    session['user_id'],
                    amount,
                    from_currency,
                    "expense",
                    "Conversion",
                    "Wallet",
                    f"Converted to {to_currency}"
                )
            )
        else:
            cur.execute(
                """
                INSERT INTO transactions
                (user_id, amount, currency, type, category, source, description)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    session['user_id'],
                    amount,
                    from_currency,
                    "expense",
                    "Conversion",
                    "Wallet",
                    f"Converted to {to_currency}"
                )
            )

        # ➕ ADD TO NEW CURRENCY (ledger)
        if DATABASE_URL:
            cur.execute(
                """
                INSERT INTO transactions
                (user_id, amount, currency, type, category, source, description)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    session['user_id'],
                    converted_amount,
                    to_currency,
                    "income",
                    "Conversion",
                    "Wallet",
                    f"Converted from {from_currency}"
                )
            )
        else:
            cur.execute(
                """
                INSERT INTO transactions
                (user_id, amount, currency, type, category, source, description)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    session['user_id'],
                    converted_amount,
                    to_currency,
                    "income",
                    "Conversion",
                    "Wallet",
                    f"Converted from {from_currency}"
                )
            )

        # ➖ REMOVE OLD CURRENCY (guarded, same transaction)
        debit_ok = update_wallet_balance(
            session['user_id'],
            from_currency,
            amount,
            "subtract",
            conn=conn
        )

        if not debit_ok:
            conn.rollback()
            return redirect('/dashboard')

        # ➕ ADD NEW CURRENCY (same transaction)
        update_wallet_balance(
            session['user_id'],
            to_currency,
            converted_amount,
            "add",
            conn=conn
        )

        conn.commit()

    except Exception:

        conn.rollback()

        return redirect('/dashboard')

    finally:

        cur.close()
        conn.close()

    create_notification(
        session['user_id'],
        f"💱 Converted {amount} {from_currency} → {to_currency}"
    )

    return redirect('/dashboard?success=converted')

# 🚪 LOGOUT
@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

@app.route('/')
def home():
    return redirect('/dashboard')


# 🌐 HOME
@app.route('/dashboard', methods=['GET', 'POST'])
def dashboard():
    if 'user_id' not in session:
        return redirect('/login')

    conn = get_db_connection()
    cur = conn.cursor()

    if request.method == 'POST':

        amount_text = request.form.get('amount', '').strip()

        if not amount_text:
            return redirect('/dashboard')

        try:
            amount = float(amount_text)
        except:
            return redirect('/dashboard')

        # ✅ Reject zero/negative amounts (prevents wallet manipulation)
        if amount <= 0:
            return redirect('/dashboard')

        t_type = request.form.get('type', '')

        # ✅ Only income/expense are valid transaction types
        if t_type not in ('income', 'expense'):
            return redirect('/dashboard')

        source = request.form.get('source', '')
        desc = request.form.get('description', '')
        category = request.form.get('category', '') or auto_category(desc)
        
        currency = request.form.get('currency', 'KES')

        try:

            # ➕ INSERT TRANSACTION (ledger)
            if DATABASE_URL:
                cur.execute(
                    """
                    INSERT INTO transactions
                    (user_id, amount, currency, type, category, source, description)

                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        session['user_id'],
                        amount,
                        currency,
                        t_type,
                        category,
                        source,
                        desc
                    )
                )
            else:
                cur.execute(
                    """
                    INSERT INTO transactions
                    (user_id, amount, currency, type, category, source, description)

                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session['user_id'],
                        amount,
                        currency,
                        t_type,
                        category,
                        source,
                        desc
                    )
                )

            # 📧 GET USER EMAIL
            if DATABASE_URL:
                cur.execute(
                    "SELECT email FROM users WHERE id=%s",
                    (session['user_id'],)
                )
            else:
                cur.execute(
                    "SELECT email FROM users WHERE id=?",
                    (session['user_id'],)
                )

            user_email = cur.fetchone()[0]

            # 🔔 SEND TRANSACTION NOTIFICATION
            if user_email:

                message = f"""
        Transaction Alert

        Type: {t_type}
        Amount: Ksh {amount}
        Category: {category}
        Description: {desc}

        Your transaction was recorded successfully.
        """

                send_email(
                    user_email,
                    "FinFlow Transaction Alert",
                    message
                )

            # 💳 UPDATE REAL WALLET (same transaction as the ledger INSERT)

            if t_type == "income":
                update_wallet_balance(
                    session['user_id'],
                    currency,
                    amount,
                    "add",
                    conn=conn
                )

            elif t_type == "expense":
                expense_ok = update_wallet_balance(
                    session['user_id'],
                    currency,
                    amount,
                    "subtract",
                    conn=conn
                )

                if not expense_ok:
                    conn.rollback()
                    return redirect('/dashboard')

            # 💾 Commit ledger + wallet together
            conn.commit()

        except Exception:

            conn.rollback()

            return redirect('/dashboard')

        finally:

            cur.close()
            conn.close()

        create_notification(
            session['user_id'],
            f"📝 {t_type.title()} transaction of Ksh {amount} added"
        )

        return redirect('/dashboard?success=added')

    # 🔍 FILTERS + SEARCH

    filter_type = request.args.get('filter_type', '').strip()
    # 📄 PAGINATION
    page = request.args.get('page', 1, type=int)
    per_page = 10
    offset = (page - 1) * per_page
    search = request.args.get('search', '').strip()

    query = """
    SELECT * FROM transactions
    WHERE user_id=
    """
    params = [session['user_id']]

    if DATABASE_URL:
        query += "%s"
    else:
        query += "?"

    # ✅ TYPE FILTER
    if filter_type:

        if DATABASE_URL:
            query += " AND type=%s"
        else:
            query += " AND type=?"

        params.append(filter_type)

    # ✅ SEARCH FILTER
    if search:

        search_value = f"%{search}%"

        if DATABASE_URL:
            query += """
                AND (
                    category ILIKE %s
                    OR description ILIKE %s
                )
            """
        else:
            query += """
                AND (
                    category LIKE ?
                    OR description LIKE ?
                )
            """

        params.extend([search_value, search_value])

    # 📄 ORDER + PAGINATION

    if DATABASE_URL:
        query += " ORDER BY id DESC LIMIT %s OFFSET %s"
    else:
        query += " ORDER BY id DESC LIMIT ? OFFSET ?"

    params.extend([per_page, offset])

    # 📊 EXECUTE QUERY
    cur.execute(query, tuple(params))

    transactions = cur.fetchall()
    # 📊 TOTAL COUNT FOR PAGINATION

    count_query = "SELECT COUNT(*) FROM transactions WHERE user_id="
    count_params = [session['user_id']]

    if DATABASE_URL:
        count_query += "%s"
    else:
        count_query += "?"

    if filter_type:

        if DATABASE_URL:
            count_query += " AND type=%s"
        else:
            count_query += " AND type=?"

        count_params.append(filter_type)

    if search:

        search_value = f"%{search}%"

        if DATABASE_URL:
            count_query += """
                AND (
                    category ILIKE %s
                    OR description ILIKE %s
                )
            """
        else:
            count_query += """
                AND (
                    category LIKE ?
                    OR description LIKE ?
                )
            """

        count_params.extend([search_value, search_value])

    cur.execute(count_query, tuple(count_params))

    total_transactions = cur.fetchone()[0]

    total_pages = (total_transactions + per_page - 1) // per_page
    has_next = page < total_pages
    # 🔔 GET NOTIFICATIONS
    if DATABASE_URL:
        cur.execute(
            """
            SELECT id, message, is_read
            FROM notifications
            WHERE user_id=%s
            ORDER BY id DESC
            LIMIT 8
            """,
            (session['user_id'],)
        )
    else:
        cur.execute(
            """
            SELECT id, message, is_read
            FROM notifications
            WHERE user_id=?
            ORDER BY id DESC
            LIMIT 8
            """,
            (session['user_id'],)
        )

    notifications = cur.fetchall()

    # 🔴 UNREAD COUNT
    unread_count = sum(
        1 for n in notifications
        if not n[2]
    )
    # 💰 CALCULATE TOTAL BALANCE FROM WALLETS

    if DATABASE_URL:
        cur.execute(
            """
            SELECT currency, balance
            FROM wallets
            WHERE user_id=%s
            """,
            (session['user_id'],)
        )
    else:
        cur.execute(
            """
            SELECT currency, balance
            FROM wallets
            WHERE user_id=?
            """,
            (session['user_id'],)
        )

    wallet_rows = cur.fetchall()

    real_balance = 0

    for row in wallet_rows:

        currency = row[0]
        balance = row[1]

        real_balance += convert_currency(
            balance,
            currency,
            "KES"
        )
        # 🔔 LOAD NOTIFICATIONS
    if DATABASE_URL:
        cur.execute(
            """
            SELECT id, message, is_read
            FROM notifications
            WHERE user_id=%s
            ORDER BY id DESC
            LIMIT 10
            """,
            (session['user_id'],)
        )
    else:
        cur.execute(
            """
            SELECT id, message, is_read
            FROM notifications
            WHERE user_id=?
            ORDER BY id DESC
            LIMIT 10
            """,
            (session['user_id'],)
        )

    notifications = cur.fetchall()

    # 🔴 COUNT UNREAD
    unread_count = sum(1 for n in notifications if not n[2])

    cur.close()
    conn.close()

    # 💰 CALCULATIONS
    income = sum(
        convert_currency(t[2], t[3], "KES")
        for t in transactions
        if t[4] == "income"
    )

    expenses = sum(
        convert_currency(t[2], t[3], "KES")
        for t in transactions
        if t[4] == "expense"
    )

    # ✅ USE REAL ACCOUNT BALANCE
    balance = real_balance

    category_data = {}
    for t in transactions:
        converted_amount = convert_currency(
            t[2],
            t[3],
            "KES"
        )

        category_data[t[5]] = (
            category_data.get(t[5], 0)
            + converted_amount
        )

    

    # 🌍 CONVERTED DISPLAY VALUES
    converted_transactions = []

    for t in transactions:

        converted = convert_currency(
            t[2],
            t[3],
            "KES"
        )

        converted_transactions.append({
            "id": t[0],
            "amount": t[2],
            "converted": converted,
            "currency": t[3],
            "type": t[4],
            "category": t[5],
            "source": t[6],
            "description": t[7]
        })

    # 💰 LOAD REAL WALLETS

    conn = get_db_connection()
    cur = conn.cursor()

    if DATABASE_URL:

        cur.execute(
            """
            SELECT currency, balance
            FROM wallets
            WHERE user_id=%s
            """,
            (session['user_id'],)
        )

    else:

        cur.execute(
            """
            SELECT currency, balance
            FROM wallets
            WHERE user_id=?
            """,
            (session['user_id'],)
        )

    wallet_rows = cur.fetchall()

    cur.close()
    conn.close()

    wallets = {}

    for row in wallet_rows:

        currency = row[0]
        balance = row[1]

        wallets[currency] = {
            "balance": balance,
            "converted": convert_currency(
                balance,
                currency,
                "KES"
            )
        }

    # 🌍 CONVERT EVERYTHING TO KES
    for currency in wallets:

        wallets[currency]["converted"] = convert_currency(
            wallets[currency]["balance"],
            currency,
            "KES"
        )

    insights = generate_insights(transactions, income, expenses, category_data)
    budget_data, budget_tips = generate_budget(category_data, income, expenses)
    score, score_status = calculate_financial_score(income, expenses)
    savings_goal = generate_savings_goal(income, expenses)
    forex_rates = get_live_rates()

    return render_template(
        'index.html',
        transactions=converted_transactions,
        wallets=wallets,
        forex_rates=forex_rates,
        income=income,
        expenses=expenses,
        balance=real_balance,
        category_data=category_data,
        insights=insights,
        budget_data=budget_data,
        budget_tips=budget_tips,
        score=score,
        score_status=score_status,
        savings_goal=savings_goal,
        page=page,
        total_pages=total_pages,
        has_next=has_next,
        notifications=notifications,
        unread_count=unread_count
    )

@app.route('/transactions')
def transactions():

    if 'user_id' not in session:
        return redirect('/login')

    conn = get_db_connection()

    if not DATABASE_URL:
        conn.row_factory = sqlite3.Row

    cursor = conn.cursor()

    if DATABASE_URL:
        cursor.execute("""
            SELECT *
            FROM transactions
            WHERE user_id=%s
            ORDER BY id DESC
        """, (session['user_id'],))
    else:
        cursor.execute("""
            SELECT *
            FROM transactions
            WHERE user_id=?
            ORDER BY id DESC
        """, (session['user_id'],))

    transactions = cursor.fetchall()

    conn.close()

    return render_template(
        'transactions.html',
        transactions=transactions
    )

# 📥 EXPORT TRANSACTIONS CSV
@app.route('/export-transactions')
def export_transactions():

    if 'user_id' not in session:
        return redirect('/login')

    conn = get_db_connection()

    if not DATABASE_URL:
        conn.row_factory = sqlite3.Row

    cur = conn.cursor()

    if DATABASE_URL:

        cur.execute("""
            SELECT *
            FROM transactions
            WHERE user_id=%s
            ORDER BY id DESC
        """, (session['user_id'],))

    else:

        cur.execute("""
            SELECT *
            FROM transactions
            WHERE user_id=?
            ORDER BY id DESC
        """, (session['user_id'],))

    rows = cur.fetchall()

    output = StringIO()

    writer = csv.writer(output)

    writer.writerow([
        "Amount",
        "Currency",
        "Type",
        "Category",
        "Source",
        "Description"
    ])

    for row in rows:

        writer.writerow([
            row['amount'],
            row['currency'],
            row['type'],
            row['category'],
            row['source'],
            row['description']
        ])

    conn.close()

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition":
            "attachment; filename=transactions.csv"
        }
    )


# 📊 ANALYTICS PAGE
@app.route('/analytics')
def analytics():

    if 'user_id' not in session:
        return redirect('/login')

    conn = get_db_connection()

    if not DATABASE_URL:
        conn.row_factory = sqlite3.Row

    cur = conn.cursor()

    # -----------------------------
    # GET USER TRANSACTIONS
    # -----------------------------

    if DATABASE_URL:

        cur.execute("""
            SELECT *
            FROM transactions
            WHERE user_id=%s
        """, (session['user_id'],))

    else:

        cur.execute("""
            SELECT *
            FROM transactions
            WHERE user_id=?
        """, (session['user_id'],))

    transactions = cur.fetchall()

    # -----------------------------
    # CALCULATE TOTALS
    # -----------------------------

    income = 0
    expenses = 0

    category_data = {}

    for t in transactions:

        amount = float(t['amount'])

        if t['type'] == "income":

            income += amount

        else:

            expenses += amount

            category = t['category']

            if not category:
                category = "Other"

            if category not in category_data:
                category_data[category] = 0

            category_data[category] += amount

    balance = income - expenses

    # -----------------------------
    # FINANCIAL SCORE
    # -----------------------------

    score = 100

    if expenses > income:
        score -= 30

    if balance < 0:
        score -= 20

    score = max(score, 0)

    if score >= 90:
        grade = "A+"

    elif score >= 80:
        grade = "A"

    elif score >= 70:
        grade = "B"

    elif score >= 60:
        grade = "C"

    else:
        grade = "D"

    # -----------------------------
    # SAVINGS GOAL
    # -----------------------------

    saved_amount = balance if balance > 0 else 0

    target_amount = 50000

    progress = min(
        round((saved_amount / target_amount) * 100, 1),
        100
    )

    savings_goal = {
        "saved": saved_amount,
        "target": target_amount,
        "progress": progress
    }

    # -----------------------------
    # TOP SPENDING CATEGORIES
    # -----------------------------

    top_categories = sorted(
        category_data.items(),
        key=lambda x: x[1],
        reverse=True
    )[:5]

    # -----------------------------
    # MONTHLY TRENDS
    # -----------------------------

    monthly_income = {}

    monthly_expenses = {}

    for t in transactions:

        try:

            created_at = str(t['created_at'])

            month = created_at[:7]

        except:

            month = "Unknown"

        amount = float(t['amount'])

        if t['type'] == "income":

            if month not in monthly_income:
                monthly_income[month] = 0

            monthly_income[month] += amount

        else:

            if month not in monthly_expenses:
                monthly_expenses[month] = 0

            monthly_expenses[month] += amount

    monthly_labels = sorted(
        list(
            set(
                list(monthly_income.keys()) +
                list(monthly_expenses.keys())
            )
        )
    )

    monthly_income_values = []

    monthly_expense_values = []

    for month in monthly_labels:

        monthly_income_values.append(
            monthly_income.get(month, 0)
        )

        monthly_expense_values.append(
            monthly_expenses.get(month, 0)
        )

    # -----------------------------
    # AI INSIGHTS
    # -----------------------------

    insights = []

    if expenses > income:

        insights.append(
            "⚠️ Your expenses exceed your income. Consider reducing non-essential spending."
        )

    if income > expenses:

        insights.append(
            "✅ Healthy cash flow. You are spending less than you earn."
        )

    if balance > 10000:

        insights.append(
            "💰 Excellent savings position."
        )

    if income > 0 and expenses > income * 0.8:

        insights.append(
            "📉 Your expenses are approaching your income level."
        )

    if len(category_data) > 0:

        largest_category = max(
            category_data,
            key=category_data.get
        )

        insights.append(
            f"🔥 Highest spending category: {largest_category}"
        )

    if score >= 80:

        insights.append(
            "🏆 Your financial score is strong."
        )

    if score < 50:

        insights.append(
            "⚠️ Your financial score needs improvement."
        )

    if not insights:

        insights.append(
            "📊 Keep tracking transactions to unlock deeper insights."
        )

    conn.close()

    return render_template(
        "analytics.html",
        income=income,
        expenses=expenses,
        balance=balance,
        score=score,
        grade=grade,
        category_data=category_data,
        insights=insights,
        savings_goal=savings_goal,
        top_categories=top_categories,
        monthly_labels=monthly_labels,
        monthly_income_values=monthly_income_values,
        monthly_expense_values=monthly_expense_values
    )

# 📄 EXPORT ANALYTICS PDF
@app.route('/export-analytics-pdf')
def export_analytics_pdf():

    if 'user_id' not in session:
        return redirect('/login')

    conn = get_db_connection()

    if not DATABASE_URL:
        conn.row_factory = sqlite3.Row

    cur = conn.cursor()

    # -----------------------------
    # GET USER DETAILS
    # -----------------------------

    if DATABASE_URL:

        cur.execute("""
            SELECT username, email
            FROM users
            WHERE id=%s
        """, (session['user_id'],))

    else:

        cur.execute("""
            SELECT username, email
            FROM users
            WHERE id=?
        """, (session['user_id'],))

    user = cur.fetchone()

    if user:

        if DATABASE_URL:

            user_name = user[0]
            user_email = user[1]

        else:

            user_name = user["username"]
            user_email = user["email"]

    else:

        user_name = "Unknown User"
        user_email = "Unknown Email"

    # -----------------------------
    # GET USER TRANSACTIONS
    # -----------------------------

    if DATABASE_URL:

        cur.execute("""
            SELECT *
            FROM transactions
            WHERE user_id=%s
        """, (session['user_id'],))

    else:

        cur.execute("""
            SELECT *
            FROM transactions
            WHERE user_id=?
        """, (session['user_id'],))

    transactions = cur.fetchall()

    conn.close()

    # -----------------------------
    # CALCULATE ANALYTICS
    # -----------------------------

    income = 0
    expenses = 0

    category_data = {}

    for t in transactions:

        amount = float(t['amount'])

        if t['type'] == "income":

            income += amount

        else:

            expenses += amount

            category = t['category'] or "Other"

            if category not in category_data:
                category_data[category] = 0

            category_data[category] += amount

    balance = income - expenses

    # -----------------------------
    # MONTHLY DATA
    # -----------------------------

    monthly_income = {}
    monthly_expenses = {}

    for t in transactions:

        # Month label from created_at
        from datetime import datetime

        try:

            month = datetime.strptime(
                t["created_at"][:10],
                "%Y-%m-%d"
            ).strftime("%b %Y")

        except:

            month = "Unknown"

        amount = float(t["amount"])

        if t["type"] == "income":

            monthly_income[month] = (
                monthly_income.get(month, 0)
                + amount
            )

        else:

            monthly_expenses[month] = (
                monthly_expenses.get(month, 0)
                + amount
            )

    # Get every month that appears

    months = sorted(
        set(monthly_income.keys())
        | set(monthly_expenses.keys())
    )

    income_values = [
        monthly_income.get(month, 0)
        for month in months
    ]

    expense_values = [
        monthly_expenses.get(month, 0)
        for month in months
    ]

    # -----------------------------
    # CREATE MONTHLY LINE CHART
    # -----------------------------

    if months:

        plt.figure(figsize=(7,4))

        plt.plot(
            months,
            income_values,
            color="#22c55e",
            marker="o",
            linewidth=3,
            label="Income"   
        )

        plt.plot(
            months,
            expense_values,
            color="#ef4444",
            marker="o",
            linewidth=3,
            label="Expenses"
        )

        plt.title(
            "Monthly Cash Flow Analysis",
            fontsize=16,
            fontweight="bold"
        )

        plt.xlabel("Month")

        plt.ylabel("Amount")

        plt.grid(
            linestyle="--",
            alpha=0.35
        )

        plt.legend(
            loc="upper left"
        )

        plt.xticks(
            rotation=20,
            fontsize=9
        )

        line_file = tempfile.NamedTemporaryFile(
            delete=False,
            suffix=".png"
        )

        plt.tight_layout()

        plt.savefig(
            line_file.name,
            bbox_inches="tight"
        )

        plt.close()

        story.append(
            Paragraph(
                "<b>📈 Monthly Income vs Expenses</b>",
                styles["Heading2"]
            )
        )

        story.append(
            Image(
                line_file.name,
                width=430,
                height=250
            )
        )

        story.append(
            Spacer(1,0.30*inch)
        )
    # -----------------------------
    # MONTHLY SAVINGS TABLE
    # -----------------------------

    story.append(
        Paragraph(
            "<b>💰 Monthly Net Savings</b>",
            styles["Heading2"]
        )
    )

    rows = [
        ["Month", "Net Savings"]
    ]

    for i in range(len(months)):

        net = income_values[i] - expense_values[i]

        rows.append(
            [
                months[i],
                f"KES {net:,.2f}"
            ]
        )

    table = Table(
        rows,
        colWidths=[180,180]
    )

    table.setStyle(TableStyle([

        ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#2563eb")),

        ("TEXTCOLOR",(0,0),(-1,0),colors.white),

        ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),

        ("GRID",(0,0),(-1,-1),1,colors.grey),

        ("BACKGROUND",(0,1),(-1,-1),colors.whitesmoke)

    ]))

    story.append(table)

    story.append(
        Spacer(1,0.25*inch)
    )

    # -----------------------------
    # OVERALL TREND
    # -----------------------------

    total_net = balance

    if total_net > 0:

        trend = "📈 Positive cash flow"

    elif total_net < 0:

        trend = "📉 Negative cash flow"

    else:

        trend = "➡️ Break-even"

    story.append(
        Paragraph(
            f"<b>Overall Trend:</b> {trend}",
            styles["BodyText"]
        )
    )

    story.append(
        Spacer(1,0.30*inch)
    )

    # -----------------------------
    # FINANCIAL SCORE
    # -----------------------------

    score = 100

    if expenses > income:
        score -= 30

    if balance < 0:
        score -= 20

    score = max(score, 0)

    if score >= 90:
        grade = "A+"

    elif score >= 80:
        grade = "A"

    elif score >= 70:
        grade = "B"

    elif score >= 60:
        grade = "C"

    else:
        grade = "D"

    # -----------------------------
    # SAVINGS GOAL
    # -----------------------------

    saved = max(balance, 0)

    target = 50000

    progress = min(
        round((saved / target) * 100, 1),
        100
    )

    # -----------------------------
    # AI INSIGHTS
    # -----------------------------

    insights = []

    if income > expenses:

        insights.append(
            "Healthy cash flow. You're spending less than you earn."
        )

    if expenses > income:

        insights.append(
            "Expenses exceed income. Consider reducing spending."
        )

    if balance > 10000:

        insights.append(
            "Excellent savings position."
        )

    if category_data:

        largest = max(
            category_data,
            key=category_data.get
        )

        insights.append(
            f"Highest spending category: {largest}"
        )

    if score >= 80:

        insights.append(
            "Financial score is strong."
        )

    if not insights:

        insights.append(
            "Keep tracking transactions to unlock more insights."
        )

    

    # -----------------------------
    # CREATE PDF
    # -----------------------------

    buffer = BytesIO()

    doc = SimpleDocTemplate(buffer)


    from datetime import datetime

    styles = getSampleStyleSheet()

    title_style = styles["Heading1"]
    title_style.alignment = TA_CENTER
    title_style.textColor = colors.HexColor("#22c55e")

    subtitle_style = styles["Heading2"]
    subtitle_style.alignment = TA_CENTER

    normal = styles["BodyText"]

    story = []

    # -----------------------------
    # REPORT HEADER
    # -----------------------------

    story.append(
        Paragraph(
            "<font color='#22c55e'><b>💰 FinFlow</b></font>",
            title_style
        )
    )

    story.append(
        Paragraph(
            "Personal Finance Manager",
            subtitle_style
        )
    )

    story.append(
        Spacer(1,0.10*inch)
    )

    story.append(
        Paragraph(
            "<b>FINANCIAL ANALYTICS REPORT</b>",
            styles["Heading2"]
        )
    )

    story.append(
        Spacer(1,0.20*inch)
    )

    story.append(
        Paragraph(
            f"<b>Prepared For:</b> {user_name}",
            normal
        )
    )

    story.append(
        Paragraph(
            f"<b>Account:</b> {user_email}",
            normal
        )
    )

    story.append(
        Paragraph(
            f"<b>Generated:</b> {datetime.now().strftime('%d %B %Y • %H:%M')}",
            normal
        )
    )

    story.append(
        Spacer(1,0.35*inch)
    )

    # -----------------------------
    # EXECUTIVE SUMMARY
    # -----------------------------

    story.append(
        Paragraph(
            "<b>📋 EXECUTIVE SUMMARY</b>",
            styles["Heading2"]
        )
    )

    story.append(
        Spacer(1,0.15*inch)
    )

    # Overall financial health

    if score >= 90:

        health = "Excellent ⭐⭐⭐⭐⭐"

    elif score >= 80:

        health = "Very Good ⭐⭐⭐⭐"

    elif score >= 70:

        health = "Good ⭐⭐⭐"

    elif score >= 60:

        health = "Fair ⭐⭐"

    else:

        health = "Needs Improvement ⭐"

    story.append(
        Paragraph(
            f"<b>Overall Financial Health:</b> {health}",
            normal
        )
    )

    story.append(
        Spacer(1,0.10*inch)
    )

    # Income vs Expenses

    if income > expenses:

        story.append(
            Paragraph(
                "✔ Your income exceeds your expenses.",
                normal
            )
        )

    else:

        story.append(
            Paragraph(
                "⚠ Your expenses exceed your income.",
                normal
            )
        )

    # Savings

    story.append(
        Paragraph(
            f"✔ Savings Goal Progress: {progress}%",
            normal
        )
    )

    # Largest spending category

    if category_data:

        largest_category = max(
            category_data,
            key=category_data.get
        )

        story.append(
            Paragraph(
                f"✔ Highest Spending Category: {largest_category}",
                normal
            )
        )

    story.append(
        Paragraph(
            f"✔ Financial Score: {score}/100 ({grade})",
            normal
        )
    )

    story.append(
        Spacer(1,0.15*inch)
    )

    # -----------------------------
    # FINANCIAL HEALTH GAUGE
    # -----------------------------

    story.append(
        Paragraph(
            "<b>🏆 FINANCIAL HEALTH</b>",
            styles["Heading2"]
        )
    )

    story.append(
        Spacer(1,0.15*inch)
    )

    # Choose color based on score

    if score >= 90:

        gauge_color = colors.green

    elif score >= 75:

        gauge_color = colors.HexColor("#22c55e")

    elif score >= 60:

        gauge_color = colors.orange

    else:

        gauge_color = colors.red


    gauge_data = [
        [
            f"{score}/100 ({grade})"
        ]
    ]

    gauge_table = Table(
        gauge_data,
        colWidths=[440]
    )

    gauge_table.setStyle(TableStyle([

        ("BACKGROUND",(0,0),(-1,-1),gauge_color),

        ("TEXTCOLOR",(0,0),(-1,-1),colors.white),

        ("FONTNAME",(0,0),(-1,-1),"Helvetica-Bold"),

        ("FONTSIZE",(0,0),(-1,-1),22),

        ("ALIGN",(0,0),(-1,-1),"CENTER"),

        ("VALIGN",(0,0),(-1,-1),"MIDDLE"),

        ("BOTTOMPADDING",(0,0),(-1,-1),18),

        ("TOPPADDING",(0,0),(-1,-1),18),

        ("BOX",(0,0),(-1,-1),1,colors.black)

    ]))

    story.append(gauge_table)

    story.append(
        Spacer(1,0.30*inch)
    )

    if score >= 90:

        message = (
            "Your finances are in excellent condition. "
            "Maintain your current spending and saving habits."
        )

    elif score >= 75:

        message = (
            "Your financial health is very good with only minor improvements needed."
        )

    elif score >= 60:

        message = (
            "Your finances are stable, but reducing unnecessary spending would improve your score."
        )

    else:

        message = (
            "Immediate financial improvements are recommended to strengthen your financial position."
        )

    story.append(
        Paragraph(
            message,
            normal
        )
    )

    story.append(
        Spacer(1,0.35*inch)
    )

    # -----------------------------
    # AI RECOMMENDATION
    # -----------------------------

    story.append(
        Paragraph(
            "<b>💡 AI Recommendation</b>",
            styles["Heading2"]
        )
    )

    if expenses > income:

        recommendation = (
            "Reduce discretionary spending and prioritize essential expenses "
            "until your monthly income exceeds expenses."
        )

    elif progress < 50:

        recommendation = (
            "Increase your monthly savings contribution to reach your savings goal faster."
        )

    elif category_data:

        recommendation = (
            f"Monitor spending in '{largest_category}' since it represents your largest expense category."
        )

    else:

        recommendation = (
            "Continue maintaining healthy financial habits and regularly monitor your finances."
        )

    story.append(
        Paragraph(
            recommendation,
            normal
        )
    )

    story.append(
        Spacer(1,0.35*inch)
    )

    # -----------------------------
    # SUMMARY TABLE
    # -----------------------------

    summary = [

        ["Metric","Value"],

        ["Income",f"KES {income:,.2f}"],

        ["Expenses",f"KES {expenses:,.2f}"],

        ["Balance",f"KES {balance:,.2f}"],

        ["Financial Score",f"{score}/100"],

        ["Grade",grade],

        ["Savings Progress",f"{progress}%"]

    ]

    table = Table(summary,colWidths=[220,220])

    table.setStyle(TableStyle([

        ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#1e3a8a")),

        ("TEXTCOLOR",(0,0),(-1,0),colors.white),

        ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),

        ("FONTSIZE",(0,0),(-1,0),12),

        ("BOTTOMPADDING",(0,0),(-1,0),12),

        ("GRID",(0,0),(-1,-1),1,colors.grey),

        ("BACKGROUND",(0,1),(-1,-1),colors.beige),

        ("FONTNAME",(0,1),(-1,-1),"Helvetica"),

        ("BOTTOMPADDING",(0,1),(-1,-1),8)

    ]))

    story.append(table)

    story.append(
        Spacer(1,0.35*inch)
    )

    # -----------------------------
    # SPENDING CATEGORIES
    # -----------------------------

    story.append(
        Paragraph(
            "<b>📊 Spending by Category</b>",
            styles["Heading2"]
        )
    )

    category_rows = [["Category","Amount"]]

    for category, amount in category_data.items():

        category_rows.append(
            [
                category,
                f"KES {amount:,.2f}"
            ]
        )

    category_table = Table(
        category_rows,
        colWidths=[220,220]
    )

    category_table.setStyle(TableStyle([

        ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#16a34a")),

        ("TEXTCOLOR",(0,0),(-1,0),colors.white),

        ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),

        ("GRID",(0,0),(-1,-1),1,colors.grey),

        ("BACKGROUND",(0,1),(-1,-1),colors.whitesmoke)

    ]))

    story.append(category_table)

    story.append(
        Spacer(1,0.35*inch)
    )

    # -----------------------------
    # AI INSIGHTS
    # -----------------------------

    story.append(
        Paragraph(
            "<b>🤖 AI Financial Insights</b>",
            styles["Heading2"]
        )
    )

    for insight in insights:

        story.append(
            Paragraph(
                f"• {insight}",
                styles["BodyText"]
            )
        )

    story.append(
        Spacer(1,0.4*inch)
    )

    story.append(
        Paragraph(
            "<font color='#22c55e'><b>Generated by FinFlow</b></font>",
            styles["Heading3"]
        )
    )

    story.append(
        Paragraph(
            "This report is automatically generated from your financial records.",
            styles["BodyText"]
        )
    )

    story.append(
        Paragraph(
            "© 2026 FinFlow Personal Finance Manager",
            styles["BodyText"]
        )
    )

    # -----------------------------
    # CREATE CATEGORY PIE CHART
    # -----------------------------

    if category_data:

        plt.figure(figsize=(5,5))

        plt.pie(
            category_data.values(),
            labels=category_data.keys(),
            autopct="%1.1f%%",
            startangle=90
        )

        plt.title("Spending by Category")

        pie_file = tempfile.NamedTemporaryFile(
            delete=False,
            suffix=".png"
        )

        plt.savefig(
            pie_file.name,
            bbox_inches="tight"
        )

        plt.close()

        story.append(
            Paragraph(
                "<b>📊 Spending by Category</b>",
                styles["Heading2"]
            )
        )

        story.append(
            Image(
                pie_file.name,
                width=300,
                height=300
            )
        )

        story.append(
            Spacer(1,0.3*inch)
        )

    doc.build(story)

    # Remove temporary chart

    if category_data:

        os.remove(pie_file.name)
    
    if months:

        os.remove(line_file.name)

    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name="FinFlow_Analytics_Report.pdf",
        mimetype="application/pdf"
    )

# 📄 EXPORT ANALYTICS PDF
@app.route('/export-analytics')
def export_analytics():

    if 'user_id' not in session:
        return redirect('/login')

    filename = "analytics_report.pdf"

    pdf = SimpleDocTemplate(filename)

    styles = getSampleStyleSheet()

    content = []

    content.append(
        Paragraph(
            "FinFlow Analytics Report",
            styles['Title']
        )
    )

    content.append(Spacer(1, 12))

    content.append(
        Paragraph(
            f"Generated for User ID: {session['user_id']}",
            styles['Normal']
        )
    )

    pdf.build(content)

    return send_file(
        filename,
        as_attachment=True
    )

# 💳 WALLET PAGE
@app.route('/wallet')
def wallet():

    if 'user_id' not in session:
        return redirect('/login')

    conn = get_db_connection()

    if not DATABASE_URL:
        conn.row_factory = sqlite3.Row

    cur = conn.cursor()

    if DATABASE_URL:

        cur.execute("""
            SELECT *
            FROM wallets
            WHERE user_id=%s
        """, (session['user_id'],))

    else:

        cur.execute("""
            SELECT *
            FROM wallets
            WHERE user_id=?
        """, (session['user_id'],))

    wallets = cur.fetchall()

    cur.close()
    conn.close()

    return render_template(
        "wallet.html",
        wallets=wallets
    )

# ➕ ADD NEW WALLET
@app.route('/add-wallet/<currency>')
def add_wallet(currency):

    if 'user_id' not in session:
        return redirect('/login')

    currency = currency.upper()

    if currency not in ['KES', 'USD', 'EUR', 'GBP']:
        return redirect('/wallet')

    get_wallet_balance(
        session['user_id'],
        currency
    )

    return redirect('/wallet')

# 🔄 TRANSFER BETWEEN WALLETS
@app.route('/transfer-wallet', methods=['POST'])
def transfer_wallet():

    if 'user_id' not in session:
        return redirect('/login')

    from_currency = request.form['from_currency']
    to_currency = request.form['to_currency']

    amount = float(request.form['amount'])

    # Prevent zero or negative transfers
    if amount <= 0:

        flash(
            "Amount must be greater than zero",
            "error"
        )

        return redirect('/wallet')

    # Prevent same-wallet transfer
    if from_currency == to_currency:

        flash(
            "Cannot transfer to the same wallet",
            "error"
        )

        return redirect('/wallet')

    conn = get_db_connection()
    cur = conn.cursor()

    try:

        # Check source wallet balance
        source_balance = get_wallet_balance(
            session['user_id'],
            from_currency,
            conn=conn
        )

        # Prevent insufficient funds
        if source_balance < amount:

            conn.rollback()

            flash(
                "Insufficient funds",
                "error"
            )

            return redirect('/wallet')

        # Remove from source wallet (guarded)
        debit_ok = update_wallet_balance(
            session['user_id'],
            from_currency,
            amount,
            "subtract",
            conn=conn
        )

        if not debit_ok:

            conn.rollback()

            flash(
                "Insufficient funds",
                "error"
            )

            return redirect('/wallet')

        # Add to destination wallet
        update_wallet_balance(
            session['user_id'],
            to_currency,
            amount,
            "add",
            conn=conn
        )

        # 📝 Record transfer in transaction ledger (expense leg)
        if DATABASE_URL:

            cur.execute(
                """
                INSERT INTO transactions
                (user_id, amount, currency, type, category, source, description)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    session['user_id'],
                    amount,
                    from_currency,
                    "expense",
                    "Transfer",
                    "Wallet",
                    f"Transferred {amount} {from_currency} to {to_currency}"
                )
            )

        else:

            cur.execute(
                """
                INSERT INTO transactions
                (user_id, amount, currency, type, category, source, description)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session['user_id'],
                    amount,
                    from_currency,
                    "expense",
                    "Transfer",
                    "Wallet",
                    f"Transferred {amount} {from_currency} to {to_currency}"
                )
            )

        # 📝 Record transfer in transaction ledger (income leg)
        if DATABASE_URL:

            cur.execute(
                """
                INSERT INTO transactions
                (user_id, amount, currency, type, category, source, description)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    session['user_id'],
                    amount,
                    to_currency,
                    "income",
                    "Transfer",
                    "Wallet",
                    f"Received {amount} {to_currency} from {from_currency}"
                )
            )

        else:

            cur.execute(
                """
                INSERT INTO transactions
                (user_id, amount, currency, type, category, source, description)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session['user_id'],
                    amount,
                    to_currency,
                    "income",
                    "Transfer",
                    "Wallet",
                    f"Received {amount} {to_currency} from {from_currency}"
                )
            )

        conn.commit()

    except Exception:

        conn.rollback()

        return redirect('/wallet')

    finally:

        cur.close()
        conn.close()

    flash(
        f"Transferred {amount} {from_currency} to {to_currency}",
        "success"
    )

    return redirect('/wallet')

# 🤖 AI ASSISTANT PAGE
@app.route('/ai-assistant')
def ai_assistant():

    if 'user_id' not in session:
        return redirect('/login')

    return render_template('ai-assistant.html')

# 🤖 AI CHAT
@app.route('/chat', methods=['POST'])
def chat():

    if 'user_id' not in session:
        return "Please login first."

    message = request.form.get("message", "").lower()

    conn = get_db_connection()

    if not DATABASE_URL:
        conn.row_factory = sqlite3.Row

    cur = conn.cursor()

    # GET USER TRANSACTIONS

    if DATABASE_URL:

        cur.execute("""
            SELECT *
            FROM transactions
            WHERE user_id=%s
        """, (session['user_id'],))

    else:

        cur.execute("""
            SELECT *
            FROM transactions
            WHERE user_id=?
        """, (session['user_id'],))

    transactions = cur.fetchall()

    income = 0
    expenses = 0

    category_data = {}

    for t in transactions:

        amount = float(t['amount'])

        if t['type'] == "income":
            income += amount

        else:
            expenses += amount

            category = t['category']

            if category not in category_data:
                category_data[category] = 0

            category_data[category] += amount

    balance = income - expenses

    cur.close()
    conn.close()

    # AI RESPONSES

    if "save" in message:

        savings_rate = 0

        if income > 0:
            savings_rate = round((balance / income) * 100, 1)

        response = (
            f"💰 You have saved KES {balance:,.2f}. "
            f"Your savings rate is {savings_rate}%."
        )

    elif "budget" in message:

        response = (
            f"📊 Income: KES {income:,.2f}\n"
            f"📉 Expenses: KES {expenses:,.2f}\n"
            f"💰 Balance: KES {balance:,.2f}\n\n"
            f"Try keeping expenses below 80% of income."
        )

    elif "invest" in message:

        if balance > 0:

            response = (
                f"📈 You currently have KES {balance:,.2f} available.\n\n"
                f"Consider building an emergency fund first, "
                f"then explore diversified investments."
            )

        else:

            response = (
                "⚠️ Focus on increasing savings before investing."
            )

    elif "debt" in message:

        response = (
            "⚠️ Prioritize paying high-interest debt first before making major investments."
        )

    elif (
        "finance" in message
        or "finances" in message
        or "money" in message
    ):

        if income > expenses:

            response = (
                f"📊 Financial Health Report\n\n"
                f"Income: KES {income:,.2f}\n"
                f"Expenses: KES {expenses:,.2f}\n"
                f"Balance: KES {balance:,.2f}\n\n"
                f"✅ You're spending less than you earn."
            )

        else:

            response = (
                f"📊 Financial Health Report\n\n"
                f"Income: KES {income:,.2f}\n"
                f"Expenses: KES {expenses:,.2f}\n"
                f"Balance: KES {balance:,.2f}\n\n"
                f"⚠️ Your expenses are exceeding your income."
            )

    elif (
        "spending" in message
        or "expenses" in message
    ):

        if category_data:

            top_category = max(
                category_data,
                key=category_data.get
            )

            response = (
                f"📉 Total Expenses: KES {expenses:,.2f}\n\n"
                f"🔥 Highest spending category:\n"
                f"{top_category} "
                f"(KES {category_data[top_category]:,.2f})"
            )

        else:

            response = "No spending data available."

    else:

        response = (
            "🤖 I can help with:\n\n"
            "• Budgeting\n"
            "• Saving\n"
            "• Investing\n"
            "• Spending Analysis\n"
            "• Financial Health Reports\n"
            "• Debt Management\n\n"
            "Try asking:\n"
            "'What do you think of my finances?'"
        )

    return response

# ⚙️ SETTINGS PAGE
@app.route('/settings')
def settings():

    if 'user_id' not in session:
        return redirect('/login')

    conn = get_db_connection()

    if not DATABASE_URL:
        conn.row_factory = sqlite3.Row

    cur = conn.cursor()

    if DATABASE_URL:

        cur.execute("""
            SELECT preferred_currency
            FROM users
            WHERE id=%s
        """, (session['user_id'],))

    else:

        cur.execute("""
            SELECT preferred_currency
            FROM users
            WHERE id=?
        """, (session['user_id'],))

    user = cur.fetchone()

    preferred_currency = "KES"

    if user:
        try:
            preferred_currency = user['preferred_currency']
        except:
            preferred_currency = user[0]

    cur.close()
    conn.close()

    return render_template(
        'settings.html',
        preferred_currency=preferred_currency
    )

# 🔒 CHANGE PASSWORD
@app.route('/change-password', methods=['GET', 'POST'])
def change_password():

    if 'user_id' not in session:
        return redirect('/login')
    
    if request.method == 'GET':
        return render_template('change_password.html')

    current_password = request.form['current_password']
    new_password = request.form['new_password']

    confirm_password = request.form['confirm_password']

    if new_password != confirm_password:
        return redirect('/change-password')

    conn = get_db_connection()

    if not DATABASE_URL:
        conn.row_factory = sqlite3.Row

    cur = conn.cursor()

    # GET USER
    if DATABASE_URL:

        cur.execute(
            "SELECT * FROM users WHERE id=%s",
            (session['user_id'],)
        )

    else:

        cur.execute(
            "SELECT * FROM users WHERE id=?",
            (session['user_id'],)
        )

    user = cur.fetchone()

    if not user:
        conn.close()
        return redirect('/settings')

    # PASSWORD COLUMN
    stored_password = user['password'] if not DATABASE_URL else user[3]

    if not check_password_hash(
        stored_password,
        current_password
    ):
        conn.close()
        return redirect('/settings?error=wrongpassword')

    new_hash = generate_password_hash(new_password)

    if DATABASE_URL:

        cur.execute(
            "UPDATE users SET password=%s WHERE id=%s",
            (new_hash, session['user_id'])
        )

    else:

        cur.execute(
            "UPDATE users SET password=? WHERE id=?",
            (new_hash, session['user_id'])
        )

    conn.commit()

    cur.close()
    conn.close()

    return redirect('/settings?success=passwordchanged')

# 💱 UPDATE CURRENCY
@app.route('/update-currency', methods=['POST'])
def update_currency():

    if 'user_id' not in session:
        return redirect('/login')

    currency = request.form.get('currency')

    conn = get_db_connection()
    cur = conn.cursor()

    if DATABASE_URL:

        cur.execute("""
            UPDATE users
            SET preferred_currency=%s
            WHERE id=%s
        """, (currency, session['user_id']))

    else:

        cur.execute("""
            UPDATE users
            SET preferred_currency=?
            WHERE id=?
        """, (currency, session['user_id']))

    conn.commit()

    cur.close()
    conn.close()

    return redirect('/settings')

# 💾 SAVE SETTINGS
@app.route('/save-settings', methods=['POST'])
def save_settings():

    if 'user_id' not in session:
        return redirect('/login')

    currency = request.form.get('currency')

    conn = get_db_connection()
    cur = conn.cursor()

    if DATABASE_URL:

        cur.execute("""
            UPDATE users
            SET preferred_currency=%s
            WHERE id=%s
        """, (currency, session['user_id']))

    else:

        cur.execute("""
            UPDATE users
            SET preferred_currency=?
            WHERE id=?
        """, (currency, session['user_id']))

    conn.commit()

    cur.close()
    conn.close()

    return redirect('/settings')

# ✅ MARK NOTIFICATIONS AS READ
@app.route('/read-notifications', methods=['POST'])
def read_notifications():

    if 'user_id' not in session:
        return '', 401

    conn = get_db_connection()
    cur = conn.cursor()

    if DATABASE_URL:
        cur.execute(
            """
            UPDATE notifications
            SET is_read=TRUE
            WHERE user_id=%s
            """,
            (session['user_id'],)
        )
    else:
        cur.execute(
            """
            UPDATE notifications
            SET is_read=1
            WHERE user_id=?
            """,
            (session['user_id'],)
        )

    conn.commit()

    cur.close()
    conn.close()

    return '', 204

# 🔥 RUN DB INIT ON STARTUP (ALWAYS RUNS)
init_db()


# ▶️ RUN (LOCAL ONLY)
if __name__ == "__main__":
    app.run(debug=False)