"""pytest settings: the real settings with STATE=DEV when the shell sets none.

`config.settings` refuses to load without STATE (no default, so a prod box
whose env failed to load cannot boot in DEV). Tests are DEV by definition.
"""

import os

os.environ.setdefault("STATE", "DEV")

from config.settings import *  # noqa: E402, F403
