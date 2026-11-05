"""Start ATLAS:  python run.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import migrate_legacy_files  # noqa: E402

_moved = migrate_legacy_files()  # older copies kept settings/data inside the program folder
if _moved:
    print("  Moved your settings and data into the shared ATLAS folder: " + ", ".join(_moved))

from app.server import main  # noqa: E402

if __name__ == "__main__":
    main()
