import os, time, secrets, sqlite3, csv
from io import StringIO
from datetime import datetime, timezone, timedelta
from functools import wraps
from urllib.parse import urlencode

from flask import (
    Flask, request, jsonify, render_template, redirect, url_for, session,
    abort, send_from_directory, flash, Response
)
from flask_cors import CORS
from flask_jwt_extended import (
    JWTManager, create_access_token, jwt_required, get_jwt, get_jwt_identity
)
from passlib.hash import bcrypt
from sqlalchemy import (
    create_engine, Column, Integer, String, DateTime, Boolean, ForeignKey,
    UniqueConstraint, and_, or_, select
)
from sqlalchemy.orm import sessionmaker, declarative_base, relationship, scoped_session
from werkzeug.utils import secure_filename

# This is a time tracking backend service that provides both web and API interfaces for managing users, organizations, projects, tasks, time sessions, and heartbeats. It uses Flask for the web framework, SQLAlchemy for ORM, and JWT for authentication.

# ---------------- App config ----------------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "tracker.db")
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"))
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret")
app.config["JWT_SECRET_KEY"] = os.environ.get("JWT_SECRET_KEY", "dev-jwt-secret")
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

CORS(app, supports_credentials=True)
jwt = JWTManager(app)

# ---------------- DB setup ----------------
engine = create_engine(f"sqlite:///{DB_PATH}", echo=False, future=True)
SessionLocal = scoped_session(sessionmaker(bind=engine, autoflush=False, autocommit=False))
Base = declarative_base()

def now_utc() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)

# ---------------- Models ----------------
class Organization(Base):
    """Represents a workspace organization that owns projects and members."""
    __tablename__ = "organizations"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    created_at = Column(DateTime, default=now_utc)

class User(Base):
    """Registered user account with email-based authentication."""
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    full_name = Column(String, default="")
    default_org_id = Column(Integer, ForeignKey("organizations.id"), nullable=True)
    default_org = relationship("Organization")

class Membership(Base):
    """Links a user to an organization with a role (admin or member)."""
    __tablename__ = "memberships"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    role = Column(String, default="member")  # 'admin' | 'member'
    __table_args__ = (UniqueConstraint("user_id", "org_id", name="uq_member_org"),)
    user = relationship("User")
    org = relationship("Organization")

class Project(Base):
    """A named project within an organization that tasks are grouped under."""
    __tablename__ = "projects"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    name = Column(String, nullable=False)
    __table_args__ = (UniqueConstraint("org_id", "name", name="uq_org_project"),)

class Assignment(Base):
    """ কোন ইউজার কোন প্রজেক্টে কাজ করতে পারবে """
    __tablename__ = "assignments"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    __table_args__ = (UniqueConstraint("org_id", "user_id", "project_id", name="uq_assignment"),)

class Task(Base):
    """A unit of work within a project, created by a user, that time is tracked against."""
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)  # creator
    name = Column(String, nullable=False)

class TimeSession(Base):
    """A tracked work session with start/end timestamps for a given task."""
    __tablename__ = "time_sessions"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False)
    started_at = Column(DateTime, nullable=False)
    ended_at = Column(DateTime)

class Heartbeat(Base):
    """Periodic activity snapshot (keystrokes, mouse events, idle flag, optional screenshot)."""
    __tablename__ = "heartbeats"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    session_id = Column(Integer, ForeignKey("time_sessions.id"), nullable=False)
    ts = Column(DateTime, default=now_utc)
    key_count = Column(Integer, default=0)
    mouse_count = Column(Integer, default=0)
    is_idle = Column(Boolean, default=False)
    note = Column(String, default="")
    screenshot_path = Column(String, nullable=True)

class Invitation(Base):
    """Token-based invitation linking a new user to an organization and project."""
    __tablename__ = "invitations"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    email = Column(String, nullable=False)
    token = Column(String, unique=True, nullable=False)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    accepted = Column(Boolean, default=False)
    created_at = Column(DateTime, default=now_utc)

Base.metadata.create_all(engine)

# --------- ensure schema for old DB ---------
def ensure_schema() -> None:
    """Run forward-compatible schema migrations on an existing SQLite database.

    Adds any missing tables (projects, assignments) and columns
    (project_id, org_id, default_org_id) so older databases are
    seamlessly upgraded on startup.
    """
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        def col_exists(table, col):
            cur.execute(f"PRAGMA table_info({table})")
            return any(r[1] == col for r in cur.fetchall())
        # new tables
        cur.execute("""CREATE TABLE IF NOT EXISTS projects(
            id INTEGER PRIMARY KEY, org_id INTEGER, name TEXT
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS assignments(
            id INTEGER PRIMARY KEY, org_id INTEGER, user_id INTEGER, project_id INTEGER
        )""")
        # invitations add project_id if missing
        if not col_exists("invitations", "project_id"):
            try: cur.execute("ALTER TABLE invitations ADD COLUMN project_id INTEGER")
            except: pass
        # org_id columns if missing
        if not col_exists("users", "default_org_id"):
            cur.execute("ALTER TABLE users ADD COLUMN default_org_id INTEGER")
        for t in ("tasks","time_sessions","heartbeats"):
            if not col_exists(t, "org_id"):
                cur.execute(f"ALTER TABLE {t} ADD COLUMN org_id INTEGER")
        # task add project_id if missing
        if not col_exists("tasks", "project_id"):
            try: cur.execute("ALTER TABLE tasks ADD COLUMN project_id INTEGER")
            except: pass
        con.commit()
ensure_schema()

# ---------------- Helpers ----------------
def pw_hash(p: str) -> str:
    """Hash a plaintext password using bcrypt."""
    return bcrypt.hash(p)

def pw_ok(p: str, h: str) -> bool:
    """Verify a plaintext password against a bcrypt hash."""
    return bcrypt.verify(p, h)

def to_bool(raw: str) -> bool:
    """Parse a truthy string ('true'/'1'/'yes', case-insensitive) into a bool."""
    return str(raw).strip().lower() in ("true", "1", "yes")

def current_context() -> tuple[int, int, str]:
    """Extract (user_id, org_id, role) from the current JWT."""
    j = get_jwt()
    uid = int(get_jwt_identity())
    org_id = int(j["org_id"])
    role = j.get("role","member")
    return uid, org_id, role

def as_aware(dt: datetime | None) -> datetime | None:
    """Ensure a datetime is timezone-aware, defaulting to UTC."""
    if dt is None: return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

def format_duration(total_seconds: int) -> str:
    """Convert a duration in seconds to an HH:MM:SS string."""
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    s = total_seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

def format_duration_compact(total_seconds: int) -> str:
    """Convert a duration in seconds to a short "1h 5m" style string, omitting zero units."""
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    if h and m:
        return f"{h}h {m}m"
    return f"{h}h" if h else f"{m}m"

def seconds_to_hours(total_seconds: int) -> float:
    """Convert a duration in seconds to hours, rounded to two decimal places.

    Handy for reporting/export views that show totals in hours rather
    than the HH:MM:SS format used elsewhere.
    """
    return round(total_seconds / 3600, 2)

def admin_required(fn):
    """Route decorator that aborts with 403 unless the current user's role is admin.

    Wraps a Flask view; relies on current_context() to resolve the caller's
    (org_id, user_id, role) for the active request.
    """
    @wraps(fn)
    def wrapper(*a, **k):
        _, _, role = current_context()
        if role != "admin": abort(403, description="admin only")
        return fn(*a, **k)
    return wrapper

def parse_range(qs) -> tuple[datetime, datetime]:
    """Resolve a request's ?range=today|week|month|from/to query params into a (start, end) UTC datetime pair."""
    today = datetime.now(timezone.utc).date()
    preset = (qs.get("range") or "today").lower()
    if preset == "today":
        start = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
        end = start + timedelta(days=1)
    elif preset == "week":
        start = datetime.combine(today - timedelta(days=today.weekday()), datetime.min.time(), tzinfo=timezone.utc)
        end = start + timedelta(days=7)
    elif preset == "month":
        start = datetime.combine(today.replace(day=1), datetime.min.time(), tzinfo=timezone.utc)
        end = (datetime(start.year+1,1,1,tzinfo=timezone.utc) if start.month==12
               else datetime(start.year,start.month+1,1,tzinfo=timezone.utc))
    else:
        f = qs.get("from"); t = qs.get("to")
        if not f or not t:
            return parse_range({"range":"today"})
        start = datetime.fromisoformat(f).replace(tzinfo=timezone.utc)
        end = datetime.fromisoformat(t).replace(tzinfo=timezone.utc) + timedelta(days=1)
    return start, end

def sanitize_email(raw: str) -> str:
    """Normalize an email address by stripping whitespace and lowercasing.

    Used across registration, login, and invitation flows to ensure
    consistent email comparison and storage.
    """
    return raw.strip().lower()


def user_project_ids(db, org_id: int, user_id: int) -> set[int]:
    """Return the set of project IDs the user is assigned to in the given org."""
    ids = [r.project_id for r in db.query(Assignment).filter_by(org_id=org_id, user_id=user_id).all()]
    return set(ids)

# -------- Activity builders --------
def build_activities(db, org_id, user_id, start, end, allow_projects: set, limit=1000):
    """
    প্রতি session = একেকটা row (Activity Log style)
    allow_projects = যে প্রজেক্টগুলোর ডেটা দেখা যাবে (Assignment অনুযায়ী)
    SQL-এ time ফিল্টার করবো না; পাইথনে clamping করবো → timezone সমস্যা এড়াতে।
    """
    q = (db.query(TimeSession, Task, User)
           .join(Task, TimeSession.task_id==Task.id)
           .join(User, TimeSession.user_id==User.id)
           .filter(TimeSession.org_id==org_id, TimeSession.user_id==user_id))

    if allow_projects:
        q = q.filter(Task.project_id.in_(list(allow_projects)))

    rows = q.order_by(TimeSession.id.desc()).limit(limit).all()

    out = []
    for s, t, u in rows:
        s_start = as_aware(s.started_at)
        s_end = as_aware(s.ended_at) or now_utc()
        st = max(s_start, start); en = min(s_end, end)
        dur = max(0, int((en - st).total_seconds()))
        if dur <= 0: continue

        ss_cnt = db.query(Heartbeat).filter(
            Heartbeat.org_id==org_id,
            Heartbeat.session_id==s.id,
            Heartbeat.screenshot_path!=None
        ).count()

        local_st = st.astimezone(timezone.utc); local_en = en.astimezone(timezone.utc)
        date_str = local_st.strftime("%b %d")
        time_range = f"{local_st.strftime('%I:%M%p').lstrip('0').lower()} – {local_en.strftime('%I:%M%p').lstrip('0').lower()}"

        out.append({
            "date": date_str,
            "time_range": time_range,
            "activity": t.name,
            "project": db.query(Project).filter_by(id=t.project_id).first().name if t.project_id else "-",
            "user_email": u.email,
            "duration_secs": dur,
            "screenshots": ss_cnt,
            "session_id": s.id
        })
    return out

def sum_session_seconds(db, org_id, user_id, start, end, allow_projects: set) -> int:
    """Sum the clamped duration (in seconds) of all matching time sessions."""
    total = 0
    q = db.query(TimeSession, Task).join(Task, TimeSession.task_id==Task.id)\
        .filter(TimeSession.org_id==org_id, TimeSession.user_id==user_id)
    if allow_projects:
        q = q.filter(Task.project_id.in_(list(allow_projects)))
    for s, t in q.all():
        s_start = as_aware(s.started_at)
        s_end = as_aware(s.ended_at) or now_utc()
        st = max(s_start, start); en = min(s_end, end)
        total += max(0, int((en - st).total_seconds()))
    return total

def activity_snapshot(db, org_id, user_id, start, end, allow_projects: set, limit_ss=8):
    """Build a summary snapshot of a user's activity within a time range.

    Returns total tracked seconds, the last-seen heartbeat timestamp,
    aggregate key/mouse counts, and up to `limit_ss` recent screenshot
    URLs, all scoped to `allow_projects` when provided.
    """
    total_secs = sum_session_seconds(db, org_id, user_id, start, end, allow_projects)
    last_hb = db.query(Heartbeat).join(TimeSession, Heartbeat.session_id==TimeSession.id)\
        .join(Task, TimeSession.task_id==Task.id)\
        .filter(Heartbeat.org_id==org_id, TimeSession.user_id==user_id)\
        .filter(Task.project_id.in_(list(allow_projects)) if allow_projects else True)\
        .order_by(Heartbeat.id.desc()).first()
    last_seen = as_aware(last_hb.ts) if last_hb else None

    hb_rows = db.query(Heartbeat).join(TimeSession, Heartbeat.session_id==TimeSession.id)\
        .join(Task, TimeSession.task_id==Task.id)\
        .filter(Heartbeat.org_id==org_id, TimeSession.user_id==user_id)\
        .filter(Task.project_id.in_(list(allow_projects)) if allow_projects else True).all()
    k_sum = sum(h.key_count for h in hb_rows); m_sum = sum(h.mouse_count for h in hb_rows)

    ss = db.query(Heartbeat).join(TimeSession, Heartbeat.session_id==TimeSession.id)\
        .join(Task, TimeSession.task_id==Task.id)\
        .filter(Heartbeat.org_id==org_id, TimeSession.user_id==user_id, Heartbeat.screenshot_path!=None)\
        .filter(Task.project_id.in_(list(allow_projects)) if allow_projects else True)\
        .order_by(Heartbeat.id.desc()).limit(limit_ss).all()
    ss_items = [{"ts": h.ts, "url": url_for("serve_upload", filename=os.path.basename(h.screenshot_path))} for h in ss]

    return {"total_secs": total_secs, "last_seen": last_seen, "key_sum": k_sum, "mouse_sum": m_sum, "screenshots": ss_items}

# ---------------- WEB auth helpers ----------------
def web_login_required(view):
    """Route decorator that redirects to the login page unless a user is signed in.

    Checks for "uid" in the Flask session, mirroring the JWT-based
    admin_required check used by the API routes.
    """
    @wraps(view)
    def w(*a, **k):
        if "uid" not in session:
            return redirect(url_for("web_login"))
        return view(*a, **k)
    return w

# ---------------- WEB pages ----------------
@app.route("/")
@web_login_required
def dashboard():
    db = SessionLocal()
    try:
        uid = session["uid"]; org_id = session["org_id"]; role = session["role"]
        start, end = parse_range(request.args); rng = request.args.get("range") or "today"

        # আমার নিজের জন্য allowed projects
        allow = user_project_ids(db, org_id, uid)
        me = activity_snapshot(db, org_id, uid, start, end, allow, limit_ss=10)

        team_cards = []
        projects = []
        if role == "admin":
            members = db.query(Membership).filter(Membership.org_id==org_id).all()
            for mem in members:
                allow_m = user_project_ids(db, org_id, mem.user_id)  # মেম্বারের assigned projects
                info = activity_snapshot(db, org_id, mem.user_id, start, end, allow_m, limit_ss=6)
                team_cards.append({"user_id": mem.user_id, "email": mem.user.email, "role": mem.role, **info})
            projects = db.query(Project).filter_by(org_id=org_id).order_by(Project.name.asc()).all()

        def link_range(preset): return f"/?{urlencode({'range': preset})}"

        return render_template("dashboard.html",
                               role=role, me=me, team_cards=team_cards,
                               range_sel=rng, link_range=link_range,
                               start=start, end=end - timedelta(seconds=1),
                               projects=projects)
    finally:
        db.close()

@app.route("/register", methods=["GET","POST"])
def web_register():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        pw = request.form["password"]; full = request.form.get("full_name","")
        db = SessionLocal()
        try:
            if db.query(User).filter_by(email=email).first():
                flash("Email already registered.", "danger"); return redirect(url_for("web_register"))
            org = Organization(name=f"{email.split('@')[0]}'s team"); db.add(org); db.flush()
            user = User(email=email, password_hash=pw_hash(pw), full_name=full, default_org_id=org.id)
            db.add(user); db.flush()
            db.add(Membership(user_id=user.id, org_id=org.id, role="admin"))

            # ডিফল্ট Project + Assignment (admin সব প্রজেক্টে থাকুক)
            proj = Project(org_id=org.id, name="General"); db.add(proj); db.flush()
            db.add(Assignment(org_id=org.id, user_id=user.id, project_id=proj.id))

            db.commit()
            session.update({"uid": user.id, "org_id": org.id, "role": "admin"})
            return redirect(url_for("dashboard"))
        finally: db.close()
    return render_template("register.html")

@app.route("/login", methods=["GET","POST"])
def web_login():
    if request.method == "POST":
        email = request.form["email"].strip().lower(); pw = request.form["password"]
        db = SessionLocal()
        try:
            u = db.query(User).filter_by(email=email).first()
            if not u or not pw_ok(pw, u.password_hash):
                flash("Invalid credentials.", "danger"); return redirect(url_for("web_login"))
            org_id = u.default_org_id
            mem = db.query(Membership).filter_by(user_id=u.id, org_id=org_id).first()
            role = mem.role if mem else "member"
            session.update({"uid": u.id, "org_id": org_id, "role": role})
            return redirect(url_for("dashboard"))
        finally: db.close()
    return render_template("login.html")

@app.route("/logout")
def web_logout():
    session.clear(); return redirect(url_for("web_login"))

# ---- Projects (admin) ----
@app.post("/projects/create")
@web_login_required
def web_create_project():
    if session.get("role") != "admin": abort(403)
    name = request.form.get("name","").strip()
    if not name: return redirect(url_for("dashboard"))
    db = SessionLocal()
    try:
        org_id = session["org_id"]
        if db.query(Project).filter_by(org_id=org_id, name=name).first():
            flash("Project already exists.", "warning")
        else:
            db.add(Project(org_id=org_id, name=name)); db.commit()
            flash("Project created.", "success")
    finally: db.close()
    return redirect(url_for("dashboard"))

# ---- Invite (admin) with project ----
@app.post("/invite")
@web_login_required
def web_invite():
    if session.get("role") != "admin": abort(403)
    email = request.form["email"].strip().lower()
    project_id = int(request.form["project_id"])
    token = secrets.token_urlsafe(24)
    db = SessionLocal()
    try:
        org_id = session["org_id"]
        if not db.query(Project).filter_by(id=project_id, org_id=org_id).first():
            flash("Invalid project.", "danger"); return redirect(url_for("dashboard"))
        inv = Invitation(org_id=org_id, email=email, token=token, project_id=project_id, accepted=False)
        db.add(inv); db.commit()
        flash(f"Invite link: {request.host_url.rstrip('/')}/accept-invite?token={token}", "success")
    finally: db.close()
    return redirect(url_for("dashboard"))

@app.route("/accept-invite", methods=["GET","POST"])
def web_accept_invite():
    token = request.args.get("token") or request.form.get("token","")
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        db = SessionLocal()
        try:
            inv = db.query(Invitation).filter_by(token=token, accepted=False).first()
            if not inv or inv.email != email:
                flash("Invalid invite token or email mismatch.", "danger")
                return redirect(url_for("web_accept_invite", token=token))
            user = db.query(User).filter_by(email=email).first()
            if not user:
                flash("User not found. Please register first.", "warning")
                return redirect(url_for("web_register"))
            # assignment create
            if not db.query(Assignment).filter_by(org_id=inv.org_id, user_id=user.id, project_id=inv.project_id).first():
                db.add(Assignment(org_id=inv.org_id, user_id=user.id, project_id=inv.project_id))
            if not user.default_org_id: user.default_org_id = inv.org_id
            inv.accepted = True
            db.commit()
            flash("Invitation accepted. You can login now.", "success")
            return redirect(url_for("web_login"))
        finally: db.close()
    return render_template("accept_invite.html", token=token)

# ---- Member self activity ----
@app.route("/activity")
@web_login_required
def web_activity():
    db = SessionLocal()
    try:
        uid = session["uid"]; org_id = session["org_id"]
        start, end = parse_range(request.args); rng = request.args.get("range") or "today"
        allow = user_project_ids(db, org_id, uid)
        items = build_activities(db, org_id, uid, start, end, allow)
        return render_template("activity.html", title="My Activity", items=items,
                               show_member=False, range_sel=rng, start=start, end=end - timedelta(seconds=1))
    finally: db.close()

# ---- Admin: members list + profile activity ----
@app.route("/admin/members")
@web_login_required
def web_admin_members():
    if session.get("role") != "admin": abort(403)
    db = SessionLocal()
    try:
        org_id = session["org_id"]
        members = db.query(Membership).filter_by(org_id=org_id).all()
        return render_template("members.html", members=members)
    finally: db.close()

@app.route("/admin/member/<int:uid>")
@web_login_required
def web_admin_member(uid: int):
    if session.get("role") != "admin": abort(403)
    db = SessionLocal()
    try:
        org_id = session["org_id"]
        start, end = parse_range(request.args); rng = request.args.get("range") or "today"
        mem = db.query(Membership).filter_by(org_id=org_id, user_id=uid).first()
        if not mem: abort(404)
        allow = user_project_ids(db, org_id, uid)  # member's assigned projects only
        items = build_activities(db, org_id, uid, start, end, allow)
        ss = db.query(Heartbeat).join(TimeSession, Heartbeat.session_id==TimeSession.id)\
             .join(Task, TimeSession.task_id==Task.id)\
             .filter(Heartbeat.org_id==org_id, TimeSession.user_id==uid, Heartbeat.screenshot_path!=None)\
             .filter(Task.project_id.in_(list(allow)) if allow else True)\
             .order_by(Heartbeat.id.desc()).limit(12).all()
        gallery = [url_for("serve_upload", filename=os.path.basename(h.screenshot_path)) for h in ss]
        return render_template("member_activity.html", member=mem.user, items=items, gallery=gallery,
                               range_sel=rng, start=start, end=end - timedelta(seconds=1))
    finally: db.close()

# ---- CSV Export (self or admin for any user; project-scope respected) ----
@app.route("/export.csv")
@web_login_required
def export_csv():
    db = SessionLocal()
    try:
        me_id = session["uid"]; org_id = session["org_id"]; role = session["role"]
        user_id = request.args.get("user_id", type=int) or me_id
        if role != "admin" and user_id != me_id: abort(403)
        start, end = parse_range(request.args)
        allow = user_project_ids(db, org_id, user_id)
        items = build_activities(db, org_id, user_id, start, end, allow, limit=5000)

        sio = StringIO()
        w = csv.writer(sio)
        w.writerow(["Date","Time","Activity Name","Project","Member","Duration (h:m:s)","Screenshots"])
        for r in items:
            w.writerow([r["date"], r["time_range"], r["activity"], r["project"], r["user_email"], format_duration(r["duration_secs"]), r["screenshots"]])
        output = sio.getvalue()
        filename = f"activity_{user_id}_{int(time.time())}.csv"
        return Response(output, mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment;filename={filename}"})
    finally: db.close()

# ---- serve uploads ----
@app.get("/uploads/<path:filename>")
def serve_upload(filename):
    return send_from_directory(UPLOAD_FOLDER, filename, as_attachment=False)

# ---------------- API (Desktop) ----------------
@app.post("/auth/register")
def api_register():
    data = request.json or {}
    email = data.get("email","").strip().lower(); pw = data.get("password",""); full = data.get("full_name","")
    if not email or not pw: return jsonify({"error":"email/password required"}), 400
    db = SessionLocal()
    try:
        if db.query(User).filter_by(email=email).first(): return jsonify({"error":"Email exists"}), 400
        org = Organization(name=f"{email.split('@')[0]}'s team"); db.add(org); db.flush()
        user = User(email=email, password_hash=pw_hash(pw), full_name=full, default_org_id=org.id)
        db.add(user); db.flush()
        db.add(Membership(user_id=user.id, org_id=org.id, role="admin"))
        proj = Project(org_id=org.id, name="General"); db.add(proj); db.flush()
        db.add(Assignment(org_id=org.id, user_id=user.id, project_id=proj.id))
        db.commit()
        return jsonify({"status":"ok"})
    finally: db.close()

@app.post("/auth/login")
def api_login():
    data = request.json or {}
    email = data.get("email","").strip().lower(); pw = data.get("password","")
    db = SessionLocal()
    try:
        u = db.query(User).filter_by(email=email).first()
        if not u or not pw_ok(pw, u.password_hash): return jsonify({"error":"bad credentials"}), 401
        org_id = u.default_org_id
        mem = db.query(Membership).filter_by(user_id=u.id, org_id=org_id).first()
        role = mem.role if mem else "member"
        token = create_access_token(identity=str(u.id), additional_claims={"org_id": org_id, "role": role})
        return jsonify({"access_token": token, "user":{"id":u.id,"email":u.email,"org_id":org_id,"role":role}})
    finally: db.close()

@app.get("/tasks")
@jwt_required()
def api_tasks_list():
    uid, org_id, role = current_context()
    db = SessionLocal()
    try:
        if role == "admin":
            rows = db.query(Task).filter(Task.org_id==org_id).order_by(Task.id.desc()).all()
        else:
            allow = user_project_ids(db, org_id, uid)
            rows = db.query(Task).filter(Task.org_id==org_id, Task.project_id.in_(list(allow))).order_by(Task.id.desc()).all()
        return jsonify([{"id":t.id,"name":t.name,"project_id":t.project_id} for t in rows])
    finally: db.close()

@app.post("/tasks")
@jwt_required()
def api_tasks_create():
    uid, org_id, role = current_context()
    payload = request.json or {}
    name = (payload.get("name") or "").strip()
    project_id = payload.get("project_id", None)
    if not name: return jsonify({"error":"name required"}), 400
    db = SessionLocal()
    try:
        if role != "admin":
            # member must be assigned to the chosen project
            allow = user_project_ids(db, org_id, uid)
            if project_id is None:
                project_id = next(iter(allow), None)
            if not project_id or project_id not in allow:
                return jsonify({"error":"no access to project"}), 403
        else:
            # admin: default first project if not provided
            if project_id is None:
                first = db.query(Project).filter_by(org_id=org_id).first()
                if not first:  # create default
                    first = Project(org_id=org_id, name="General"); db.add(first); db.flush()
                project_id = first.id
        t = Task(org_id=org_id, project_id=project_id, user_id=uid, name=name)
        db.add(t); db.commit()
        return jsonify({"id":t.id,"name":t.name,"project_id":project_id})
    finally: db.close()

@app.post("/sessions/start")
@jwt_required()
def api_sessions_start():
    """Start a new time session for the caller, closing any session left open."""
    uid, org_id, role = current_context()
    task_id = int((request.json or {}).get("task_id",0))
    db = SessionLocal()
    try:
        t = db.query(Task).filter(Task.id==task_id, Task.org_id==org_id).first()
        if not t: return jsonify({"error":"bad task"}), 400
        if role != "admin":
            allow = user_project_ids(db, org_id, uid)
            if t.project_id not in allow: return jsonify({"error":"no access to project"}), 403
        open_s = db.query(TimeSession).filter_by(user_id=uid, org_id=org_id, ended_at=None).first()
        if open_s: open_s.ended_at = now_utc()
        s = TimeSession(org_id=org_id, user_id=uid, task_id=task_id, started_at=now_utc())
        db.add(s); db.commit()
        return jsonify({"session_id": s.id, "started_at": s.started_at.isoformat()})
    finally: db.close()

@app.post("/sessions/stop")
@jwt_required()
def api_sessions_stop():
    """Stop the caller's currently open time session, if any."""
    uid, org_id, _ = current_context()
    db = SessionLocal()
    try:
        s = db.query(TimeSession).filter_by(user_id=uid, org_id=org_id, ended_at=None).first()
        if not s: return jsonify({"message":"no open session; already stopped"}), 200
        s.ended_at = now_utc(); db.commit()
        return jsonify({"session_id": s.id, "ended_at": s.ended_at.isoformat()})
    finally: db.close()

@app.post("/heartbeats")
@jwt_required()
def api_heartbeats():
    uid, org_id, role = current_context()
    form = request.form; sid = int(form.get("session_id",0))
    db = SessionLocal()
    try:
        s = db.query(TimeSession).filter_by(id=sid, org_id=org_id).first()
        if not s: return jsonify({"error":"bad session"}), 400
        t = db.query(Task).filter_by(id=s.task_id).first()
        if role != "admin":
            allow = user_project_ids(db, org_id, uid)
            if t.project_id not in allow: return jsonify({"error":"no access to project"}), 403

        hb = Heartbeat(org_id=org_id, session_id=sid,
                       key_count=int(form.get("key_count",0)),
                       mouse_count=int(form.get("mouse_count",0)),
                       is_idle=to_bool(form.get("is_idle", "false")),
                       note=form.get("note",""))
        if "screenshot" in request.files:
            f = request.files["screenshot"]
            fname = secure_filename(f"ss_{uid}_{int(time.time())}.png")
            path = os.path.join(UPLOAD_FOLDER, fname); f.save(path)
            hb.screenshot_path = path
        db.add(hb); db.commit()
        return jsonify({"id": hb.id})
    finally: db.close()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
