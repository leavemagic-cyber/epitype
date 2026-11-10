"""Keep synthetic ancestor catalogs inside their fixture, including on Windows."""
from pathlib import Path
from unittest.mock import patch

from epitype import fixed_load


def isolate_catalogs(root):
    root = Path(root).resolve()
    original = fixed_load._launch_directories
    return patch.object(fixed_load, "_launch_directories", side_effect=lambda cwd: [
        path for path in original(cwd) if path == root or root in path.parents])
