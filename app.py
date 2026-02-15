from flask import (
    Flask, request, render_template, redirect,
    url_for, send_file, flash, session
)
import os
import sqlite3
from datetime import datetime
from io import BytesIO
from functools import wraps
from decimal import Decimal, ROUND_HALF_UP

from werkzeug.security import generate_password_hash, check_password_hash
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4


app = Flask(__name__)
app.secret_key = os.getenv("HK_SECRET_KEY", "hisab-kitab-dev-key")

BASE_DIR = os.path.dirname(__file__)

# Render-safe DB location (writable). Note: /tmp resets on redeploy.
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
        name TEXT NOT NULL,
        email TEXT NOT NULL UNIQUE,
        pass_hash TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS invoices(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        invoice_no TEXT NOT NULL,
        invoice_date TEXT NOT NULL,

        from_name TEXT NOT NULL,
        from_email TEXT,
        from_phone TEXT,

        to_name TEXT NOT NULL,
        to_email TEXT,
        to_phone TEXT,

        notes TEXT,

        gst_mode INTEGER DEFAULT 0,
        gst_rate REAL DEFAULT 18,

        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS items(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invoice_id INTEGER NOT NULL,
        description TEXT NOT NULL,
        qty REAL NOT NULL,
        rate REAL NOT NULL
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


def money2(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def to_dec(v, default="0"):
    try:
        s = ("" if v is None else str(v))
        s = s.replace("%", "").replace(",", "").strip()
        if s == "":
            s = default
        return Decimal(s)
    except:
        return Decimal(default)


def next_invoice_no(user_id: int):
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"HK-{today}-"
    conn = db_conn()
    row = conn.execute(
        "SELECT invoice_no FROM invoices WHERE user_id=? AND invoice_no LIKE ? ORDER BY id DESC LIMIT 1",
        (user_id, prefix + "%")
    ).fetchone()
    conn.close()

    if not row:
        return prefix + "001"

    last = row["invoice_no"].split("-")[-1]
    try:
        n = int(last) + 1
    except:
        n = 1
    return prefix + str(n).zfill(3)


def calc_totals(invoice_id: int, user_id: int):
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

    if not inv:
        return None, [], 0.0, 0.0, 0.0

    subtotal = Decimal("0")
    for it in items:
        qty = to_dec(it["qty"], "0")
        rate = to_dec(it["rate"], "0")
        subtotal += qty * rate

    subtotal = money2(subtotal)

    gst = Decimal("0")
    gst_rate = to_dec(inv["gst_rate"], "0")
    if int(inv["gst_mode"] or 0) == 1:
        gst = money2(subtotal * gst_rate / Decimal("100"))

    total = money2(subtotal + gst)

    return inv, items, float(subtotal), float(gst), float(total)


# -------------------- Auth --------------------
@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "GET":
        return render_template("signup.html")

    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    password = (request.form.get("password") or "").strip()

    if not name or not email or not password:
        flash("All fields required.", "error")
        return redirect(url_for("signup"))

    pass_hash = generate_password_hash(password)

    try:
        conn = db_conn()
        conn.execute(
            "INSERT INTO users(name,email,pass_hash) VALUES(?,?,?)",
            (name, email, pass_hash)
        )
        conn.commit()
        conn.close()
        flash("Account created ✅ Please login.", "ok")
        return redirect(url_for("login"))
    except sqlite3.IntegrityError:
        flash("Email already exists. Please login.", "error")
        return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    email = (request.form.get("email") or "").strip().lower()
    password = (request.form.get("password") or "").strip()

    conn = db_conn()
    user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    conn.close()

    if not user or not check_password_hash(user["pass_hash"], password):
        flash("Invalid email/password.", "error")
        return redirect(url_for("login"))

    session["user_id"] = user["id"]
    session["user_name"] = user["name"]
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# -------------------- App --------------------
@app.route("/")
@login_required
def index():
    user_id = int(session["user_id"])
    conn = db_conn()
    invoices = conn.execute(
        "SELECT id, invoice_no, invoice_date, to_name, from_name FROM invoices WHERE user_id=? ORDER BY id DESC",
        (user_id,)
    ).fetchall()
    conn.close()
    return render_template("index.html", invoices=invoices, user_name=session.get("user_name", ""))


@app.route("/new", methods=["GET", "POST"])
@login_required
def new_invoice():
    user_id = int(session["user_id"])

    if request.method == "GET":
        return render_template(
            "new_invoice.html",
            invoice_no=next_invoice_no(user_id),
            today=datetime.now().strftime("%Y-%m-%d")
        )

    invoice_no = (request.form.get("invoice_no") or "").strip()
    invoice_date = (request.form.get("invoice_date") or "").strip()

    from_name = (request.form.get("from_name") or "").strip()
    from_email = (request.form.get("from_email") or "").strip()
    from_phone = (request.form.get("from_phone") or "").strip()

    to_name = (request.form.get("to_name") or "").strip()
    to_email = (request.form.get("to_email") or "").strip()
    to_phone = (request.form.get("to_phone") or "").strip()

    notes = (request.form.get("notes") or "").strip()

    gst_mode = 1 if request.form.get("gst_mode") == "on" else 0
    gst_rate = float(to_dec(request.form.get("gst_rate"), "18"))

    descs = request.form.getlist("desc[]")
    qtys = request.form.getlist("qty[]")
    rates = request.form.getlist("rate[]")

    if not invoice_no or not invoice_date or not from_name or not to_name:
        flash("Invoice No, Date, From Name, To Name required.", "error")
        return redirect(url_for("new_invoice"))

    cleaned_items = []
    for d, q, r in zip(descs, qtys, rates):
        d = (d or "").strip()
        if not d:
            continue
        qv = to_dec(q, "1")
        rv = to_dec(r, "0")
        if qv <= 0:
            qv = Decimal("1")
        cleaned_items.append((d, qv, rv))

    if not cleaned_items:
        flash("At least 1 item required.", "error")
        return redirect(url_for("new_invoice"))

    conn = db_conn()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO invoices(
            user_id, invoice_no, invoice_date,
            from_name, from_email, from_phone,
            to_name, to_email, to_phone,
            notes, gst_mode, gst_rate
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        user_id, invoice_no, invoice_date,
        from_name, from_email, from_phone,
        to_name, to_email, to_phone,
        notes, gst_mode, gst_rate
    ))
    invoice_id = cur.lastrowid

    for d, qv, rv in cleaned_items:
        cur.execute(
            "INSERT INTO items(invoice_id, description, qty, rate) VALUES(?,?,?,?)",
            (invoice_id, d, float(qv), float(rv))
        )

    conn.commit()
    conn.close()

    return redirect(url_for("view_invoice", invoice_id=invoice_id))


@app.route("/invoice/<int:invoice_id>")
@login_required
def view_invoice(invoice_id):
    user_id = int(session["user_id"])
    inv, items, subtotal, gst, total = calc_totals(invoice_id, user_id)
    if not inv:
        flash("Invoice not found.", "error")
        return redirect(url_for("index"))

    return render_template(
        "view_invoice.html",
        inv=inv, items=items,
        subtotal=subtotal, gst=gst, total=total,
        user_name=session.get("user_name", "")
    )


@app.route("/invoice/<int:invoice_id>/pdf")
@login_required
def invoice_pdf(invoice_id):
    user_id = int(session["user_id"])
    inv, items, subtotal, gst, total = calc_totals(invoice_id, user_id)
    if not inv:
        flash("Invoice not found.", "error")
        return redirect(url_for("index"))

    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    x = 40
    y = height - 50

    def line(txt, size=11, gap=16):
        nonlocal y
        if y < 60:
            c.showPage()
            y = height - 50
        c.setFont("Helvetica", size)
        c.drawString(x, y, txt[:120])
        y -= gap

    line("HISAB KITAB - INVOICE", size=16, gap=22)
    line(f"Invoice No: {inv['invoice_no']}")
    line(f"Date: {inv['invoice_date']}")
    y -= 6
    line("-" * 95, size=10, gap=14)

    line("From:", size=12, gap=18)
    line(f"  {inv['from_name']}")
    if inv["from_email"]:
        line(f"  Email: {inv['from_email']}")
    if inv["from_phone"]:
        line(f"  Phone: {inv['from_phone']}")
    y -= 6

    line("To:", size=12, gap=18)
    line(f"  {inv['to_name']}")
    if inv["to_email"]:
        line(f"  Email: {inv['to_email']}")
    if inv["to_phone"]:
        line(f"  Phone: {inv['to_phone']}")
    y -= 8
    line("-" * 95, size=10, gap=14)

    line("Description                           Qty      Rate      Amount", size=11, gap=18)
    line("-" * 95, size=10, gap=14)

    for it in items:
        desc = it["description"]
        qty = Decimal(str(it["qty"]))
        rate = Decimal(str(it["rate"]))
        amt = money2(qty * rate)

        # wrap description
        d = desc
        lines = []
        while len(d) > 35:
            lines.append(d[:35])
            d = d[35:]
        lines.append(d)

        first = True
        for dl in lines:
            if first:
                line(f"{dl:<35}  {float(qty):>5.2f}  {float(rate):>8.2f}  {float(amt):>9.2f}", size=10, gap=14)
                first = False
            else:
                line(f"{dl}", size=10, gap=14)

    y -= 6
    line("-" * 95, size=10, gap=14)

    line(f"Subtotal: {subtotal:.2f}", size=11)
    if int(inv["gst_mode"] or 0) == 1:
        line(f"GST ({float(inv['gst_rate'] or 0):.2f}%): {gst:.2f}", size=11)
    line(f"TOTAL: {total:.2f}", size=12, gap=20)

    if inv["notes"]:
        y -= 6
        line("Notes:", size=12, gap=18)
        notes = inv["notes"].replace("\r", "")
        for part in notes.split("\n"):
            while len(part) > 95:
                line("  " + part[:95], size=10, gap=14)
                part = part[95:]
            line("  " + part, size=10, gap=14)

    c.showPage()
    c.save()

    buffer.seek(0)
    return send_file(
        buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"invoice_{inv['invoice_no']}.pdf"
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
