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

# ---------------------------------------------------------------------------
# Responsive phone + tablet UI
# ---------------------------------------------------------------------------
_templates_dir = RUNTIME_DIR / "templates"
_viewport = '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'

# Make every page render at the correct phone/tablet width.
for _template_file in _templates_dir.glob("*.html"):
    _html = _template_file.read_text(encoding="utf-8")
    if 'name="viewport"' not in _html:
        if '<meta charset="UTF-8">' in _html:
            _html = _html.replace(
                '<meta charset="UTF-8">',
                '<meta charset="UTF-8">\\n    ' + _viewport,
                1,
            )
        elif '<meta charset="utf-8">' in _html:
            _html = _html.replace(
                '<meta charset="utf-8">',
                '<meta charset="utf-8">\\n    ' + _viewport,
                1,
            )
    _template_file.write_text(_html, encoding="utf-8")

# A compact desktop/tablet navbar plus a thumb-friendly phone bottom bar.
_navbar_path = _templates_dir / "navbar.html"
_navbar_path.write_text(r'''<nav class="navbar navbar-expand-lg sticky-top easy-navbar">
  <div class="container-fluid easy-navbar-inner">
    <a class="navbar-brand brand-text" href="/">Easy Soko</a>

    <button class="navbar-toggler easy-menu-toggle" type="button"
            data-bs-toggle="collapse" data-bs-target="#navbarNav"
            aria-controls="navbarNav" aria-expanded="false"
            aria-label="Open navigation">
      <span aria-hidden="true">☰</span>
    </button>

    <div class="collapse navbar-collapse" id="navbarNav">
      <ul class="navbar-nav me-auto mb-2 mb-lg-0">
        <li class="nav-item"><a class="nav-link {% if request.path.startswith('/browse') %}active{% endif %}" href="/browse">Browse</a></li>

        {% if current_user.is_authenticated %}
          <li class="nav-item"><a class="nav-link {% if request.path == '/dashboard' %}active{% endif %}" href="/dashboard">Dashboard</a></li>
          <li class="nav-item"><a class="nav-link {% if request.path.startswith('/sell') %}active{% endif %}" href="/sell">Sell</a></li>
          <li class="nav-item"><a class="nav-link {% if request.path == '/orders' %}active{% endif %}" href="/orders">Orders</a></li>
          <li class="nav-item"><a class="nav-link {% if request.path == '/cart' %}active{% endif %}" href="/cart">Cart</a></li>
          <li class="nav-item"><a class="nav-link {% if request.path == '/wishlist' %}active{% endif %}" href="/wishlist">Wishlist</a></li>
          <li class="nav-item"><a class="nav-link {% if request.path == '/profile' %}active{% endif %}" href="/profile">Profile</a></li>
          <li class="nav-item"><a class="nav-link {% if request.path == '/history' %}active{% endif %}" href="/history">History</a></li>
        {% else %}
          <li class="nav-item"><a class="nav-link {% if request.path == '/login' %}active{% endif %}" href="/login">Login</a></li>
          <li class="nav-item"><a class="nav-link {% if request.path == '/signup' %}active{% endif %}" href="/signup">Sign Up</a></li>
        {% endif %}

        <li class="nav-item"><a class="nav-link {% if request.path == '/contact' %}active{% endif %}" href="/contact">Help / About</a></li>
      </ul>

      {% if current_user.is_authenticated %}
        <a class="btn btn-outline-danger btn-sm easy-logout" href="/logout">Logout</a>
      {% endif %}
    </div>
  </div>
</nav>

<nav class="easy-mobile-nav" aria-label="Mobile navigation">
  {% if current_user.is_authenticated %}
    <a href="/dashboard" class="{% if request.path == '/dashboard' %}active{% endif %}">
      <span class="easy-mobile-icon">⌂</span><span>Home</span>
    </a>
    <a href="/browse" class="{% if request.path.startswith('/browse') or request.path.startswith('/item/') %}active{% endif %}">
      <span class="easy-mobile-icon">⌕</span><span>Browse</span>
    </a>
    <a href="/sell" class="easy-mobile-sell {% if request.path.startswith('/sell') %}active{% endif %}">
      <span class="easy-mobile-icon">＋</span><span>Sell</span>
    </a>
    <a href="/cart" class="{% if request.path == '/cart' %}active{% endif %}">
      <span class="easy-mobile-icon">▣</span><span>Cart</span>
    </a>
    <a href="/profile" class="{% if request.path == '/profile' or request.path == '/update_profile' %}active{% endif %}">
      <span class="easy-mobile-icon">●</span><span>Profile</span>
    </a>
  {% else %}
    <a href="/" class="{% if request.path == '/' %}active{% endif %}">
      <span class="easy-mobile-icon">⌂</span><span>Home</span>
    </a>
    <a href="/browse" class="{% if request.path.startswith('/browse') or request.path.startswith('/item/') %}active{% endif %}">
      <span class="easy-mobile-icon">⌕</span><span>Browse</span>
    </a>
    <a href="/login" class="{% if request.path == '/login' %}active{% endif %}">
      <span class="easy-mobile-icon">→</span><span>Login</span>
    </a>
    <a href="/signup" class="{% if request.path == '/signup' %}active{% endif %}">
      <span class="easy-mobile-icon">＋</span><span>Sign Up</span>
    </a>
  {% endif %}
</nav>''', encoding="utf-8")

# Append responsive overrides without changing the desktop visual identity.
_css_path = RUNTIME_DIR / "static" / "css" / "style.css"
_mobile_css_marker = "/* EASY_SOKO_MOBILE_UI_V2 */"
if _css_path.exists():
    _css = _css_path.read_text(encoding="utf-8")
    if _mobile_css_marker not in _css:
        _css += r'''

/* EASY_SOKO_MOBILE_UI_V2 */
:root {
    --easy-touch: 46px;
    --easy-mobile-nav-h: 68px;
}

html {
    max-width: 100%;
    overflow-x: hidden;
    -webkit-text-size-adjust: 100%;
}

body {
    max-width: 100%;
    overflow-x: hidden;
}

img,
video {
    max-width: 100%;
}

.easy-mobile-nav {
    display: none;
}

.easy-menu-toggle {
    border: 0 !important;
    box-shadow: none !important;
    color: #16324f !important;
    font-size: 1.55rem;
    line-height: 1;
    padding: .35rem .6rem !important;
}

.form-control,
.form-select,
.input-group-text {
    min-height: var(--easy-touch);
}

.btn {
    min-height: 42px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
}

.card-body {
    min-width: 0;
}

.card-text,
p,
td,
th {
    overflow-wrap: anywhere;
}

.table-responsive {
    -webkit-overflow-scrolling: touch;
}

/* Tablet */
@media (min-width: 768px) and (max-width: 991.98px) {
    .container {
        width: calc(100% - 32px);
        max-width: none;
        padding: 24px;
    }

    .brand-text {
        font-size: 1.9rem !important;
    }

    .navbar-collapse {
        margin-top: .65rem;
        padding: .75rem;
        background: rgba(255,255,255,.92);
        border-radius: 14px;
        box-shadow: 0 12px 30px rgba(0,0,0,.10);
    }

    .navbar-nav .nav-link {
        padding: 11px 14px !important;
    }

    .row > .col-md-4 {
        flex: 0 0 50%;
        max-width: 50%;
    }

    .advertisement-slider {
        height: 300px;
    }

    form[style*="max-width"],
    .container[style*="max-width"] {
        max-width: 720px !important;
    }
}

/* Phone */
@media (max-width: 767.98px) {
    body {
        padding-bottom: calc(var(--easy-mobile-nav-h) + env(safe-area-inset-bottom) + 8px);
        font-size: 15px;
    }

    .easy-navbar {
        position: sticky;
        top: 0;
        z-index: 1030;
        padding: .38rem .25rem;
    }

    .easy-navbar-inner {
        padding-left: .55rem !important;
        padding-right: .55rem !important;
    }

    .brand-text {
        font-size: 1.55rem !important;
        line-height: 1.2;
    }

    .brand-text:hover,
    .navbar-brand:hover,
    .nav-link:hover {
        transform: none !important;
        font-size: inherit !important;
        filter: none !important;
    }

    .navbar-collapse {
        max-height: calc(100vh - 145px);
        overflow-y: auto;
        margin-top: .45rem;
        padding: .6rem;
        background: rgba(255,255,255,.97);
        border-radius: 12px;
        box-shadow: 0 10px 26px rgba(0,0,0,.12);
    }

    .navbar-nav .nav-link {
        padding: 12px 14px !important;
        margin: 2px 0;
    }

    .easy-logout {
        width: 100%;
        margin-top: .4rem;
    }

    .easy-mobile-nav {
        position: fixed;
        display: grid;
        grid-auto-flow: column;
        grid-auto-columns: 1fr;
        left: 8px;
        right: 8px;
        bottom: calc(8px + env(safe-area-inset-bottom));
        min-height: var(--easy-mobile-nav-h);
        z-index: 1050;
        background: rgba(255,255,255,.97);
        border: 1px solid rgba(30,58,138,.12);
        border-radius: 18px;
        box-shadow: 0 10px 32px rgba(20,45,80,.20);
        backdrop-filter: blur(14px);
        overflow: visible;
    }

    .easy-mobile-nav a {
        color: #52606d;
        text-decoration: none;
        min-width: 0;
        min-height: 60px;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        gap: 1px;
        padding: 7px 4px;
        font-size: .69rem;
        font-weight: 700;
        border-radius: 15px;
        -webkit-tap-highlight-color: transparent;
    }

    .easy-mobile-nav a.active {
        color: #123f75;
        background: rgba(83,223,209,.16);
    }

    .easy-mobile-icon {
        display: block;
        font-size: 1.28rem;
        line-height: 1.05;
        font-weight: 800;
    }

    .easy-mobile-nav .easy-mobile-sell {
        align-self: center;
        min-height: 58px;
        margin-top: -16px;
        color: #123f75;
        background: linear-gradient(135deg, #53dfd1, #9de3f6);
        border: 4px solid rgba(255,255,255,.98);
        border-radius: 50%;
        box-shadow: 0 8px 20px rgba(36,145,165,.28);
    }

    .easy-mobile-nav .easy-mobile-sell .easy-mobile-icon {
        font-size: 1.65rem;
    }

    .container,
    section.container {
        width: calc(100% - 16px) !important;
        max-width: none !important;
        margin-left: 8px !important;
        margin-right: 8px !important;
        margin-top: 12px !important;
        margin-bottom: 12px !important;
        padding: 16px !important;
        border-radius: 14px;
    }

    .container-fluid {
        max-width: 100%;
    }

    h1 {
        font-size: 1.75rem;
        line-height: 1.2;
    }

    h2 {
        font-size: 1.45rem;
        line-height: 1.25;
    }

    h3 {
        font-size: 1.25rem;
    }

    .lead {
        font-size: 1rem;
    }

    .row {
        --bs-gutter-x: .8rem;
    }

    .row > [class*="col-"] {
        min-width: 0;
    }

    .card {
        border-radius: 14px;
        overflow: hidden;
        margin-bottom: 12px;
    }

    .card:hover,
    .list-group-item:hover,
    .btn:hover {
        transform: none !important;
    }

    .card-body {
        padding: 14px;
    }

    .card-img-top {
        height: 180px !important;
        object-fit: cover;
    }

    .form-label {
        font-weight: 700;
        margin-bottom: .35rem;
    }

    .form-control,
    .form-select {
        width: 100%;
        min-height: 48px;
        font-size: 16px !important; /* prevents iOS input zoom */
        border-radius: 10px;
    }

    textarea.form-control {
        min-height: 110px;
    }

    input[type="file"].form-control {
        padding-top: .65rem;
    }

    .form-text {
        font-size: .78rem;
        line-height: 1.35;
    }

    .input-group {
        display: grid !important;
        grid-template-columns: 1fr;
        gap: 8px;
    }

    .input-group > .form-control,
    .input-group > .form-select,
    .input-group > .btn,
    .input-group > .input-group-text {
        width: 100% !important;
        max-width: none !important;
        border-radius: 10px !important;
        margin: 0 !important;
    }

    .btn {
        min-height: 46px;
        padding: .6rem .9rem;
        border-radius: 10px;
    }

    form > .btn[type="submit"],
    form > button[type="submit"] {
        min-height: 48px;
    }

    .d-inline .btn,
    form.d-inline .btn {
        margin-top: 4px;
        margin-bottom: 4px;
    }

    .category-filters {
        display: flex;
        gap: 6px;
        max-height: none;
        overflow-x: auto;
        overflow-y: hidden;
        padding: 8px;
        scroll-snap-type: x proximity;
        -webkit-overflow-scrolling: touch;
    }

    .category-filters .btn {
        flex: 0 0 auto;
        scroll-snap-align: start;
    }

    .advertisement-slider {
        height: 190px !important;
        border-radius: 12px;
    }

    .advertisement-container {
        min-height: 190px !important;
    }

    .advertisement-overlay {
        transform: translateY(0) !important;
        padding: 34px 14px 12px;
    }

    .advertisement-title {
        font-size: 1rem;
        margin-bottom: 3px;
    }

    .advertisement-description {
        font-size: .78rem;
        line-height: 1.25;
        display: -webkit-box;
        -webkit-line-clamp: 2;
        -webkit-box-orient: vertical;
        overflow: hidden;
    }

    .advertisement-arrow {
        width: 34px !important;
        height: 34px !important;
        font-size: .85rem !important;
    }

    .advertisement-prev {
        left: 8px;
    }

    .advertisement-next {
        right: 8px;
    }

    .advertisement-progress {
        top: 10px;
        left: 12px;
        right: 12px;
    }

    .footer {
        margin-bottom: 4px;
    }

    .footer .container {
        margin-top: 8px !important;
        padding: 10px !important;
        font-size: .78rem;
    }

    .alert {
        padding: .75rem .85rem;
        border-radius: 10px;
    }

    .table {
        font-size: .84rem;
    }

    .table:not(.table-responsive .table) {
        display: block;
        width: 100%;
        overflow-x: auto;
        -webkit-overflow-scrolling: touch;
        white-space: nowrap;
    }

    .table td,
    .table th {
        padding: .6rem;
        vertical-align: middle;
    }

    .rounded-circle[style*="150px"] {
        width: 110px !important;
        height: 110px !important;
    }

    .position-fixed.btn {
        bottom: calc(var(--easy-mobile-nav-h) + 18px) !important;
        right: 12px !important;
    }
}

/* Very small phones */
@media (max-width: 390px) {
    .container,
    section.container {
        width: calc(100% - 10px) !important;
        margin-left: 5px !important;
        margin-right: 5px !important;
        padding: 13px !important;
    }

    .easy-mobile-nav {
        left: 5px;
        right: 5px;
    }

    .easy-mobile-nav a {
        font-size: .64rem;
        padding-left: 2px;
        padding-right: 2px;
    }

    .advertisement-slider {
        height: 165px !important;
    }

    .advertisement-container {
        min-height: 165px !important;
    }
}

@media (hover: none) and (pointer: coarse) {
    .card:hover,
    .btn:hover,
    .list-group-item:hover,
    .navbar-brand:hover,
    .nav-link:hover {
        transform: none !important;
    }
}
'''
        _css_path.write_text(_css, encoding="utf-8")

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
