#!/usr/bin/env python3
"""Print the package version held in ``setup.py`` (the single source of truth).

Pure stdlib: the version is read by parsing the AST of ``setup.py``, so no
setuptools is required and the file is never executed. Used by ``build.sh``,
the compat scripts and the release-tag validation in CI.
"""

import ast
import pathlib
import sys

repo_root = pathlib.Path(__file__).resolve().parents[1]
tree = ast.parse((repo_root / "setup.py").read_text())

for node in ast.walk(tree):
    if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "setup":
        for keyword in node.keywords:
            if keyword.arg == "version":
                version = getattr(keyword.value, "value", None)
                if isinstance(version, str) and version:
                    print(version)
                    sys.exit(0)
                print("error: version= in setup.py is not a non-empty string literal", file=sys.stderr)
                sys.exit(1)

print("error: could not find version= in the setup() call in setup.py", file=sys.stderr)
sys.exit(1)
