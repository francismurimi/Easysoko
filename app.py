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
RUNTIME_DIR = Path("/tmp/easysoko_runtime")
READY_MARKER = RUNTIME_DIR / ".ready"


def _restore_recovered_project():
    app_file = RUNTIME_DIR / "app.py"

    if READY_MARKER.exists() and app_file.exists():
        return app_file

    if RUNTIME_DIR.exists():
        shutil.rmtree(RUNTIME_DIR)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    chunks = sorted(RECOVERY_DIR.glob("chunk*"))
    if len(chunks) != 12:
        raise RuntimeError(
            f"Easy Soko recovery payload is incomplete: expected 12 chunks, found {len(chunks)}"
        )

    encoded = "".join(chunk.read_text(encoding="utf-8").strip() for chunk in chunks)
    archive_bytes = base64.b64decode(encoded, validate=True)

    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        archive.testzip()
        archive.extractall(RUNTIME_DIR)

    if not app_file.exists():
        raise RuntimeError("Recovered Easy Soko app.py was not found after extraction.")

    READY_MARKER.write_text("ok", encoding="utf-8")
    return app_file


_recovered_app_file = _restore_recovered_project()
os.chdir(RUNTIME_DIR)

spec = importlib.util.spec_from_file_location("easysoko_recovered", _recovered_app_file)
if spec is None or spec.loader is None:
    raise RuntimeError("Unable to load the recovered Easy Soko Flask application.")

module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

# Vercel detects this top-level Flask object.
app = module.app

# Initialize a temporary SQLite database for the deployed demo.
# Persistent production data should later be moved to PostgreSQL.
try:
    with app.app_context():
        module.db.create_all()
        module.add_initial_categories()
        module.add_admin_user()
        module.add_demo_user()
        module.add_demo_advertisements()
        module.update_demo_item_images()
        module.add_demo_items()
except Exception:
    app.logger.exception("Easy Soko startup database initialization failed")
