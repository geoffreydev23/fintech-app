from flask import Flask, render_template, request
import sqlite3
import os

app = Flask(__name__)

# 📁 DATABASE PATH (IMPORTANT FOR DEPLOYMENT)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
db_path = os.path.join(BASE_DIR, 'database.db')

# 🧠 AUTO CATEGORY FUNCTION
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

# 🤖 AI INSIGHTS FUNCTION
def generate_insights(transactions, income, expenses, category_data):
    insights = []

    # ⚖️ Balance warning
    if expenses > income:
        insights.append("⚠️ Your expenses are higher than your income!")

    # 🍔 Category warnings
    total_spent = sum(category_data.values())

    for category, amount in category_data.items():
        if total_spent > 0:
            percent = (amount / total_spent) * 100

            if percent > 40:
                insights.append(f"⚠️ You are spending a lot on {category} ({percent:.1f}%)")

    # 🪙 Crypto insight
    if "Crypto" in category_data and category_data["Crypto"] > 0:
        insights.append("🪙 You are actively investing in crypto")

    return insights

# 🗄️ DATABASE SETUP
def init_db():
    conn = sqlite3.connect(db_path)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            amount REAL,
            type TEXT,
            category TEXT,
            source TEXT,
            description TEXT
        )
    ''')
    conn.close()

# 🌐 MAIN ROUTE
@app.route('/', methods=['GET', 'POST'])
def index():
    conn = sqlite3.connect(db_path)

    if request.method == 'POST':
        amount = request.form['amount']
        t_type = request.form['type']
        source = request.form['source']
        desc = request.form['description']

        # 🧠 AUTO CATEGORY
        category = request.form['category'] or auto_category(desc)

        conn.execute(
            "INSERT INTO transactions (amount, type, category, source, description) VALUES (?, ?, ?, ?, ?)",
            (amount, t_type, category, source, desc)
        )
        conn.commit()

    # 📥 FETCH DATA
    transactions = conn.execute("SELECT * FROM transactions").fetchall()
    conn.close()

    # 📊 CALCULATIONS
    income = sum(t[1] for t in transactions if t[2] == "income")
    expenses = sum(t[1] for t in transactions if t[2] == "expense")
    balance = income - expenses

    # 📊 CATEGORY DATA
    category_data = {}

    for t in transactions:
        category = t[3]
        amount = t[1]

        if category in category_data:
            category_data[category] += amount
        else:
            category_data[category] = amount

    # 🤖 GENERATE INSIGHTS
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

# ▶️ RUN APP
if __name__ == "__main__":
    init_db()
    app.run(debug=True)