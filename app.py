from flask import (
    Flask, request, render_template, redirect,
    url_for, send_file, flash, session
)
import os
import sqlite3
from datetime import datetime
from io import BytesIO
from functools import wraps

from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader


app = Flask(__name__)
app.secret_key = os.getenv("HK_SECRET_KEY", "hisab-kitab-dev-key")

BASE_DIR = os.path.dirname(__file__)

# Render-safe DB location
DATA_DIR = os.getenv("RENDER_DISK_PATH", "/tmp")
DB_PATH = os.path.join(DATA_DIR, "hisabkitab.db")


# -------------------- DB --------------------
def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db_conn()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        email TEXT UNIQUE,
        pass_hash TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS invoices(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        invoice_no TEXT,
        invoice_date TEXT,
        from_name TEXT,
        from_email TEXT,
        to_name TEXT,
        to_email TEXT,
        notes TEXT,
        gst_mode INTEGER DEFAULT 0,
        gst_rate REAL DEFAULT 18
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS items(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invoice_id INTEGER,
        description TEXT,
        qty REAL,
        rate REAL
    )
    """)

    conn.commit()
    conn.close()


init_db()


# -------------------- Helpers --------------------
def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def clean_float(v, default=0.0):
    try:
        return float((v or "").replace("%", "").strip())
    except:
        return default


def calc_totals(invoice_id, user_id):
    conn = db_conn()
    inv = conn.execute(
        "SELECT * FROM invoices WHERE id=? AND user_id=?",
        (invoice_id, user_id)
    ).fetchone()
    items = conn.execute(
        "SELECT * FROM items WHERE invoice_id=?",
        (invoice_id,)
    ).fetchall()
    conn.close()

    subtotal = sum(float(i["qty"]) * float(i["rate"]) for i in items)
    gst = subtotal * (float(inv["gst_rate"] or 0) / 100) if inv["gst_mode"] else 0
    total = subtotal + gst
    return inv, items, round(subtotal,2), round(gst,2), round(total,2)


# -------------------- Auth --------------------
@app.route("/signup", methods=["GET","POST"])
def signup():
    if request.method == "GET":
        return render_template("signup.html")

    name = request.form["name"]
    email = request.form["email"]
    password = generate_password_hash(request.form["password"])

    conn = db_conn()
    conn.execute(
        "INSERT INTO users(name,email,pass_hash) VALUES(?,?,?)",
        (name,email,password)
    )
    conn.commit()
    conn.close()

    return redirect(url_for("login"))


@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    email = request.form["email"]
    password = request.form["password"]

    conn = db_conn()
    user = conn.execute(
        "SELECT * FROM users WHERE email=?",
        (email,)
    ).fetchone()
    conn.close()

    if user and check_password_hash(user["pass_hash"], password):
        session["user_id"] = user["id"]
        return redirect(url_for("index"))

    flash("Invalid login")
    return redirect(url_for("login"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# -------------------- Main --------------------
@app.route("/")
@login_required
def index():
    user_id = session["user_id"]
    conn = db_conn()
    invoices = conn.execute(
        "SELECT * FROM invoices WHERE user_id=? ORDER BY id DESC",
        (user_id,)
    ).fetchall()
    conn.close()
    return render_template("index.html", invoices=invoices)


@app.route("/new", methods=["GET","POST"])
@login_required
def new_invoice():
    user_id = session["user_id"]

    if request.method == "GET":
        return render_template("new_invoice.html")

    invoice_no = request.form["invoice_no"]
    invoice_date = request.form["invoice_date"]
    from_name = request.form["from_name"]
    from_email = request.form["from_email"]
    to_name = request.form["to_name"]
    to_email = request.form["to_email"]
    notes = request.form["notes"]

    gst_mode = 1 if request.form.get("gst_mode") else 0
    gst_rate = clean_float(request.form.get("gst_rate"),18)

    descs = request.form.getlist("desc[]")
    qtys = request.form.getlist("qty[]")
    rates = request.form.getlist("rate[]")

    conn = db_conn()
    cur = conn.cursor()

    # ✅ FIXED INSERT BLOCK
    cur.execute("""
        INSERT INTO invoices(
            user_id, invoice_no, invoice_date,
            from_name, from_email,
            to_name, to_email,
            notes, gst_mode, gst_rate
        )
        VALUES(?,?,?,?,?,?,?,?,?,?)
    """, (
        user_id, invoice_no, invoice_date,
        from_name, from_email,
        to_name, to_email,
        notes, gst_mode, gst_rate
    ))

    invoice_id = cur.lastrowid

    for d,q,r in zip(descs,qtys,rates):
        cur.execute(
            "INSERT INTO items(invoice_id,description,qty,rate) VALUES(?,?,?,?)",
            (invoice_id,d,clean_float(q,1),clean_float(r,0))
        )

    conn.commit()
    conn.close()

    return redirect(url_for("view_invoice",invoice_id=invoice_id))


@app.route("/invoice/<int:invoice_id>")
@login_required
def view_invoice(invoice_id):
    inv, items, subtotal, gst, total = calc_totals(invoice_id, session["user_id"])
    return render_template(
        "view_invoice.html",
        inv=inv, items=items,
        subtotal=subtotal, gst=gst, total=total
    )


@app.route("/invoice/<int:invoice_id>/pdf")
@login_required
def invoice_pdf(invoice_id):
    inv, items, subtotal, gst, total = calc_totals(invoice_id, session["user_id"])

    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width,height = A4
    y = height - 40

    def line(t):
        nonlocal y
        c.drawString(40,y,t)
        y -= 15

    line("HISAB KITAB - INVOICE")
    line(f"Invoice No: {inv['invoice_no']}")
    line(f"Date: {inv['invoice_date']}")
    y -= 10

    for it in items:
        amt = it["qty"]*it["rate"]
        line(f"{it['description']} - {it['qty']} x {it['rate']} = {amt}")

    y -= 10
    line(f"Subtotal: {subtotal}")
    line(f"GST: {gst}")
    line(f"Total: {total}")

    c.save()
    buffer.seek(0)

    return send_file(buffer,
                     as_attachment=True,
                     download_name="invoice.pdf",
                     mimetype="application/pdf")


if __name__ == "__main__":
    port = int(os.getenv("PORT","5000"))
    app.run(host="0.0.0.0", port=port)
