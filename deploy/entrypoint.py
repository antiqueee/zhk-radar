"""Seed non-secret defaults into the persistent workspace on first container start."""

import os
import shutil
from pathlib import Path

root = Path("/workspace")
(root / "config").mkdir(parents=True, exist_ok=True)
for source in Path("/app/config").glob("*.yaml"):
    target = root / "config" / source.name
    if not target.exists():
        shutil.copyfile(source, target)
os.chdir(root)
os.execvp(os.sys.argv[1], os.sys.argv[1:])
