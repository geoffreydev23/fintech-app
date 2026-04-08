from flask import Flask, render_template, request, redirect, session
import sqlite3
import os
from werkzeug.security import generate_password_hash, check_password_hash
import openai

app = Flask(__name__)
app.secret_key = "secret123"

# 🔐 API KEY (FROM ENV)
openai.api_key = os.getenv("OPENAI_API_KEY")

# 📁 DATABASE
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
db_path = os.path.join(BASE_DIR, 'database.db')

# 🧠 AUTO CATEGORY
def auto_category(desc):
    desc = desc.lower()
    if "food" in desc: return "Food"
    if "uber" in desc or "matatu" in desc: return "Transport"
    if "rent" in desc: return "Housing"
    if "crypto" in desc: return "Crypto"
    return "Other"

# 🤖 AI INSIGHTS
def generate_insights(transactions, income, expenses, category_data):
    insights = []
    if expenses > income:
        insights.append("⚠️ Expenses exceed income")
    for cat, amt in category_data.items():
        if expenses > 0 and amt > expenses * 0.4:
            insights.append(f"⚠️ High spending on {cat}")
    return insights

# 🗄️ INIT DB
def init_db():
    conn = sqlite3.connect(db_path)

    conn.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password TEXT)''')

    conn.execute('''CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount REAL,
        type TEXT,
        category TEXT,
        source TEXT,
        description TEXT)''')

    conn.execute('''CREATE TABLE IF NOT EXISTS archived_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount REAL,
        type TEXT,
        category TEXT,
        source TEXT,
        description TEXT)''')

    conn.close()

# 📝 REGISTER
@app.route('/register', methods=['GET','POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = generate_password_hash(request.form['password'])

        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO users (username,password) VALUES (?,?)",(username,password))
        conn.commit()
        conn.close()
        return redirect('/login')

    return render_template('register.html')

# 🔐 LOGIN
@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        conn = sqlite3.connect(db_path)
        user = conn.execute("SELECT * FROM users WHERE username=?",(username,)).fetchone()
        conn.close()

        if user and check_password_hash(user[2],password):
            session['user_id'] = user[0]
            return redirect('/')
        return "Invalid login"

    return render_template('login.html')

# 🚪 LOGOUT
@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

# 🌐 HOME
@app.route('/', methods=['GET','POST'])
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

        conn.execute("INSERT INTO transactions (user_id,amount,type,category,source,description) VALUES (?,?,?,?,?,?)",
        (session['user_id'],amount,t_type,category,source,desc))
        conn.commit()

    transactions = conn.execute("SELECT * FROM transactions WHERE user_id=?",(session['user_id'],)).fetchall()
    conn.close()

    income = sum(t[2] for t in transactions if t[3]=='income')
    expenses = sum(t[2] for t in transactions if t[3]=='expense')
    balance = income - expenses

    category_data={}
    for t in transactions:
        category_data[t[4]] = category_data.get(t[4],0)+t[2]

    insights = generate_insights(transactions,income,expenses,category_data)

    return render_template('index.html',
        transactions=transactions,
        income=income,
        expenses=expenses,
        balance=balance,
        category_data=category_data,
        insights=insights,
        success=success)

# 💳 M-PESA
@app.route('/mpesa', methods=['POST'])
def mpesa():
    if 'user_id' not in session:
        return redirect('/login')

    amount = request.form['amount']
    phone = request.form['phone']

    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO transactions (user_id,amount,type,category,source,description) VALUES (?,?,?,?,?,?)",
    (session['user_id'],amount,"income","M-Pesa","mpesa","Deposit"))
    conn.commit()
    conn.close()

    return redirect('/?success=mpesa')

# 🗑️ CLEAR → ARCHIVE
@app.route('/clear', methods=['POST'])
def clear_data():
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO archived_transactions SELECT * FROM transactions")
    conn.execute("DELETE FROM transactions")
    conn.commit()
    conn.close()
    return redirect('/')

# 📂 ARCHIVE
@app.route('/archive')
def archive():
    conn = sqlite3.connect(db_path)
    data = conn.execute("SELECT * FROM archived_transactions").fetchall()
    conn.close()
    return render_template('archive.html', archived=data)

# 🤖 CHATBOT
@app.route('/chat', methods=['POST'])
def chat():
    user_message = request.form['message']

    response = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        messages=[
            {"role":"system","content":"You are a helpful finance assistant"},
            {"role":"user","content":user_message}
        ]
    )

    return response['choices'][0]['message']['content']

# ▶️ RUN
if __name__ == "__main__":
    init_db()
    app.run(debug=True)