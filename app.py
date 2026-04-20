from flask import Flask, render_template, request, redirect, session, url_for, abort
import sqlite3
import os
import secrets
import random
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash

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

# 🔐 EMAIL CONFIG (NEW)
EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")

# 📧 SEND EMAIL FUNCTION (NEW)
def send_email(to_email, subject, message):
    msg = MIMEText(message)
    msg['Subject'] = subject
    msg['From'] = EMAIL_ADDRESS
    msg['To'] = to_email

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
            server.send_message(msg)
    except Exception as e:
        print("Email error:", e)

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
    conn = sqlite3.connect(db_path)

    # 👤 USERS TABLE
    conn.execute('''
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

    # ✅ ADD EMAIL COLUMN SAFELY (WILL NOT BREAK EXISTING DB)
    try:
        conn.execute("ALTER TABLE users ADD COLUMN email TEXT")
    except:
        pass

    # 💰 TRANSACTIONS TABLE
    conn.execute('''
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

    # 📂 ARCHIVED TRANSACTIONS
    conn.execute('''
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

    conn.commit()  # ✅ IMPORTANT (ensures changes are saved)
    conn.close()

# 📝 REGISTER
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        email = request.form['email']  # 🆕 GET EMAIL
        password = request.form['password']

        if not is_strong_password(password):
            return render_template("register.html", error="Weak password")

        hashed = generate_password_hash(password)

        try:
            conn = sqlite3.connect(db_path)
            conn.execute(
                "INSERT INTO users (username, email, password) VALUES (?, ?, ?)",
                (username, email, hashed)
            )
            conn.commit()
            conn.close()
            return redirect('/login')
        except:
            return render_template("register.html", error="Username exists")

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

        conn = sqlite3.connect(db_path)
        user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        conn.close()

        if user and check_password_hash(user[2], password):
            session.clear()
            session['user_id'] = user[0]
            session.permanent = True
            login_attempts[ip] = 0
            return redirect('/')
        else:
            login_attempts[ip] = login_attempts.get(ip, 0) + 1
            return render_template("login.html", error="Invalid login")

    return render_template('login.html')

# 🔑 REQUEST RESET (UPDATED WITH EMAIL)
@app.route('/request-reset', methods=['GET', 'POST'])
def request_reset():
    if request.method == 'POST':
        username = request.form['username']

        conn = sqlite3.connect(db_path)
        user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()

        if user:
            token = secrets.token_urlsafe(32)
            otp = str(random.randint(100000, 999999))
            expiry = datetime.utcnow() + timedelta(minutes=10)

            conn.execute(
                "UPDATE users SET reset_token=?, token_expiry=?, otp=?, otp_expiry=? WHERE username=?",
                (token, expiry.isoformat(), otp, expiry.isoformat(), username)
            )
            conn.commit()
            conn.close()

            link = url_for('reset_with_token', token=token, _external=True)

            # 📧 SEND EMAIL INSTEAD OF SHOWING
            message = f"""
Password Reset Request

Click the link below:
{link}

Your OTP Code:
{otp}

This will expire in 10 minutes.
"""

            send_email(username, "Password Reset - Fintech App", message)

            return "Reset link and OTP sent to your email."

        conn.close()
        return "User not found"

    return render_template("reset_request.html")

# 🔒 RESET WITH TOKEN + OTP (UNCHANGED - ALREADY SECURE)
@app.route('/reset/<token>', methods=['GET', 'POST'])
def reset_with_token(token):
    conn = sqlite3.connect(db_path)
    user = conn.execute("SELECT * FROM users WHERE reset_token=?", (token,)).fetchone()

    if not user:
        conn.close()
        return "Invalid token"

    expiry = datetime.fromisoformat(user[4])
    otp_expiry = datetime.fromisoformat(user[6])

    if datetime.utcnow() > expiry or datetime.utcnow() > otp_expiry:
        conn.close()
        return "Expired"

    if request.method == 'POST':
        otp_input = request.form.get('otp', '').strip()

        if not otp_input or otp_input != user[5]:
            conn.close()
            return "Wrong OTP"

        password = request.form['password']

        if not is_strong_password(password):
            conn.close()
            return "Weak password"

        hashed = generate_password_hash(password)

        conn.execute(
            "UPDATE users SET password=?, reset_token=NULL, otp=NULL, token_expiry=NULL, otp_expiry=NULL WHERE id=?",
            (hashed, user[0])
        )
        conn.commit()
        conn.close()

        return redirect('/login')

    conn.close()
    return render_template("reset.html")

# 🔄 RESTORE
@app.route('/restore/<int:id>', methods=['POST'])
def restore(id):
    if 'user_id' not in session:
        return redirect('/login')

    conn = sqlite3.connect(db_path)

    t = conn.execute(
        "SELECT * FROM archived_transactions WHERE id=? AND user_id=?",
        (id, session['user_id'])
    ).fetchone()

    if t:
        conn.execute(
            "INSERT INTO transactions (user_id, amount, type, category, source, description) VALUES (?, ?, ?, ?, ?, ?)",
            (t[1], t[2], t[3], t[4], t[5], t[6])
        )
        conn.execute("DELETE FROM archived_transactions WHERE id=?", (id,))
        conn.commit()

    conn.close()
    return redirect('/archive')

# 🚪 LOGOUT
@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

# 🌐 HOME
@app.route('/', methods=['GET', 'POST'])
def index():
    if 'user_id' not in session:
        return redirect('/login')

    conn = sqlite3.connect(db_path)

    if request.method == 'POST':
        amount = float(request.form['amount'])
        t_type = request.form['type']
        source = request.form['source']
        desc = request.form['description']
        category = request.form['category'] or auto_category(desc)

        conn.execute(
            "INSERT INTO transactions (user_id, amount, type, category, source, description) VALUES (?, ?, ?, ?, ?, ?)",
            (session['user_id'], amount, t_type, category, source, desc)
        )
        conn.commit()

    transactions = conn.execute("SELECT * FROM transactions WHERE user_id=?", (session['user_id'],)).fetchall()
    conn.close()

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

# ▶️ RUN
if __name__ == "__main__":
    init_db()
    app.run(debug=True)