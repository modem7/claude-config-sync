"""claude_sync: sync Claude Code config between machines through a git repo.

Run it as `python -m claude_sync`. Standard library only; needs Python 3.12+.
"""

__all__ = ["Fail", "run"]

from claude_sync.util import Fail, run
