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

import argparse
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
# ABI mismatch, not a translation difference. Resolved by glob rather than by
# name because auditwheel may rename the file when it vendors dependencies,
# and the extension differs per platform (.so / .dll / .dylib).
import glob as _glob
import os as _os

_pkgdir = _os.path.dirname(_os.path.abspath(__file__))
_libdir = _os.path.join(_pkgdir, "_lib")
_candidates = sorted(
    _glob.glob(_os.path.join(_libdir, "liblouis*.so*"))
    + _glob.glob(_os.path.join(_libdir, "*louis*.dll"))
    + _glob.glob(_os.path.join(_libdir, "liblouis*.dylib"))
)
if not _candidates:
    raise ImportError(
        f"bundled liblouis library missing from {_libdir}; this wheel is corrupt"
    )
if platform == "win32":
    # NB: _is_windows is defined *below* this point in the upstream module,
    # so test `platform` (imported from sys above) instead. Windows resolves
    # a DLL's own dependencies relative to its directory only if that
    # directory is registered - harmless when the DLL is fully static,
    # required as soon as it is not.
    _os.add_dll_directory(_libdir)
# Tables ship with the wheel. The env var is set for any code that reads it
# directly, but it is NOT how tables are found - see _createTableBuf below.
_bundled_tables = _os.path.join(_pkgdir, "_tables")
_user_tablepath = bool(_os.environ.get("LOUIS_TABLEPATH"))
_os.environ.setdefault("LOUIS_TABLEPATH", _bundled_tables)
liblouis = _loader[_candidates[0]]
# --- end wheel patch -------------------------------------------------------
'''

# Replaces the whole of _createTableBuf, the single point every entry taking a
# tableList funnels through.
TABLE_RESOLVER = '''\
def _createTableBuf(tablesList: TableListT) -> Array[c_char]:
    """Creates a tables string for liblouis calls.

    Wheel patch: bare table names are resolved to absolute paths inside the
    bundled _tables directory, rather than left for liblouis to look up.

    LOUIS_TABLEPATH cannot be relied on from Python. On Windows the library
    links msvcrt, the legacy CRT, which snapshots its own environment block at
    initialisation; a ucrt Python's os.environ writes never reach that copy,
    so the library's getenv does not see the path (upstream #1299). Absolute
    paths sidestep the lookup entirely, and liblouis still resolves each
    table's `include` directives relative to the table's own directory.

    Anything the caller supplies that already exists as a path is left alone,
    as is everything if LOUIS_TABLEPATH was set before this module was
    imported - that is a deliberate choice of tables and must win.
    """
    resolved = []
    for entry in tablesList:
        name = entry.decode(fileSystemEncoding) if isinstance(entry, bytes) else entry
        if not _user_tablepath and not _os.path.exists(name):
            bundled = _os.path.join(_bundled_tables, name)
            if _os.path.exists(bundled):
                name = bundled
        resolved.append(name.encode(fileSystemEncoding))
    return create_string_buffer(b",".join(resolved))
'''


def run(cmd, **kw):
    print(f"$ {' '.join(str(c) for c in cmd)}")
    # auditwheel shells out to patchelf, which pip installed into the build
    # venv rather than onto the system PATH.
    env = {**os.environ, "PATH": f"{VENV}{os.pathsep}{os.environ['PATH']}"}
    subprocess.run(cmd, check=True, env=env, **kw)


def stage(build):
    if STAGE.exists():
        shutil.rmtree(STAGE)
    (STAGE / "louis" / "_lib").mkdir(parents=True)

    # 1. the bindings, with the loader repointed at the bundled library
    generated = build / "python" / "louis" / "__init__.py"
    if not generated.exists():
        sys.exit(f"{generated} missing - run ./configure && make first")
    source = generated.read_text()
    patched, count = re.subn(r'^liblouis = _loader\[".*?"\]$', LOADER,
                             source, count=1, flags=re.MULTILINE)
    if count != 1:
        sys.exit("could not find the ctypes loader line to patch")

    # Swap out _createTableBuf entirely: splice between its def and the next
    # top-level statement, rather than pattern-matching its body.
    start = patched.find("def _createTableBuf")
    if start == -1:
        sys.exit("could not find _createTableBuf to patch")
    end = patched.find("\ndef ", start)
    if end == -1:
        sys.exit("could not find the end of _createTableBuf")
    patched = patched[:start] + TABLE_RESOLVER + patched[end + 1:]

    (STAGE / "louis" / "__init__.py").write_text(patched)

    # 2. the shared library. Real files only - .so.N.N.N on Linux is the
    #    target of two symlinks we must not copy; Windows gives a plain .dll.
    libdir = build / "liblouis" / ".libs"
    libs = [p for p in sorted(libdir.glob("*louis*"))
            if not p.is_symlink()
            and (re.search(r"\.so\.\d+\.\d+\.\d+$", p.name) or p.suffix == ".dll")]
    if not libs:
        sys.exit(f"no built library in {libdir} - run make first")
    shutil.copy2(libs[0], STAGE / "louis" / "_lib" / libs[0].name)

    # 3. the translation tables. Two of them (nl-NL-g0.utb, nl-chardefs.uti)
    #    are GENERATED from .in templates at build time and land in the build
    #    tree, not the source tree - so overlay the build tree's copies.
    #    Without this an out-of-tree build silently ships without them.
    tables = STAGE / "louis" / "_tables"
    shutil.copytree(ROOT / "tables", tables,
                    ignore=shutil.ignore_patterns("Makefile*", "*.am", "*.in"))
    generated = 0
    if (build / "tables") != (ROOT / "tables"):
        for made in (build / "tables").iterdir():
            if made.is_file() and made.suffix in (".utb", ".uti", ".ctb", ".dis", ".cti"):
                shutil.copy2(made, tables / made.name)
                generated += 1
    print(f"tables: {len(list(tables.iterdir()))} "
          f"({generated} generated, taken from the build tree)")

    # 4. licences, so the redistributed library and tables carry their terms
    for name in ("COPYING", "COPYING.LESSER"):
        shutil.copy2(ROOT / name, STAGE / name)

    version = (ROOT / "configure.ac").read_text()
    version = re.search(r"AC_INIT\(\[[^\]]+\],\s*\[([^\]]+)\]", version).group(1)
    (STAGE / "pyproject.toml").write_text(PYPROJECT.format(version=version))
    return version


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--build-dir", type=Path, default=ROOT,
                        help="configured build tree (default: the source root)")
    parser.add_argument("--plat-tag", default="linux_x86_64",
                        help="wheel platform tag, e.g. win_amd64")
    args = parser.parse_args()

    build = args.build_dir.resolve()
    version = stage(build)
    tables = len(list((STAGE / "louis" / "_tables").rglob("*")))
    lib = next((STAGE / "louis" / "_lib").iterdir())
    print(f"staged liblouis {version}: {lib.name}, {tables} table files")

    # Clear only this target's artefacts, so wheels for other platforms
    # built earlier survive alongside it.
    DIST.mkdir(exist_ok=True)
    for stale in list(DIST.glob("*-any.whl")) + list(DIST.glob(f"*{args.plat_tag}.whl")):
        stale.unlink()
    run([VENV / "python", "-m", "build", "--wheel", "--outdir", DIST], cwd=STAGE)

    # Specifically the wheel just built, not whichever the glob yields first:
    # other platforms' wheels now live in dist/ alongside it.
    built = next(DIST.glob("*-any.whl"))
    # setuptools tags this for the building interpreter; retag it as
    # ABI-agnostic, which is what a pure-ctypes package actually is.
    run([VENV / "python", "-m", "wheel", "tags", "--python-tag", "py3",
         "--abi-tag", "none", "--platform-tag", args.plat_tag,
         "--remove", built])

    if args.plat_tag.startswith("linux"):
        # auditwheel vendors the library's own dependencies and relabels the
        # wheel manylinux. There is no equivalent step for the MinGW build,
        # which is linked -static-libgcc and depends only on system DLLs.
        retagged = next(DIST.glob(f"*py3-none-{args.plat_tag}.whl"))
        run([VENV / "auditwheel", "repair", "--wheel-dir", DIST, retagged])
    print("\nwheels:")
    for whl in sorted(DIST.glob("*.whl")):
        print(f"  {whl.name}  ({whl.stat().st_size // 1024} KiB)")


if __name__ == "__main__":
    main()
