"""Claude adapter package compatibility."""

import sys

from . import _hook_common


sys.modules.setdefault("_hook_common", _hook_common)
