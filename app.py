from flask import Flask, render_template, request, redirect, session
import sqlite3
import os
from werkzeug.security import generate_password_hash, check_password_hash
from openai import OpenAI

app = Flask(__name__)
app.secret_key = "secret123"

# 🔐 SECURE API KEY
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

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

# 💰 AI BUDGETING SYSTEM (NEW)
def generate_budget(category_data, income):
    budget = {}
    tips = []

    for category, amount in category_data.items():
        recommended = income * 0.3  # 30% rule

        budget[category] = {
            "spent": amount,
            "recommended": recommended
        }

        if amount > recommended:
            tips.append(f"⚠️ You are overspending on {category}")
        else:
            tips.append(f"✅ Your {category} spending is healthy")

    return budget, tips

# 🗄️ INIT DB
def init_db():
    conn = sqlite3.connect(db_path)

    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password TEXT
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

    conn.close()

# 📝 REGISTER
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = generate_password_hash(request.form['password'])

        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO users (username, password) VALUES (?, ?)",
            (username, password)
        )
        conn.commit()
        conn.close()

        return redirect('/login')

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
            return "Invalid login"

    return render_template('login.html')

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

    success = request.args.get('success')

    conn = sqlite3.connect(db_path)

    if request.method == 'POST':
        amount = request.form['amount']
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

    # ✅ NEW: AI BUDGETING
    budget_data, budget_tips = generate_budget(category_data, income)

    return render_template(
        'index.html',
        transactions=transactions,
        income=income,
        expenses=expenses,
        balance=balance,
        category_data=category_data,
        insights=insights,
        success=success,
        budget_data=budget_data,      # ✅ NEW
        budget_tips=budget_tips       # ✅ NEW
    )

# 💳 M-PESA
@app.route('/mpesa', methods=['POST'])
def mpesa():
    if 'user_id' not in session:
        return redirect('/login')

    amount = request.form['amount']
    phone = request.form['phone']

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO transactions (user_id, amount, type, category, source, description) VALUES (?, ?, ?, ?, ?, ?)",
        (session['user_id'], amount, "income", "M-Pesa", "mpesa", "M-Pesa Deposit")
    )
    conn.commit()
    conn.close()

    return redirect('/?success=mpesa')

# 🗑️ CLEAR
@app.route('/clear', methods=['POST'])
def clear_data():
    if 'user_id' not in session:
        return redirect('/login')

    conn = sqlite3.connect(db_path)

    conn.execute('''
        INSERT INTO archived_transactions (user_id, amount, type, category, source, description)
        SELECT user_id, amount, type, category, source, description
        FROM transactions WHERE user_id=?
    ''', (session['user_id'],))

    conn.execute("DELETE FROM transactions WHERE user_id=?", (session['user_id'],))

    conn.commit()
    conn.close()

    return redirect('/?success=cleared')

# 📂 ARCHIVE
@app.route('/archive')
def archive():
    if 'user_id' not in session:
        return redirect('/login')

    conn = sqlite3.connect(db_path)
    archived = conn.execute(
        "SELECT * FROM archived_transactions WHERE user_id=?",
        (session['user_id'],)
    ).fetchall()
    conn.close()

    return render_template('archive.html', archived=archived)

# 🤖 SMART CHATBOT (AI + FALLBACK)
@app.route('/chat', methods=['POST'])
def chat():
    if 'user_id' not in session:
        return redirect('/login')

    user_message = request.form['message'].lower()

    conn = sqlite3.connect(db_path)
    transactions = conn.execute(
        "SELECT * FROM transactions WHERE user_id=?",
        (session['user_id'],)
    ).fetchall()
    conn.close()

    income = sum(t[2] for t in transactions if t[3] == "income")
    expenses = sum(t[2] for t in transactions if t[3] == "expense")
    balance = income - expenses

    def fallback_ai(msg):
        if "balance" in msg:
            return f"💰 Your current balance is Ksh {balance}"
        elif "income" in msg:
            return f"📈 Your total income is Ksh {income}"
        elif "expense" in msg or "spending" in msg:
            return f"💸 Your total expenses are Ksh {expenses}"
        elif "save" in msg:
            return "💡 Try saving at least 20% of your income."
        elif "invest" in msg:
            return "📊 Consider investing in stocks, crypto, or savings."
        elif "hello" in msg or "hi" in msg:
            return "👋 Hello! I'm your finance assistant."
        else:
            return "🤖 Ask me about your balance, spending, or savings."

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": f"""
                    You are a smart finance assistant.

                    Income: {income}
                    Expenses: {expenses}
                    Balance: {balance}

                    Give short helpful advice.
                    """
                },
                {"role": "user", "content": user_message}
            ]
        )

        return response.choices[0].message.content

    except Exception as e:
        print("CHAT ERROR:", e)
        return fallback_ai(user_message)

# ▶️ RUN
if __name__ == "__main__":
    init_db()
    app.run(debug=True)