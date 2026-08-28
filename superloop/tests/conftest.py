from __future__ import annotations

import sys
from pathlib import Path


SUPERLOOP_ROOT = Path(__file__).resolve().parents[1]
if str(SUPERLOOP_ROOT) not in sys.path:
    sys.path.insert(0, str(SUPERLOOP_ROOT))
