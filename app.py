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

# Seller-created items must be approved by an admin before they reach Browse.
_old_item_default = "approval_status = db.Column(db.String(20), default='pending')  # pending, approved, rejected\n\nclass Cart"
_new_item_default = "approval_status = db.Column(db.String(20), default='pending')  # pending, approved, rejected\n\nclass Cart"
if _old_item_default in _source:
    _source = _source.replace(_old_item_default, _new_item_default, 1)

_old_new_item = "image_url=final_image_url, \n            seller_id=current_user.id\n        )"
_new_new_item = "image_url=final_image_url, \n            seller_id=current_user.id,\n            approval_status='pending'\n        )"
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

# Dashboard should show the currently active advertisements under the user actions.
_old_dashboard_route = """@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html', user=current_user)"""
_new_dashboard_route = """@app.route('/dashboard')
@login_required
def dashboard():
    advertisements = Advertisement.query.filter_by(active=True).order_by(
        Advertisement.position,
        Advertisement.created_at.desc()
    ).limit(7).all()
    return render_template(
        'dashboard.html',
        user=current_user,
        advertisements=advertisements
    )"""
if _old_dashboard_route in _source:
    _source = _source.replace(_old_dashboard_route, _new_dashboard_route, 1)

# ---------------------------------------------------------------------------
# Marketplace moderation, international contact, purchase and location flow
# ---------------------------------------------------------------------------

# Sellers need usable contact/location details before submitting a listing.
_sell_anchor = """    if request.method == 'POST':
        title = request.form['title']"""
_sell_replacement = """    if request.method == 'POST':
        seller_phone = re.sub(r'[\\s().-]', '', (current_user.contact_number or '').strip())
        if not re.fullmatch(r'\\+[1-9]\\d{6,14}', seller_phone):
            flash('Add a valid international contact number such as +254712345678 before listing an item.')
            return redirect(url_for('update_profile'))
        if not (current_user.shop_location or '').strip():
            flash('Add your shop/location details before listing an item so buyers can find you.')
            return redirect(url_for('update_profile'))

        title = request.form['title']"""
if _sell_anchor in _source:
    _source = _source.replace(_sell_anchor, _sell_replacement, 1)

_source = _source.replace(
    "        flash('Item listed for sale!')",
    "        flash('Item submitted for admin approval. It will appear in the marketplace after approval.')",
    1,
)

# Pending/rejected products are not public, but the seller and signed-in admin
# can still preview them.
_item_route_anchor = """def item_detail(item_id):
    item = Item.query.get_or_404(item_id)
    if request.method == 'POST':"""
_item_route_replacement = """def item_detail(item_id):
    item = Item.query.get_or_404(item_id)

    if item.approval_status != 'approved':
        seller_preview = current_user.is_authenticated and item.seller_id == current_user.id
        admin_preview = session.get('admin_logged_in')
        if not seller_preview and not admin_preview:
            flash('This item is awaiting admin approval and is not available in the marketplace yet.')
            return redirect(url_for('browse'))

    if request.method == 'POST':"""
if _item_route_anchor in _source:
    _source = _source.replace(_item_route_anchor, _item_route_replacement, 1)

# Buyer contact is mandatory and must use international/E.164-style format.
_old_buy = """        if action == 'buy':
            if item.sold:
                flash('Item already sold!')
            else:
                item.sold = True
                db.session.add(Purchase(user_id=current_user.id, item_id=item.id))
                db.session.add(UserAction(user_id=current_user.id, action='buy', item_id=item.id))
                db.session.commit()
                flash('You bought this item!')"""
_new_buy = """        if action == 'buy':
            buyer_phone = re.sub(r'[\\s().-]', '', (current_user.contact_number or '').strip())
            if not re.fullmatch(r'\\+[1-9]\\d{6,14}', buyer_phone):
                flash('Add a valid international contact number such as +254712345678 before purchasing.')
                return redirect(url_for('update_profile'))
            if item.approval_status != 'approved':
                flash('This item is not approved for sale yet.')
            elif item.sold:
                flash('Item already sold!')
            elif item.seller_id == current_user.id:
                flash('You cannot purchase your own listing.')
            else:
                current_user.contact_number = buyer_phone
                item.sold = True
                db.session.add(Purchase(user_id=current_user.id, item_id=item.id))
                db.session.add(UserAction(user_id=current_user.id, action='buy', item_id=item.id))
                db.session.commit()
                flash('Purchase recorded. Use the seller contact and shop location shown on this page to arrange payment and collection/delivery.')"""
if _old_buy in _source:
    _source = _source.replace(_old_buy, _new_buy, 1)

# Normalize/validate international contact numbers when a profile is updated.
_old_profile_contact = """        contact_number = request.form.get('contact_number')
        delivery_address = request.form.get('delivery_address')
        shop_location = request.form.get('shop_location')"""
_new_profile_contact = """        contact_number = request.form.get('contact_number', '').strip()
        normalized_contact = re.sub(r'[\\s().-]', '', contact_number)
        if normalized_contact and not re.fullmatch(r'\\+[1-9]\\d{6,14}', normalized_contact):
            flash('Contact number must include a country code, for example +254712345678, +14155552671 or +447911123456.')
            return render_template('update_profile.html', user=current_user)

        delivery_address = request.form.get('delivery_address', '').strip()
        shop_location = request.form.get('shop_location', '').strip()"""
if _old_profile_contact in _source:
    _source = _source.replace(_old_profile_contact, _new_profile_contact, 1)

_source = _source.replace(
    "        current_user.contact_number = contact_number",
    "        current_user.contact_number = normalized_contact or None",
    1,
)

# Make the admin approval confirmation explain the next step.
_source = _source.replace(
    "    flash('Item approved.')",
    "    flash('Item approved and now visible in the marketplace. You can also promote it from Manage Advertisements.')",
    1,
)

# Approved products become selectable in the advertisement manager.
_old_ads_render = """    advertisements = Advertisement.query.order_by(Advertisement.position, Advertisement.created_at.desc()).all()
    return render_template('admin_advertisements.html', advertisements=advertisements)"""
_new_ads_render = """    advertisements = Advertisement.query.order_by(Advertisement.position, Advertisement.created_at.desc()).all()
    approved_items = Item.query.filter_by(approval_status='approved', sold=False).order_by(Item.id.desc()).all()
    return render_template(
        'admin_advertisements.html',
        advertisements=advertisements,
        approved_items=approved_items
    )"""
if _old_ads_render in _source:
    _source = _source.replace(_old_ads_render, _new_ads_render, 1)

_ad_route_anchor = """@app.route('/admin/advertisement/<int:ad_id>/toggle', methods=['POST'])
def toggle_advertisement(ad_id):"""
_ad_route = """@app.route('/admin/advertise_item/<int:item_id>', methods=['POST'])
def advertise_item(item_id):
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))

    item = Item.query.get_or_404(item_id)
    if item.approval_status != 'approved' or item.sold:
        flash('Only approved, available products can be promoted.')
        return redirect(url_for('admin_advertisements'))
    if not item.image_url:
        flash('Add an image to this item before promoting it as an advertisement.')
        return redirect(url_for('admin_advertisements'))

    item_link = url_for('item_detail', item_id=item.id)
    existing = Advertisement.query.filter_by(link_url=item_link).first()
    if existing:
        existing.title = item.title
        existing.description = item.desc
        existing.image_url = item.image_url
        existing.active = True
        db.session.commit()
        flash('Advertisement refreshed and activated for this product.')
        return redirect(url_for('admin_advertisements'))

    if Advertisement.query.filter_by(active=True).count() >= 7:
        flash('Maximum 7 active advertisements allowed. Deactivate one first.')
        return redirect(url_for('admin_advertisements'))

    db.session.add(Advertisement(
        title=item.title,
        description=item.desc,
        image_url=item.image_url,
        link_url=item_link,
        position=0,
        active=True
    ))
    db.session.commit()
    flash('Approved product added to the advertisement section.')
    return redirect(url_for('admin_advertisements'))


@app.route('/admin/advertisement/<int:ad_id>/toggle', methods=['POST'])
def toggle_advertisement(ad_id):"""
if _ad_route_anchor in _source:
    _source = _source.replace(_ad_route_anchor, _ad_route, 1)


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

# Marketplace workflow UI patches.
def _easy_patch_template(name, replacements):
    path = _templates_dir / name
    if not path.exists():
        return
    html = path.read_text(encoding="utf-8")
    for old, new in replacements:
        if old in html:
            html = html.replace(old, new, 1)
    path.write_text(html, encoding="utf-8")


# Seller listing page: make moderation expectations explicit.
_easy_patch_template("sell.html", [
    (
        "<h2>Sell an Item</h2>",
        """<h2>Sell an Item</h2>
    <div class="alert alert-info">
        <strong>How listing works:</strong> submit your product here, then an Easy Soko admin reviews it.
        It appears in the marketplace only after approval. Make sure your profile has an international
        contact number and shop/location details before submitting.
    </div>"""
    ),
])

# Seller profile: show moderation state for every listing.
_easy_patch_template("profile.html", [
    (
        """<h5 class="card-title">{{ item.title }}</h5>
                            <span class="badge bg-info text-dark mb-2">{{ item.category.name|capitalize }}</span>""",
        """<h5 class="card-title">{{ item.title }}</h5>
                            <span class="badge bg-info text-dark mb-2">{{ item.category.name|capitalize }}</span>
                            {% if item.approval_status == 'approved' %}
                                <span class="badge bg-success mb-2">Approved / Live</span>
                            {% elif item.approval_status == 'rejected' %}
                                <span class="badge bg-danger mb-2">Rejected</span>
                            {% else %}
                                <span class="badge bg-warning text-dark mb-2">Pending Admin Approval</span>
                            {% endif %}"""
    ),
])

# Admin advertisement manager: approved marketplace products can be promoted directly.
_easy_patch_template("admin_advertisements.html", [
    (
        """    <!-- Add New Advertisement Form -->""",
        """    <div class="card mb-4">
        <div class="card-header d-flex justify-content-between align-items-center">
            <h5 class="mb-0"><i class="fas fa-box-open"></i> Approved Products Ready to Promote</h5>
            <span class="badge bg-success">{{ approved_items|length }} approved</span>
        </div>
        <div class="card-body">
            {% if approved_items %}
            <div class="row">
                {% for item in approved_items %}
                <div class="col-md-6 col-lg-4 mb-3">
                    <div class="card h-100">
                        {% if item.image_url %}
                            <img src="{{ item.image_url }}" class="card-img-top" alt="{{ item.title }}" style="height:170px;object-fit:cover;">
                        {% endif %}
                        <div class="card-body">
                            <h6>{{ item.title }}</h6>
                            <p class="small text-muted mb-2">{{ item.category.name }} · {{ item.seller.username }}</p>
                            <p class="mb-2">Ksh {{ '%.2f' % item.price if item.price else 'N/A' }}</p>
                            <form method="post" action="/admin/advertise_item/{{ item.id }}">
                                <button type="submit" class="btn btn-primary w-100" {% if not item.image_url %}disabled{% endif %}>
                                    <i class="fas fa-bullhorn"></i> Promote as Advertisement
                                </button>
                            </form>
                            {% if not item.image_url %}
                                <small class="text-warning">Add a product image before promoting.</small>
                            {% endif %}
                        </div>
                    </div>
                </div>
                {% endfor %}
            </div>
            {% else %}
                <p class="text-muted mb-0">No approved, available products are ready for promotion yet.</p>
            {% endif %}
        </div>
    </div>

    <!-- Add New Advertisement Form -->"""
    ),
])

# Profile update: international phone number plus device-location buttons.
_easy_patch_template("update_profile.html", [
    (
        """<input type="tel" class="form-control" id="contact_number" name="contact_number" value="{{ user.contact_number or '' }}" placeholder="Enter your phone number">""",
        """<input type="tel" inputmode="tel" autocomplete="tel" class="form-control" id="contact_number"
                                   name="contact_number" value="{{ user.contact_number or '' }}"
                                   placeholder="+254712345678" required>
                            <div class="form-text">
                                Include the country code. Examples: Kenya +254712345678, USA/Canada +14155552671,
                                UK +447911123456. International numbers from any country are supported.
                            </div>"""
    ),
    (
        """<textarea class="form-control" id="delivery_address" name="delivery_address" rows="3" placeholder="Enter your delivery address">{{ user.delivery_address or '' }}</textarea>
                            <div class="form-text">This address will be used for deliveries when you buy items.</div>""",
        """<textarea class="form-control" id="delivery_address" name="delivery_address" rows="3" placeholder="Enter your delivery address">{{ user.delivery_address or '' }}</textarea>
                            <button type="button" class="btn btn-outline-primary btn-sm mt-2"
                                    onclick="easyUseLocation('delivery_address', 'delivery_location_status')">
                                📍 Use my current location
                            </button>
                            <div id="delivery_location_status" class="form-text">This address will be used for deliveries when you buy items.</div>"""
    ),
    (
        """<textarea class="form-control" id="shop_location" name="shop_location" rows="3" placeholder="Enter your shop location">{{ user.shop_location or '' }}</textarea>
                            <div class="form-text">If you're a seller, provide your shop location for customers to find you.</div>""",
        """<textarea class="form-control" id="shop_location" name="shop_location" rows="3" placeholder="Enter your shop location or use device location">{{ user.shop_location or '' }}</textarea>
                            <button type="button" class="btn btn-outline-success btn-sm mt-2"
                                    onclick="easyUseLocation('shop_location', 'shop_location_status')">
                                📍 Use my shop/device location
                            </button>
                            <div id="shop_location_status" class="form-text">
                                Your browser will ask permission before Easy Soko reads the device location. You can also type the location manually.
                            </div>"""
    ),
    (
        """<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>""",
        """<script>
function easyUseLocation(targetId, statusId) {
    const field = document.getElementById(targetId);
    const status = document.getElementById(statusId);

    if (!navigator.geolocation) {
        status.textContent = 'Location is not supported by this browser. Enter the location manually.';
        return;
    }

    status.textContent = 'Requesting location permission…';

    navigator.geolocation.getCurrentPosition(
        function(position) {
            const lat = position.coords.latitude.toFixed(6);
            const lng = position.coords.longitude.toFixed(6);
            const accuracy = Math.round(position.coords.accuracy || 0);
            const mapUrl = 'https://www.google.com/maps?q=' + lat + ',' + lng;
            field.value = 'Latitude: ' + lat + ', Longitude: ' + lng +
                          ', Accuracy: about ' + accuracy + ' m | ' + mapUrl;
            status.textContent = 'Location captured. You can edit the text before saving.';
        },
        function(error) {
            let message = 'Could not access your location. Enter it manually.';
            if (error.code === 1) message = 'Location permission was denied. You can still enter the location manually.';
            if (error.code === 2) message = 'Your device could not determine its location. Enter it manually.';
            if (error.code === 3) message = 'Location request timed out. Try again or enter it manually.';
            status.textContent = message;
        },
        { enableHighAccuracy: true, timeout: 12000, maximumAge: 60000 }
    );
}
</script>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>"""
    ),
])

# Product page: show seller shop location and explain buyer contact requirement.
_easy_patch_template("item_detail.html", [
    (
        """{% if item.seller.contact_number %}
                        <a href="https://wa.me/{{ item.seller.contact_number }}?text=Hi, I'm interested in your item: {{ item.title }} - {{ request.url }}" """,
        """{% if item.seller.shop_location %}
                        <p class="card-text mb-2">
                            <strong>Shop / Seller Location:</strong><br>{{ item.seller.shop_location }}
                        </p>
                        <a class="btn btn-outline-primary btn-sm mb-2" target="_blank"
                           href="https://www.google.com/maps/search/?api=1&query={{ item.seller.shop_location|urlencode }}">
                            📍 Open in Maps
                        </a>
                    {% endif %}
                    {% if item.seller.contact_number %}
                        <a href="https://wa.me/{{ item.seller.contact_number|replace('+','') }}?text=Hi, I'm interested in your item: {{ item.title }} - {{ request.url }}" """
    ),
    (
        """<p class="text-warning"><strong>Are you sure you want to purchase this item?</strong></p>""",
        """{% if current_user.contact_number %}
                    <p><strong>Your contact:</strong> {{ current_user.contact_number }}</p>
                {% else %}
                    <div class="alert alert-warning">
                        Add an international contact number in your profile before purchasing.
                        <a href="/profile/update" class="alert-link">Update profile</a>
                    </div>
                {% endif %}
                <p class="text-warning"><strong>Are you sure you want to purchase this item?</strong></p>"""
    ),
    (
        """<button name="action" value="buy" class="btn btn-success">Confirm Purchase</button>""",
        """<button name="action" value="buy" class="btn btn-success" {% if not current_user.contact_number %}disabled{% endif %}>
                        Confirm Purchase
                    </button>"""
    ),
])


# Dashboard: place advertisements below the dashboard cards and keep footer at page bottom.
_dashboard_path = _templates_dir / "dashboard.html"
if _dashboard_path.exists():
    _dashboard_html = _dashboard_path.read_text(encoding="utf-8")

    if 'class="easy-dashboard-main"' not in _dashboard_html:
        _dashboard_html = _dashboard_html.replace(
            '<div class="container mt-5">',
            '<main class="easy-dashboard-main">\n<div class="container mt-5">',
            1,
        )

        _dashboard_ads = r'''
</div>

<section class="container mt-4 mb-4 easy-dashboard-ads">
    <div class="d-flex justify-content-between align-items-center flex-wrap gap-2 mb-3">
        <div>
            <h3 class="mb-1">Featured on Easy Soko</h3>
            <p class="text-muted mb-0">Products and offers currently being advertised.</p>
        </div>
        <a href="/browse" class="btn btn-outline-primary btn-sm">View Marketplace</a>
    </div>

    {% if advertisements %}
    <div class="row g-3">
        {% for ad in advertisements %}
        <div class="col-12 col-sm-6 col-lg-4">
            <div class="card h-100 easy-ad-card">
                {% if ad.image_url %}
                <img src="{{ ad.image_url }}"
                     class="card-img-top easy-ad-card-image"
                     alt="{{ ad.title }}"
                     loading="lazy">
                {% endif %}
                <div class="card-body d-flex flex-column">
                    <span class="badge bg-primary-subtle text-primary align-self-start mb-2">Featured</span>
                    <h5 class="card-title">{{ ad.title }}</h5>
                    {% if ad.description %}
                    <p class="card-text text-muted easy-ad-description">{{ ad.description }}</p>
                    {% endif %}
                    {% if ad.link_url %}
                    <a href="{{ ad.link_url }}" class="btn btn-primary mt-auto">View Item</a>
                    {% else %}
                    <a href="/browse" class="btn btn-primary mt-auto">Browse Marketplace</a>
                    {% endif %}
                </div>
            </div>
        </div>
        {% endfor %}
    </div>
    {% else %}
    <div class="card easy-empty-ads">
        <div class="card-body text-center py-4">
            <h5 class="mb-2">No featured items yet</h5>
            <p class="text-muted mb-3">Approved products promoted by the admin will appear here.</p>
            <a href="/browse" class="btn btn-primary">Browse Marketplace</a>
        </div>
    </div>
    {% endif %}
</section>
</main>
'''

        _dashboard_html = _dashboard_html.replace(
            '</div>\n<footer class="footer mt-auto py-3">',
            _dashboard_ads + '\n<footer class="footer mt-auto py-3">',
            1,
        )

    _dashboard_path.write_text(_dashboard_html, encoding="utf-8")


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

/* EASY_SOKO_DASHBOARD_ADS_V1 */
.easy-dashboard-main {
    flex: 1 0 auto;
    width: 100%;
}

.easy-dashboard-ads {
    width: calc(100% - 24px);
}

.easy-ad-card {
    overflow: hidden;
}

.easy-ad-card-image {
    width: 100%;
    height: 210px;
    object-fit: cover;
}

.easy-ad-description {
    display: -webkit-box;
    -webkit-line-clamp: 3;
    -webkit-box-orient: vertical;
    overflow: hidden;
}

.easy-empty-ads {
    border-style: dashed;
}

@media (max-width: 767.98px) {
    .easy-dashboard-main {
        width: 100%;
    }

    .easy-dashboard-ads {
        width: calc(100% - 16px) !important;
        margin-left: 8px !important;
        margin-right: 8px !important;
    }

    .easy-ad-card-image {
        height: 180px;
    }
}

/* EASY_SOKO_STICKY_FOOTER_V1 */
html {
    min-height: 100%;
}

body {
    min-height: 100vh;
    min-height: 100dvh;
    display: flex;
    flex-direction: column;
}

.footer {
    margin-top: auto !important;
    flex-shrink: 0;
    width: 100%;
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
