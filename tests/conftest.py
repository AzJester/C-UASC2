"""Make repository services importable in tests without installation."""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
C2_CORE = REPO_ROOT / "services" / "c2-core"
if str(C2_CORE) not in sys.path:
    sys.path.insert(0, str(C2_CORE))
