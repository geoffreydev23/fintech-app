from flask import Flask, render_template, request, redirect, session, url_for
import sqlite3
import os
import secrets
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash

# ✅ SAFE OPENAI IMPORT
try:
    from openai import OpenAI
except:
    OpenAI = None

app = Flask(__name__)
app.secret_key = "secret123"

# 🔐 SAFE API KEY HANDLING
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if OPENAI_API_KEY and OpenAI:
    client = OpenAI(api_key=OPENAI_API_KEY)
else:
    client = None

# 📁 DATABASE PATH
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
db_path = os.path.join(BASE_DIR, 'database.db')

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

# 🧠 SMART AI BUDGETING
def generate_budget(category_data, income, expenses):
    budget = {}
    tips = []

    if income == 0:
        return {}, ["⚠️ Add income to generate a budget"]

    spending_ratio = expenses / income if income > 0 else 0

    for category, amount in category_data.items():

        if spending_ratio > 0.8:
            recommended_pct = 0.15
        elif spending_ratio > 0.5:
            recommended_pct = 0.25
        else:
            recommended_pct = 0.35

        recommended = income * recommended_pct

        budget[category] = {
            "spent": amount,
            "recommended": round(recommended, 2)
        }

        if amount > recommended:
            tips.append(f"⚠️ Reduce {category} spending.")
        elif amount < (recommended * 0.5):
            tips.append(f"💡 You can spend more on {category}.")
        else:
            tips.append(f"✅ Good balance in {category}.")

    return budget, tips

# 💯 FINANCIAL SCORE
def calculate_financial_score(income, expenses):
    if income == 0:
        return 0, "⚠️ No income data"

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
    else:
        score -= 15

    score = max(0, min(100, score))

    return score, "🔥 Excellent" if score >= 80 else "👍 Good" if score >= 60 else "⚠️ Average"

# 🎯 SAVINGS GOAL
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

# 🗄️ INIT DB (UPDATED)
def init_db():
    conn = sqlite3.connect(db_path)

    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password TEXT,
            reset_token TEXT,
            token_expiry TEXT
        )
    ''')

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

    conn.close()

# 📝 REGISTER
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = generate_password_hash(request.form['password'])

        try:
            conn = sqlite3.connect(db_path)
            conn.execute(
                "INSERT INTO users (username, password) VALUES (?, ?)",
                (username, password)
            )
            conn.commit()
            conn.close()

            return redirect('/login')

        except:
            return render_template("register.html", error="Username already exists")

    return render_template('register.html')

# 🔐 LOGIN
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        conn = sqlite3.connect(db_path)
        user = conn.execute(
            "SELECT * FROM users WHERE username=?",
            (username,)
        ).fetchone()
        conn.close()

        if user and check_password_hash(user[2], password):
            session['user_id'] = user[0]
            return redirect('/')
        else:
            return render_template("login.html", error="Invalid username or password")

    return render_template('login.html')

# 🔑 REQUEST RESET (STEP 3)
@app.route('/request-reset', methods=['GET', 'POST'])
def request_reset():
    if request.method == 'POST':
        username = request.form['username']

        conn = sqlite3.connect(db_path)
        user = conn.execute(
            "SELECT * FROM users WHERE username=?",
            (username,)
        ).fetchone()

        if user:
            token = secrets.token_urlsafe(32)
            expiry = datetime.utcnow() + timedelta(minutes=15)

            conn.execute(
                "UPDATE users SET reset_token=?, token_expiry=? WHERE username=?",
                (token, expiry.isoformat(), username)
            )
            conn.commit()
            conn.close()

            # 🔗 Simulated reset link (normally email)
            reset_link = url_for('reset_with_token', token=token, _external=True)

            return f"Reset link: {reset_link}"

        conn.close()
        return render_template("reset_request.html", error="User not found")

    return render_template("reset_request.html")

# 🔒 SECURE RESET WITH TOKEN (STEP 4)
@app.route('/reset/<token>', methods=['GET', 'POST'])
def reset_with_token(token):
    conn = sqlite3.connect(db_path)
    user = conn.execute(
        "SELECT * FROM users WHERE reset_token=?",
        (token,)
    ).fetchone()

    if not user:
        conn.close()
        return "Invalid or expired token"

    expiry = datetime.fromisoformat(user[4])

    if datetime.utcnow() > expiry:
        conn.close()
        return "Token expired"

    if request.method == 'POST':
        new_password = generate_password_hash(request.form['password'])

        conn.execute(
            "UPDATE users SET password=?, reset_token=NULL, token_expiry=NULL WHERE id=?",
            (new_password, user[0])
        )
        conn.commit()
        conn.close()

        return redirect('/login')

    conn.close()
    return render_template("reset.html")

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

    transactions = conn.execute(
        "SELECT * FROM transactions WHERE user_id=?",
        (session['user_id'],)
    ).fetchall()

    conn.close()

    income = sum(t[2] for t in transactions if t[3] == "income")
    expenses = sum(t[2] for t in transactions if t[3] == "expense")
    balance = income - expenses

    category_data = {}
    for t in transactions:
        cat = t[4]
        category_data[cat] = category_data.get(cat, 0) + t[2]

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