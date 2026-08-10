#!/usr/bin/env python3
"""Build a self-contained Linux wheel for liblouis (prototype).

Run from a configured, built liblouis tree:

    ./autogen.sh && ./configure --enable-ucs4 && make
    python/wheel/build_wheel.py

Produces dist/liblouis-<version>-py3-none-manylinux_*.whl containing the
ctypes bindings, the shared library, and the translation tables - so
`pip install liblouis` needs no system liblouis and no compiler.

Three things make this work, and they are the whole point of the prototype:

1. The bindings load the library by hardcoded soname
   (`_loader["liblouis.so.20"]`, substituted at configure time). That is
   correct for a system-installed liblouis - see upstream #641, where the
   ABI argument for keeping the macro was settled - but wrong for a wheel,
   which must load its OWN bundled copy. We rewrite that one line to
   resolve the library next to the package. Upstream's macro is untouched.

2. The wheel is tagged `py3-none-<platform>`. The bindings are pure ctypes
   with no CPython C-API use, so there is no ABI to match: ONE wheel per
   platform serves every Python 3.x, and PyPy and free-threaded builds
   too. No rebuild when a new Python ships.

3. Tables ship inside the wheel and are found via LOUIS_TABLEPATH, set at
   import. (lou_setDataPath is deprecated upstream. The production fix is a
   custom table resolver via lou_registerTableResolver, as suggested in
   #1700 and already done by the Java bindings - left as follow-up.)
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STAGE = ROOT / "python" / "wheel" / "_stage"
DIST = ROOT / "python" / "wheel" / "dist"
VENV = ROOT / ".wheelenv" / "bin"

PYPROJECT = """\
[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"

[project]
name = "liblouis"
version = "{version}"
description = "Braille translation and back-translation (liblouis), batteries included"
requires-python = ">=3.10"
license = {{ text = "LGPLv2.1+" }}
classifiers = [
  "Programming Language :: Python",
  "Topic :: Text Processing :: Linguistic",
  "License :: OSI Approved :: GNU Library or Lesser General Public License (LGPL)",
]

[tool.setuptools]
packages = ["louis"]
include-package-data = true
# The wheel redistributes the liblouis shared library and the tables, so the
# licences must travel with it. COPYING is GPL-3 (programs), COPYING.LESSER is
# LGPL-2.1 (the library and the tables).
license-files = ["COPYING", "COPYING.LESSER"]

[tool.setuptools.package-data]
louis = ["_lib/*", "_tables/**"]
"""

# Inserted immediately after the module's ctypes loader line.
LOADER = '''\
# --- wheel patch: load the bundled library, not a system one ---------------
# Replaces `_loader["<soname>"]`. A wheel must bind to the exact liblouis it
# ships: a system copy may be built with a different charSize, and that is an
# ABI mismatch, not a translation difference. Resolved by glob because
# auditwheel may rename the file when it vendors dependencies.
import glob as _glob
import os as _os

_pkgdir = _os.path.dirname(_os.path.abspath(__file__))
_candidates = sorted(_glob.glob(_os.path.join(_pkgdir, "_lib", "liblouis*.so*")))
if not _candidates:
    raise ImportError(
        f"bundled liblouis library missing from {_pkgdir}/_lib; "
        f"this wheel is corrupt"
    )
# Tables ship with the wheel. Set before any table is compiled; os.environ
# writes through to putenv, so the C library's getenv sees it.
_os.environ.setdefault("LOUIS_TABLEPATH", _os.path.join(_pkgdir, "_tables"))
liblouis = _loader[_candidates[0]]
# --- end wheel patch -------------------------------------------------------
'''


def run(cmd, **kw):
    print(f"$ {' '.join(str(c) for c in cmd)}")
    # auditwheel shells out to patchelf, which pip installed into the build
    # venv rather than onto the system PATH.
    env = {**os.environ, "PATH": f"{VENV}{os.pathsep}{os.environ['PATH']}"}
    subprocess.run(cmd, check=True, env=env, **kw)


def stage():
    if STAGE.exists():
        shutil.rmtree(STAGE)
    (STAGE / "louis" / "_lib").mkdir(parents=True)

    # 1. the bindings, with the loader repointed at the bundled library
    generated = ROOT / "python" / "louis" / "__init__.py"
    if not generated.exists():
        sys.exit(f"{generated} missing - run ./configure && make first")
    source = generated.read_text()
    patched, count = re.subn(r'^liblouis = _loader\[".*?"\]$', LOADER,
                             source, count=1, flags=re.MULTILINE)
    if count != 1:
        sys.exit("could not find the ctypes loader line to patch")
    (STAGE / "louis" / "__init__.py").write_text(patched)

    # 2. the shared library (real file, not the symlinks)
    libs = sorted((ROOT / "liblouis" / ".libs").glob("liblouis.so.*.*.*"))
    if not libs:
        sys.exit("no built library in liblouis/.libs - run make first")
    shutil.copy2(libs[0], STAGE / "louis" / "_lib" / libs[0].name)

    # 3. the translation tables
    shutil.copytree(ROOT / "tables", STAGE / "louis" / "_tables",
                    ignore=shutil.ignore_patterns("Makefile*", "*.am", "*.in"))

    # 4. licences, so the redistributed library and tables carry their terms
    for name in ("COPYING", "COPYING.LESSER"):
        shutil.copy2(ROOT / name, STAGE / name)

    version = (ROOT / "configure.ac").read_text()
    version = re.search(r"AC_INIT\(\[[^\]]+\],\s*\[([^\]]+)\]", version).group(1)
    (STAGE / "pyproject.toml").write_text(PYPROJECT.format(version=version))
    return version


def main():
    version = stage()
    tables = len(list((STAGE / "louis" / "_tables").rglob("*")))
    print(f"staged liblouis {version}: {tables} table files")

    if DIST.exists():
        shutil.rmtree(DIST)
    run([VENV / "python", "-m", "build", "--wheel", "--outdir", DIST], cwd=STAGE)

    built = next(DIST.glob("*.whl"))
    # setuptools tags this for the building interpreter; retag it as
    # ABI-agnostic, which is what a pure-ctypes package actually is.
    run([VENV / "python", "-m", "wheel", "tags", "--python-tag", "py3",
         "--abi-tag", "none", "--platform-tag", "linux_x86_64",
         "--remove", built])

    retagged = next(DIST.glob("*py3-none-linux_x86_64.whl"))
    run([VENV / "auditwheel", "repair", "--wheel-dir", DIST, retagged])
    print("\nwheels:")
    for whl in sorted(DIST.glob("*.whl")):
        print(f"  {whl.name}  ({whl.stat().st_size // 1024} KiB)")


if __name__ == "__main__":
    main()
