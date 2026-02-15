from flask import (
    Flask, request, render_template, redirect, url_for,
    send_file, flash, session
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

# Render safe DB location:
# - If you add a Render Disk later, it exposes RENDER_DISK_PATH
# - Otherwise /tmp is writable (but resets on redeploy)
DATA_DIR = os.getenv("RENDER_DISK_PATH", "/tmp")
DB_PATH = os.path.join(DATA_DIR, "hisabkitab.db")
b.db")


# -------------------- DB helpers --------------------
def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def col_exists(conn, table, col):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == col for r in rows)


def add_col_if_missing(conn, table, col, coltype_sql):
    if not col_exists(conn, table, col):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype_sql}")


def init_db():
    conn = db_conn()
    cur = conn.cursor()

    # Users
    cur.execute("""
      CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT NOT NULL UNIQUE,
        pass_hash TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
      )
    """)

    # Settings (per user)
    cur.execute("""
      CREATE TABLE IF NOT EXISTS settings(
        user_id INTEGER PRIMARY KEY,
        business_name TEXT,
        email TEXT,
        phone TEXT,
        address TEXT,
        gstin TEXT,
        logo_path TEXT,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
      )
    """)

    # Clients (per user)
    cur.execute("""
      CREATE TABLE IF NOT EXISTS clients(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        name TEXT NOT NULL,
        email TEXT,
        phone TEXT,
        address TEXT,
        gstin TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
      )
    """)

    # Invoices
    cur.execute("""
      CREATE TABLE IF NOT EXISTS invoices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,

        invoice_no TEXT,
        invoice_date TEXT,

        from_name TEXT,
        from_email TEXT,
        from_phone TEXT,
        from_address TEXT,
        from_gstin TEXT,

        to_name TEXT,
        to_email TEXT,
        to_phone TEXT,
        to_address TEXT,
        to_gstin TEXT,

        notes TEXT,

        gst_mode INTEGER DEFAULT 0,
        gst_rate REAL DEFAULT 18,
        place_of_supply TEXT,

        created_at TEXT DEFAULT CURRENT_TIMESTAMP
      )
    """)

    # Items
    cur.execute("""
      CREATE TABLE IF NOT EXISTS items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invoice_id INTEGER NOT NULL,
        description TEXT NOT NULL,
        qty REAL NOT NULL,
        rate REAL NOT NULL
      )
    """)

    # ---- MIGRATION (fix old DB missing columns) ----
    # invoices
    add_col_if_missing(conn, "invoices", "user_id", "INTEGER")
    add_col_if_missing(conn, "invoices", "from_address", "TEXT")
    add_col_if_missing(conn, "invoices", "from_gstin", "TEXT")
    add_col_if_missing(conn, "invoices", "to_phone", "TEXT")
    add_col_if_missing(conn, "invoices", "to_address", "TEXT")
    add_col_if_missing(conn, "invoices", "to_gstin", "TEXT")
    add_col_if_missing(conn, "invoices", "gst_mode", "INTEGER DEFAULT 0")
    add_col_if_missing(conn, "invoices", "gst_rate", "REAL DEFAULT 18")
    add_col_if_missing(conn, "invoices", "place_of_supply", "TEXT")

    # clients
    add_col_if_missing(conn, "clients", "user_id", "INTEGER")

    conn.commit()
    conn.close()


init_db()


# -------------------- Auth helpers --------------------
def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def current_user_id():
    return int(session["user_id"])


def clean_float(text: str, default: float = 0.0) -> float:
    try:
        t = (text or "").replace("%", "").strip()
        return float(t) if t else default
    except Exception:
        return default


def get_settings(user_id: int):
    conn = db_conn()
    row = conn.execute("SELECT * FROM settings WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row


def ensure_default_settings(user_id: int, user_name: str):
    conn = db_conn()
    row = conn.execute("SELECT user_id FROM settings WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO settings(user_id, business_name) VALUES(?, ?)",
            (user_id, user_name or "Hisab Kitab")
        )
        conn.commit()
    conn.close()


def next_invoice_no(user_id: int):
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"HK-{today}-"
    conn = db_conn()
    row = conn.execute(
        "SELECT invoice_no FROM invoices WHERE user_id=? AND invoice_no LIKE ? ORDER BY id DESC LIMIT 1",
        (user_id, prefix + "%")
    ).fetchone()
    conn.close()

    if not row or not row["invoice_no"]:
        return prefix + "001"

    last = row["invoice_no"].split("-")[-1]
    try:
        n = int(last) + 1
    except Exception:
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

    subtotal = 0.0
    for it in items:
        subtotal += float(it["qty"]) * float(it["rate"])

    gst_mode = int(inv["gst_mode"] or 0)
    gst_rate = float(inv["gst_rate"] or 0)
    gst_amount = subtotal * (gst_rate / 100.0) if gst_mode else 0.0
    total = subtotal + gst_amount

    return inv, items, round(subtotal, 2), round(gst_amount, 2), round(total, 2)


# -------------------- Routes: Auth --------------------
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
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO users(name,email,pass_hash) VALUES(?,?,?)",
            (name, email, pass_hash)
        )
        conn.commit()
        user_id = cur.lastrowid
        conn.close()

        ensure_default_settings(user_id, name)

        session["user_id"] = user_id
        session["user_name"] = name
        return redirect(url_for("index"))

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

    ensure_default_settings(user["id"], user["name"])
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# -------------------- Routes: App --------------------
@app.route("/")
@login_required
def index():
    user_id = current_user_id()
    conn = db_conn()
    invoices = conn.execute("""
        SELECT id, invoice_no, invoice_date, to_name, from_name, created_at
        FROM invoices
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT 50
    """, (user_id,)).fetchall()
    conn.close()

    return render_template("index.html", invoices=invoices, user_name=session.get("user_name", ""))


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    user_id = current_user_id()
    s = get_settings(user_id)

    if request.method == "GET":
        return render_template("settings.html", s=s, user_name=session.get("user_name", ""))

    business_name = (request.form.get("business_name") or "").strip()
    email = (request.form.get("email") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    address = (request.form.get("address") or "").strip()
    gstin = (request.form.get("gstin") or "").strip()

    logo_path = (s["logo_path"] if s else "") or ""
    f = request.files.get("logo")
    if f and f.filename:
        os.makedirs(os.path.join(BASE_DIR, "static", "uploads"), exist_ok=True)
        filename = secure_filename(f.filename)
        save_path = os.path.join("static", "uploads", filename)
        f.save(os.path.join(BASE_DIR, save_path))
        logo_path = "/" + save_path.replace("\\", "/")

    conn = db_conn()
    conn.execute("""
      INSERT INTO settings(user_id, business_name, email, phone, address, gstin, logo_path, updated_at)
      VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
      ON CONFLICT(user_id) DO UPDATE SET
        business_name=excluded.business_name,
        email=excluded.email,
        phone=excluded.phone,
        address=excluded.address,
        gstin=excluded.gstin,
        logo_path=excluded.logo_path,
        updated_at=CURRENT_TIMESTAMP
    """, (user_id, business_name, email, phone, address, gstin, logo_path))
    conn.commit()
    conn.close()

    flash("Settings saved ✅", "ok")
    return redirect(url_for("settings"))


@app.route("/clients")
@login_required
def clients():
    user_id = current_user_id()
    conn = db_conn()
    rows = conn.execute("SELECT * FROM clients WHERE user_id=? ORDER BY id DESC", (user_id,)).fetchall()
    conn.close()
    return render_template("clients.html", clients=rows, user_name=session.get("user_name", ""))


@app.route("/clients/new", methods=["POST"])
@login_required
def clients_new():
    user_id = current_user_id()
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Client name required.", "error")
        return redirect(url_for("clients"))

    email = (request.form.get("email") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    address = (request.form.get("address") or "").strip()
    gstin = (request.form.get("gstin") or "").strip()

    conn = db_conn()
    conn.execute(
        "INSERT INTO clients(user_id, name, email, phone, address, gstin) VALUES(?,?,?,?,?,?)",
        (user_id, name, email, phone, address, gstin)
    )
    conn.commit()
    conn.close()

    flash("Client saved ✅", "ok")
    return redirect(url_for("clients"))


@app.route("/clients/<int:client_id>/delete", methods=["POST"])
@login_required
def clients_delete(client_id):
    user_id = current_user_id()
    conn = db_conn()
    conn.execute("DELETE FROM clients WHERE id=? AND user_id=?", (client_id, user_id))
    conn.commit()
    conn.close()
    flash("Client deleted ✅", "ok")
    return redirect(url_for("clients"))


@app.route("/new", methods=["GET", "POST"])
@login_required
def new_invoice():
    user_id = current_user_id()
    s = get_settings(user_id)

    if request.method == "GET":
        conn = db_conn()
        clients_list = conn.execute(
            "SELECT id, name, email, phone, address, gstin FROM clients WHERE user_id=? ORDER BY name ASC",
            (user_id,)
        ).fetchall()
        conn.close()

        return render_template(
            "new_invoice.html",
            invoice_no=next_invoice_no(user_id),
            today=datetime.now().strftime("%Y-%m-%d"),
            s=s,
            clients=clients_list,
            user_name=session.get("user_name", "")
        )

    invoice_no = (request.form.get("invoice_no") or "").strip()
    invoice_date = (request.form.get("invoice_date") or "").strip()

    from_name = (request.form.get("from_name") or (s["business_name"] if s else "") or "").strip()
    from_email = (request.form.get("from_email") or (s["email"] if s else "") or "").strip()
    from_phone = (request.form.get("from_phone") or (s["phone"] if s else "") or "").strip()
    from_address = (request.form.get("from_address") or (s["address"] if s else "") or "").strip()
    from_gstin = (request.form.get("from_gstin") or (s["gstin"] if s else "") or "").strip()

    to_name = (request.form.get("to_name") or "").strip()
    to_email = (request.form.get("to_email") or "").strip()
    to_phone = (request.form.get("to_phone") or "").strip()
    to_address = (request.form.get("to_address") or "").strip()
    to_gstin = (request.form.get("to_gstin") or "").strip()

    notes = (request.form.get("notes") or "").strip()

    gst_mode = 1 if request.form.get("gst_mode") == "on" else 0
    gst_rate = clean_float(request.form.get("gst_rate") or "18", 18.0)
    place_of_supply = (request.form.get("place_of_supply") or "").strip()

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
        qv = clean_float(q, 1.0)
        rv = clean_float(r, 0.0)
        if qv <= 0:
            qv = 1.0
        cleaned_items.append((d, qv, rv))

    if not cleaned_items:
        flash("At least 1 item required.", "error")
        return redirect(url_for("new_invoice"))

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
      INSERT INTO invoices(
        user_id,
        invoice_no, invoice_date,
        from_name, from_email, from_phone, from_address, from_gstin,
        to_name, to_email, to_phone, to_address, to_gstin,
        notes,
        gst_mode, gst_rate, place_of_supply
      )
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        user_id,
        invoice_no, invoice_date,
        from_name, from_email, from_phone, from_address, from_gstin,
        to_name, to_email, to_phone, to_address, to_gstin,
        notes,
        gst_mode, gst_rate, place_of_supply
    ))
    invoice_id = cur.lastrowid

    for d, qv, rv in cleaned_items:
        cur.execute(
            "INSERT INTO items(invoice_id, description, qty, rate) VALUES(?,?,?,?)",
            (invoice_id, d, qv, rv)
        )

    conn.commit()
    conn.close()

    return redirect(url_for("view_invoice", invoice_id=invoice_id))


@app.route("/invoice/<int:invoice_id>")
@login_required
def view_invoice(invoice_id):
    user_id = current_user_id()
    inv, items, subtotal, gst_amount, total = calc_totals(invoice_id, user_id)
    if not inv:
        flash("Invoice not found.", "error")
        return redirect(url_for("index"))

    return render_template(
        "view_invoice.html",
        inv=inv, items=items,
        subtotal=subtotal, gst_amount=gst_amount, total=total,
        user_name=session.get("user_name", "")
    )


@app.route("/invoice/<int:invoice_id>/delete", methods=["POST"])
@login_required
def delete_invoice(invoice_id):
    user_id = current_user_id()
    conn = db_conn()
    row = conn.execute("SELECT id FROM invoices WHERE id=? AND user_id=?", (invoice_id, user_id)).fetchone()
    if row:
        conn.execute("DELETE FROM items WHERE invoice_id=?", (invoice_id,))
        conn.execute("DELETE FROM invoices WHERE id=?", (invoice_id,))
        conn.commit()
        flash("Invoice deleted ✅", "ok")
    else:
        flash("Not allowed.", "error")
    conn.close()
    return redirect(url_for("index"))


@app.route("/invoice/<int:invoice_id>/pdf")
@login_required
def invoice_pdf(invoice_id):
    user_id = current_user_id()
    inv, items, subtotal, gst_amount, total = calc_totals(invoice_id, user_id)
    if not inv:
        flash("Invoice not found.", "error")
        return redirect(url_for("index"))

    s = get_settings(user_id)

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

    # Logo
    if s and s["logo_path"]:
        try:
            img_path = os.path.join(BASE_DIR, s["logo_path"].lstrip("/"))
            img = ImageReader(img_path)
            c.drawImage(img, x, height - 95, width=70, height=70, mask='auto')
        except Exception:
            pass

    title = (s["business_name"] if s else None) or inv["from_name"] or "Hisab Kitab"
    line(f"{title} - INVOICE", size=16, gap=22)
    line(f"Invoice No: {inv['invoice_no']}")
    line(f"Date: {inv['invoice_date']}")
    if inv["place_of_supply"]:
        line(f"Place of Supply: {inv['place_of_supply']}")
    y -= 8
    line("-" * 95, size=10, gap=14)

    # From
    line("From:", size=12, gap=18)
    line(f"  {inv['from_name']}")
    if inv["from_address"]:
        line(f"  Address: {inv['from_address']}")
    if inv["from_email"]:
        line(f"  Email: {inv['from_email']}")
    if inv["from_phone"]:
        line(f"  Phone: {inv['from_phone']}")
    if inv["from_gstin"]:
        line(f"  GSTIN: {inv['from_gstin']}")

    y -= 6

    # To
    line("To:", size=12, gap=18)
    line(f"  {inv['to_name']}")
    if inv["to_address"]:
        line(f"  Address: {inv['to_address']}")
    if inv["to_email"]:
        line(f"  Email: {inv['to_email']}")
    if inv["to_phone"]:
        line(f"  Phone: {inv['to_phone']}")
    if inv["to_gstin"]:
        line(f"  GSTIN: {inv['to_gstin']}")

    y -= 10
    line("-" * 95, size=10, gap=14)

    # Items
    line("Description                           Qty      Rate      Amount", size=11, gap=18)
    line("-" * 95, size=10, gap=14)

    for it in items:
        desc = it["description"]
        qty = float(it["qty"])
        rate = float(it["rate"])
        amt = qty * rate

        desc_lines = []
        while len(desc) > 35:
            desc_lines.append(desc[:35])
            desc = desc[35:]
        desc_lines.append(desc)

        first = True
        for dl in desc_lines:
            if first:
                line(f"{dl:<35}  {qty:>5.2f}  {rate:>8.2f}  {amt:>9.2f}", size=10, gap=14)
                first = False
            else:
                line(f"{dl}", size=10, gap=14)

    y -= 6
    line("-" * 95, size=10, gap=14)

    line(f"Subtotal: {subtotal:.2f}", size=11)

    if int(inv["gst_mode"] or 0) == 1:
        line(f"GST ({float(inv['gst_rate'] or 0):.2f}%): {gst_amount:.2f}", size=11)

    line(f"TOTAL: {total:.2f}", size=12, gap=20)

    if inv["notes"]:
        y -= 8
        line("Notes:", size=12, gap=18)
        notes = (inv["notes"] or "").replace("\r", "")
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

