from flask import Flask, render_template, request, redirect, session, url_for, abort
import sqlite3
import os
import secrets
import random
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone
from werkzeug.security import generate_password_hash, check_password_hash

# 🐘 PostgreSQL
import psycopg2
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
            port=url.port
        )
        return conn
    else:
        # 🪶 SQLite (local)
        return sqlite3.connect(
            os.path.join(os.path.dirname(__file__), 'database.db')
        )
    
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
                otp_expiry TEXT,
                balance REAL DEFAULT 0
            )
        ''')

        cur.execute('''
            CREATE TABLE IF NOT EXISTS transactions (
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

    else:
        # 🪶 SQLite version
        cur.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE,
                password TEXT,
                reset_token TEXT,
                token_expiry TEXT,
                otp TEXT,
                otp_expiry TEXT,
                balance REAL DEFAULT 0
            )
        ''')

        # ✅ ADD EMAIL COLUMN SAFELY
        try:
            cur.execute("ALTER TABLE users ADD COLUMN email TEXT")
        except:
            pass

        cur.execute('''
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                type TEXT,
                category TEXT,
                source TEXT,
                description TEXT
            )
        ''')

        cur.execute('''
            CREATE TABLE IF NOT EXISTS archived_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                type TEXT,
                category TEXT,
                source TEXT,
                description TEXT
            )
        ''')

    conn.commit()
    cur.close()
    conn.close()

# 🔥 RUN IT ON STARTUP (CRITICAL FOR :contentReference[oaicite:0]{index=0})
init_db()

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
            # PostgreSQL ✅
            cur.execute("SELECT * FROM users WHERE username=%s", (username,))
        else:
            # SQLite ✅ FIXED
            cur.execute("SELECT * FROM users WHERE username=?", (username,))

        user = cur.fetchone()

        cur.close()
        conn.close()

        if user and check_password_hash(user[3], password):
            session.clear()
            session['user_id'] = user[0]
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

    amount = float(request.form['amount'])

    if amount <= 0:
        return redirect('/dashboard')

    conn = get_db_connection()
    cur = conn.cursor()

    if DATABASE_URL:
        cur.execute(
            "UPDATE users SET balance = balance + %s WHERE id=%s",
            (amount, session['user_id'])
        )
    else:
        cur.execute(
            "UPDATE users SET balance = balance + ? WHERE id=?",
            (amount, session['user_id'])
        )

    conn.commit()
    cur.close()
    conn.close()

    return redirect('/dashboard')

# 💸 WITHDRAW
@app.route('/withdraw', methods=['POST'])
def withdraw():
    if 'user_id' not in session:
        return redirect('/login')

    amount = float(request.form['amount'])

    conn = get_db_connection()
    cur = conn.cursor()

    # 🔍 Get current balance
    if DATABASE_URL:
        cur.execute("SELECT balance FROM users WHERE id=%s", (session['user_id'],))
    else:
        cur.execute("SELECT balance FROM users WHERE id=?", (session['user_id'],))

    balance = cur.fetchone()[0]

    if amount <= 0 or amount > balance:
        cur.close()
        conn.close()
        return redirect('/dashboard')

    # 💸 Deduct
    if DATABASE_URL:
        cur.execute(
            "UPDATE users SET balance = balance - %s WHERE id=%s",
            (amount, session['user_id'])
        )
    else:
        cur.execute(
            "UPDATE users SET balance = balance - ? WHERE id=?",
            (amount, session['user_id'])
        )

    conn.commit()
    cur.close()
    conn.close()

    return redirect('/dashboard')

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
        cur.execute("SELECT * FROM users WHERE reset_token=%s", (token,))
    else:
        cur.execute("SELECT * FROM users WHERE reset_token=?", (token,))

    user = cur.fetchone()

    if not user:
        cur.close()
        conn.close()
        return "Invalid token"

    # 🧠 SAFE expiry parsing (prevents crashes)
    try:
        expiry = datetime.fromisoformat(user[5]) if user[5] else None
        otp_expiry = datetime.fromisoformat(user[7]) if user[7] else None
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
        if not otp_input or otp_input != str(user[6]):
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
                (hashed, user[0])
            )
        else:
            cur.execute(
                "UPDATE users SET password=?, reset_token=NULL, otp=NULL, token_expiry=NULL, otp_expiry=NULL WHERE id=?",
                (hashed, user[0])
            )

        conn.commit()
        cur.close()
        conn.close()

        return redirect('/login')

    # 📄 Show reset page
    cur.close()
    conn.close()
    return render_template("reset.html")

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
        # ➕ RESTORE TO MAIN TABLE
        if DATABASE_URL:
            cur.execute(
                "INSERT INTO transactions (user_id, amount, type, category, source, description) VALUES (%s, %s, %s, %s, %s, %s)",
                (t[1], t[2], t[3], t[4], t[5], t[6])
            )
            cur.execute(
                "DELETE FROM archived_transactions WHERE id=%s",
                (id,)
            )
        else:
            cur.execute(
                "INSERT INTO transactions (user_id, amount, type, category, source, description) VALUES (?, ?, ?, ?, ?, ?)",
                (t[1], t[2], t[3], t[4], t[5], t[6])
            )
            cur.execute(
                "DELETE FROM archived_transactions WHERE id=?",
                (id,)
            )

        conn.commit()

    cur.close()
    conn.close()

    return redirect('/archive')

# 🚪 LOGOUT
@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

# 🌐 HOME
@app.route('/dashboard', methods=['GET', 'POST'])
def dashboard():
    if 'user_id' not in session:
        return redirect('/login')

    conn = get_db_connection()
    cur = conn.cursor()

    if request.method == 'POST':
        amount = float(request.form['amount'])
        t_type = request.form['type']
        source = request.form['source']
        desc = request.form['description']
        category = request.form['category'] or auto_category(desc)

        # ➕ INSERT TRANSACTION
        if DATABASE_URL:
            cur.execute(
                "INSERT INTO transactions (user_id, amount, type, category, source, description) VALUES (%s, %s, %s, %s, %s, %s)",
                (session['user_id'], amount, t_type, category, source, desc)
            )
        else:
            cur.execute(
                "INSERT INTO transactions (user_id, amount, type, category, source, description) VALUES (?, ?, ?, ?, ?, ?)",
                (session['user_id'], amount, t_type, category, source, desc)
            )

        conn.commit()

    # 📊 FETCH TRANSACTIONS
    if DATABASE_URL:
        cur.execute(
            "SELECT * FROM transactions WHERE user_id=%s",
            (session['user_id'],)
        )
    else:
        cur.execute(
            "SELECT * FROM transactions WHERE user_id=?",
            (session['user_id'],)
        )

    transactions = cur.fetchall()

    cur.close()
    conn.close()

    # 💰 CALCULATIONS (UNCHANGED)
    income = sum(t[2] for t in transactions if t[3] == "income")
    expenses = sum(t[2] for t in transactions if t[3] == "expense")
    balance = income - expenses

    category_data = {}
    for t in transactions:
        category_data[t[4]] = category_data.get(t[4], 0) + t[2]

    insights = generate_insights(transactions, income, expenses, category_data)
    budget_data, budget_tips = generate_budget(category_data, income, expenses)
    score, score_status = calculate_financial_score(income, expenses)
    savings_goal = generate_savings_goal(income, expenses)

    return render_template(
        'index.html',
        transactions=transactions,
        income=income,
        expenses=expenses,
        balance=balance,
        category_data=category_data,
        insights=insights,
        budget_data=budget_data,
        budget_tips=budget_tips,
        score=score,
        score_status=score_status,
        savings_goal=savings_goal
    )

# 🔥 RUN DB INIT ON STARTUP (ALWAYS RUNS)
init_db()


# ▶️ RUN (LOCAL ONLY)
if __name__ == "__main__":
    app.run(debug=False)