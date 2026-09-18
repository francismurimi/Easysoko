from pathlib import Path
import base64
import importlib.util
import io
import sys
import zipfile

# Vercel entrypoint for the recovered Easy Soko application.
# The recovered project is stored in chunked form under .recovery/.
ROOT = Path(__file__).resolve().parent
RECOVERY_DIR = ROOT / ".recovery"
RUNTIME_DIR = Path("/tmp/easysoko_runtime")


def _restore_recovered_project():
    app_file = RUNTIME_DIR / "app.py"
    if app_file.exists():
        return app_file

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    chunks = sorted(RECOVERY_DIR.glob("chunk*"))
    if not chunks:
        raise RuntimeError("Easy Soko recovery payload is missing.")

    encoded = "".join(chunk.read_text(encoding="utf-8").strip() for chunk in chunks)
    archive_bytes = base64.b64decode(encoded)

    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        archive.extractall(RUNTIME_DIR)

    if not app_file.exists():
        raise RuntimeError("Recovered Easy Soko app.py was not found.")

    return app_file


_recovered_app_file = _restore_recovered_project()

spec = importlib.util.spec_from_file_location("easysoko_recovered", _recovered_app_file)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

# Vercel looks for a top-level Flask object named "app".
app = module.app

# The current recovered project uses SQLite. On Vercel this database lives in
# /tmp and is therefore temporary. Initialize it so the deployed demo can run.
with app.app_context():
    module.db.create_all()
    module.add_initial_categories()
    module.add_admin_user()
    module.add_demo_user()
    module.add_demo_advertisements()
    module.update_demo_item_images()
    module.add_demo_items()
