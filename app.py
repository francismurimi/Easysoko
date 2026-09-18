from pathlib import Path
import base64
import importlib.util
import io
import os
import shutil
import sys
import zipfile

# Vercel entrypoint for the recovered Easy Soko application.
ROOT = Path(__file__).resolve().parent
RECOVERY_DIR = ROOT / "recovery"
REPAIR_DIR = ROOT / "repair"
RUNTIME_DIR = Path("/tmp/easysoko_runtime")
READY_MARKER = RUNTIME_DIR / ".ready"

# These four chunks were damaged during the original GitHub transfer.
# Verified replacement halves are stored under repair/.
REPAIRED_CHUNKS = {"04", "05", "06", "08"}


def _read_chunk(number):
    if number in REPAIRED_CHUNKS:
        first = (REPAIR_DIR / f"{number}a").read_text(encoding="utf-8").strip()
        second = (REPAIR_DIR / f"{number}b").read_text(encoding="utf-8").strip()
        return first + second
    return (RECOVERY_DIR / f"chunk{number}").read_text(encoding="utf-8").strip()


def _restore_recovered_project():
    app_file = RUNTIME_DIR / "app.py"

    if READY_MARKER.exists() and app_file.exists():
        return app_file

    if RUNTIME_DIR.exists():
        shutil.rmtree(RUNTIME_DIR)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    chunks = [_read_chunk(f"{i:02d}") for i in range(12)]
    expected_lengths = [8000] * 11 + [388]
    actual_lengths = [len(chunk) for chunk in chunks]

    if actual_lengths != expected_lengths:
        raise RuntimeError(
            f"Easy Soko recovery payload has invalid chunk lengths: {actual_lengths}"
        )

    encoded = "".join(chunks)
    archive_bytes = base64.b64decode(encoded, validate=True)

    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        bad_file = archive.testzip()
        if bad_file is not None:
            raise RuntimeError(f"Recovered archive contains a corrupt file: {bad_file}")
        archive.extractall(RUNTIME_DIR)

    if not app_file.exists():
        raise RuntimeError("Recovered Easy Soko app.py was not found after extraction.")

    READY_MARKER.write_text("ok", encoding="utf-8")
    return app_file


_recovered_app_file = _restore_recovered_project()

# Adapt the recovered application for a persistent Vercel PostgreSQL database.
# The original project used a local SQLite file, which is ephemeral on Vercel.
_source = _recovered_app_file.read_text(encoding="utf-8")

_old_db = "app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///marketplace.db'"
_new_db = """database_url = os.environ.get('DATABASE_URL') or os.environ.get('POSTGRES_URL')
if database_url and database_url.startswith('postgres://'):
    database_url = 'postgresql://' + database_url[len('postgres://'):]
app.config['SQLALCHEMY_DATABASE_URI'] = database_url or 'sqlite:////tmp/marketplace.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True}"""
if _old_db in _source:
    _source = _source.replace(_old_db, _new_db, 1)

# Seller-created items should be visible in Browse immediately.
_old_item_default = "approval_status = db.Column(db.String(20), default='pending')  # pending, approved, rejected\n\nclass Cart"
_new_item_default = "approval_status = db.Column(db.String(20), default='approved')  # visible immediately\n\nclass Cart"
if _old_item_default in _source:
    _source = _source.replace(_old_item_default, _new_item_default, 1)

_old_new_item = "image_url=final_image_url, \n            seller_id=current_user.id\n        )"
_new_new_item = "image_url=final_image_url, \n            seller_id=current_user.id,\n            approval_status='approved'\n        )"
if _old_new_item in _source:
    _source = _source.replace(_old_new_item, _new_new_item, 1)

# Keep normal users authenticated across navigation.
_source = _source.replace(
    "from datetime import datetime",
    "from datetime import datetime, timedelta",
    1,
)

_old_secret = "app.secret_key = os.environ.get('EASY_SOKO_SECRET_KEY', 'dev-change-this-secret')"
_new_secret = """app.secret_key = os.environ.get('EASY_SOKO_SECRET_KEY', 'dev-change-this-secret')
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = bool(os.environ.get('VERCEL'))
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
app.config['REMEMBER_COOKIE_DURATION'] = timedelta(days=30)
app.config['REMEMBER_COOKIE_HTTPONLY'] = True
app.config['REMEMBER_COOKIE_SAMESITE'] = 'Lax'
app.config['REMEMBER_COOKIE_SECURE'] = bool(os.environ.get('VERCEL'))"""
if _old_secret in _source:
    _source = _source.replace(_old_secret, _new_secret, 1)

_old_loader = """@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))"""
_new_loader = """@login_manager.user_loader
def load_user(user_id):
    try:
        return db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None"""
if _old_loader in _source:
    _source = _source.replace(_old_loader, _new_loader, 1)

_old_login = """@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user = User.query.filter_by(username=username).first()
        if user and user.password == password:
            login_user(user)
            return redirect(url_for('dashboard'))
        flash('Invalid credentials')
    return render_template('login.html')"""

_new_login = """@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        identity = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        user = User.query.filter(
            (db.func.lower(User.username) == identity.lower()) |
            (db.func.lower(User.email) == identity.lower())
        ).first()

        if user and user.password == password:
            login_user(user, remember=True, duration=timedelta(days=30), fresh=True)
            session.permanent = True
            session.modified = True
            return redirect(url_for('dashboard'))

        flash('Invalid username/email or password.')

    return render_template('login.html')"""
if _old_login in _source:
    _source = _source.replace(_old_login, _new_login, 1)

_old_signup_start = """@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':"""
_new_signup_start = """@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':"""
if _old_signup_start in _source:
    _source = _source.replace(_old_signup_start, _new_signup_start, 1)

# Normalize signup username/email so accidental spaces and email casing do not break later login.
_source = _source.replace(
    "        username = request.form['username']\n        email = request.form['email']\n        password = request.form['password']",
    "        username = request.form.get('username', '').strip()\n        email = request.form.get('email', '').strip().lower()\n        password = request.form.get('password', '')",
    1,
)

_old_signup_end = """        db.session.add(new_user)
        db.session.commit()
        flash('Account created successfully! Please log in.')
        return redirect(url_for('login'))
    return render_template('signup.html')"""
_new_signup_end = """        db.session.add(new_user)
        db.session.commit()

        login_user(new_user, remember=True, duration=timedelta(days=30), fresh=True)
        session.permanent = True
        session.modified = True
        flash('Account created successfully. You are now signed in.')
        return redirect(url_for('dashboard'))

    return render_template('signup.html')"""
if _old_signup_end in _source:
    _source = _source.replace(_old_signup_end, _new_signup_end, 1)

_recovered_app_file.write_text(_source, encoding="utf-8")
os.chdir(RUNTIME_DIR)

spec = importlib.util.spec_from_file_location("easysoko_recovered", _recovered_app_file)
if spec is None or spec.loader is None:
    raise RuntimeError("Unable to load the recovered Easy Soko Flask application.")

module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

# Vercel detects this top-level Flask object.
app = module.app

# The recovered app currently uses SQLite. Vercel's filesystem is ephemeral,
# so this is suitable only for the deployed demo until PostgreSQL is added.
try:
    with app.app_context():
        module.db.create_all()
        module.add_initial_categories()
        module.add_admin_user()
        module.add_demo_user()
        module.add_demo_advertisements()
        module.add_demo_items()
except Exception:
    app.logger.exception("Easy Soko startup database initialization failed")
