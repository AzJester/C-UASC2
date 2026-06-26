"""Make the c2-core `app` package importable in tests without installation."""
import sys
from pathlib import Path

C2_CORE = Path(__file__).resolve().parent.parent / "services" / "c2-core"
if str(C2_CORE) not in sys.path:
    sys.path.insert(0, str(C2_CORE))
