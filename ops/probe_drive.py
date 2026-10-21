"""Read-only Drive checks; see nebula.drive_probe. In the deployed image run `nebula probe-drive` instead.

Env: NEBULA_PROBE_FOLDER (folder id or link), optional NEBULA_PROBE_WATCH_SECONDS.
"""

import asyncio
import os

from nebula.drive_probe import main

asyncio.run(main(os.environ["NEBULA_PROBE_FOLDER"], int(os.environ.get("NEBULA_PROBE_WATCH_SECONDS", "0"))))
