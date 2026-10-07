"""Put `scripts/` on the path so every test module imports the site scripts bare.

The scripts import each other by bare module name (`from runtime import ...`),
because they are run as a directory of CLIs rather than as a package. Doing the
`sys.path` insert here — rather than in each test module — is what lets a new
test file import `search` or `db` without copying the two lines.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
