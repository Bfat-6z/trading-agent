"""Auto-install Phase 00 legacy live-script blocker for local Python runs."""
from __future__ import annotations

try:
    import legacy_live_blocker

    legacy_live_blocker.install()
except SystemExit:
    raise
except Exception:
    # Fail quietly here so normal Python tooling still starts. Explicit
    # preflight/security scans fail closed if blocker files are broken.
    pass
