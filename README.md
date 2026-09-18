# Easy Soko — Recovered E-commerce Project

Easy Soko is a Flask + SQLite marketplace for browsing and selling products, managing carts and wishlists, recording purchases, maintaining user profiles, and administering listings, advertisements, coupons, and downloadable reports.

## Recovery status

This copy was reconstructed from the surviving project archive. The existing Flask application, templates, stylesheet, database, and media were preserved. Broken functionality discovered during recovery was restored, including:

- wishlist item removal (`POST /remove_wishlist/<id>`)
- admin user CSV export (`GET /admin/download/users`)
- admin item CSV export (`GET /admin/download/items`)
- admin transaction CSV export (`GET /admin/download/transactions`)
- admin-only protection for coupon management
- public catalogue filtering so only approved listings are shown
- server-side signup validation matching the password rules displayed by the UI
- environment-based Flask/admin secrets so credentials are not hard-coded in source control

The old `venv` folder was intentionally removed because Python virtual environments are machine-specific. Recreate it from `requirements.txt`.

## Run locally

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:EASY_SOKO_SECRET_KEY="replace-this-with-a-long-random-secret"
$env:EASY_SOKO_ADMIN_EMAIL="admin@example.com"
$env:EASY_SOKO_ADMIN_PASSWORD="ChangeMe123!"
python app.py
```

Open `http://127.0.0.1:5000`.

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export EASY_SOKO_SECRET_KEY='replace-this-with-a-long-random-secret'
export EASY_SOKO_ADMIN_EMAIL='admin@example.com'
export EASY_SOKO_ADMIN_PASSWORD='ChangeMe123!'
python app.py
```

## Database and uploads

The recovered local copy still contains the original `instance/marketplace.db` and uploaded media so the recovered application can use the surviving data. They are intentionally excluded by `.gitignore` because a public Git repository should not contain user accounts, private profile data, runtime database records, or user uploads.

On a clean clone, `python app.py` creates the SQLite tables and demo content automatically.

## Main features

- account signup/login/logout
- product browsing and search by category/title
- item details with seller contact information
- cart and wishlist management
- purchase history and activity history
- seller listing creation/edit/delete
- profile editing and profile-photo uploads
- admin approval/rejection of listings/categories
- admin records management and CSV exports
- advertisement management
- coupon management

## GitHub

Before publishing, copy `.env.example` to your own local `.env` or set environment variables in your hosting provider. Never commit real passwords, secret keys, the live SQLite database, or private uploads.
