from flask import Flask, render_template, request, redirect, session
import sqlite3
import os

app = Flask(__name__)
app.secret_key = "secret123"

# 📁 DATABASE PATH
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
db_path = os.path.join(BASE_DIR, 'database.db')

# 🧠 AUTO CATEGORY
def auto_category(desc):
    desc = desc.lower()

    if "food" in desc or "restaurant" in desc or "kfc" in desc:
        return "Food"
    elif "uber" in desc or "bolt" in desc or "matatu" in desc:
        return "Transport"
    elif "rent" in desc or "house" in desc:
        return "Housing"
    elif "bitcoin" in desc or "crypto" in desc:
        return "Crypto"
    elif "stock" in desc:
        return "Stocks"
    elif "game" in desc:
        return "Gaming"
    else:
        return "Other"

# 🤖 AI INSIGHTS
def generate_insights(transactions, income, expenses, category_data):
    insights = []

    if expenses > income:
        insights.append("⚠️ Your expenses are higher than your income!")

    total_spent = sum(category_data.values())

    for category, amount in category_data.items():
        if total_spent > 0:
            percent = (amount / total_spent) * 100
            if percent > 40:
                insights.append(f"⚠️ You are spending a lot on {category} ({percent:.1f}%)")

    if "Crypto" in category_data and category_data["Crypto"] > 0:
        insights.append("🪙 You are actively investing in crypto")

    return insights

# 🗄️ INIT DATABASE
def init_db():
    conn = sqlite3.connect(db_path)

    # USERS
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password TEXT
        )
    ''')

    # TRANSACTIONS
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

    # ARCHIVE
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
        password = request.form['password']

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
            "SELECT * FROM users WHERE username=? AND password=?",
            (username, password)
        ).fetchone()
        conn.close()

        if user:
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

# 🌐 MAIN PAGE
@app.route('/', methods=['GET', 'POST'])
def index():
    if 'user_id' not in session:
        return redirect('/login')

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
        category = t[4]
        amount = t[2]

        if category in category_data:
            category_data[category] += amount
        else:
            category_data[category] = amount

    insights = generate_insights(transactions, income, expenses, category_data)

    return render_template(
        'index.html',
        transactions=transactions,
        income=income,
        expenses=expenses,
        balance=balance,
        category_data=category_data,
        insights=insights
    )

# 🗑️ CLEAR → ARCHIVE
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

    conn.execute(
        "DELETE FROM transactions WHERE user_id=?",
        (session['user_id'],)
    )

    conn.commit()
    conn.close()

    return redirect('/')

# 📂 VIEW ARCHIVE
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

# 🔄 RESTORE
@app.route('/restore/<int:id>')
def restore(id):
    if 'user_id' not in session:
        return redirect('/login')

    conn = sqlite3.connect(db_path)

    conn.execute('''
        INSERT INTO transactions (user_id, amount, type, category, source, description)
        SELECT user_id, amount, type, category, source, description
        FROM archived_transactions WHERE id=?
    ''', (id,))

    conn.execute(
        "DELETE FROM archived_transactions WHERE id=?",
        (id,)
    )

    conn.commit()
    conn.close()

    return redirect('/archive')

# ▶️ RUN
if __name__ == "__main__":
    init_db()
    app.run(debug=True)