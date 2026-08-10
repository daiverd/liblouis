# Self-contained Python wheel for liblouis — Linux prototype

Prototype for [#1700](https://github.com/liblouis/liblouis/issues/1700).
Builds a wheel that bundles the liblouis shared library and the translation
tables, so `pip install liblouis` needs **no system liblouis and no
compiler**.

```bash
./autogen.sh && ./configure --enable-ucs4 && make
python/wheel/build_wheel.py
```

Produces `python/wheel/dist/liblouis-3.38.0-py3-none-manylinux_2_34_x86_64.whl`
(3.9 MB, 484 entries: 475 tables, the `.so`, and the bindings).

## Verified

Installed from the wheel into clean venvs, on a machine that also has a
system liblouis, to confirm it binds to its own copy:

| Python | library | charSize | liblouis | `translateString(["en-ueb-g2.ctb"], "knowledge")` |
|---|---|---|---|---|
| 3.12 | bundled | 4 | 3.38.0 | `⠅` |
| 3.13 | bundled | 4 | 3.38.0 | `⠅` |
| 3.14 | bundled | 4 | 3.38.0 | `⠅` |

- Loads `site-packages/louis/_lib/liblouis.so.20.1.3`, **not** the system
  `/usr/lib/x86_64-linux-gnu/liblouis.so.20`.
- `ldd` on the bundled library shows only `libc` and the loader — no
  external liblouis dependency.

## Three findings for #1700

**1. Linux does not need a source build at install time.** The issue assumes
*"On Linux, we can't pre-compile, afaics, so we make the source code part of
the Linux bindings and build as part of the installation."* `manylinux` +
`auditwheel` are exactly the tooling for this: build once in CI, bundle,
ship. Linux is the *easiest* platform to serve, not the hardest. This
assumption looks like the main thing that stalled the effort.

**2. One wheel per platform serves every Python.** The bindings are pure
ctypes with no CPython C-API use, so there is no ABI to match and the wheel
tags `py3-none-<platform>`. That collapses the build matrix from platforms ×
Python versions to platforms alone, and means no rebuild when a new Python
ships. Verified above across 3.12/3.13/3.14 with a single artifact.
(By the same reasoning it should work on PyPy and free-threaded builds —
not yet tested here.)

**3. "Ensure the correct dll (32 or 64 bit)" needs no runtime logic** — that
is what platform tags do; pip picks the right wheel.

## Python version floor: 3.10, and it's real

Verified by installing the wheel into clean venvs:

| Python | result |
|---|---|
| 3.8, 3.9 | `TypeError: unsupported operand type(s) for \|: 'type' and 'type'` |
| 3.10 – 3.14 | works |

The bindings use PEP 604 unions in signatures (`typeform: Iterable[int] |
None = None`). That is valid *syntax* on any version — `py_compile` passes on
3.8 — but the annotation is evaluated at function-definition time, so it
raises at import on <3.10. Upstream's `requires-python = ">=3.10"` is
therefore correct, not conservative.

Supporting older Pythons would mean `from __future__ import annotations` or
quoted annotations in the bindings, not anything about the build. Since
3.9 is already EOL, it isn't worth it — and note the cost of a wider range
is *zero extra build time* here: one `py3-none` artifact covers whatever
range the metadata allows.

## Windows: cross-compiled from Linux, already UCS-4

`.github/workflows/mingw.yml` already cross-compiles Windows from a Linux
runner (`x86_64-w64-mingw32`), and **already passes `--enable-ucs4`** — the
same choice made here, so charSize is consistent across platforms today.
Calling conventions line up too: `liblouis.h.in` defines
`EXPORT_CALL __stdcall` on Windows, which matches the bindings' `windll` /
`WINFUNCTYPE` branch.

Two consequences:

- A Windows wheel needs **no Windows runner** — the same Linux CI job that
  builds the Linux wheel can produce `win_amd64`, with `delvewheel` bundling
  the MinGW runtime DLLs (`libwinpthread-1.dll` etc.) or `-static-libgcc`
  avoiding them.
- The 32-bit (`i686-w64-mingw32`) job in that workflow is **commented out**,
  so upstream effectively ships 64-bit Windows only. `win32` need not be a
  target unless someone asks.

## UCS-2 vs UCS-4 is already settled in practice

Both builds observed in the wild are UCS-4: upstream's own MinGW CI, and
Debian's `liblouis20` (`wideCharBytes == 4`). The bindings adapt
automatically — `conversionEncoding = "utf_%d_%s" % (wideCharBytes * 8,
endianness)` — so the choice is invisible to callers *except* for non-BMP
input: under UCS-2 an astral character encodes to a surrogate pair, and
`inlen = len(buf) // wideCharBytes` then presents it to liblouis as two lone
surrogates rather than one character.

So: build UCS-4 everywhere, ship one variant, and don't expose it as an
option. A UCS-2 wheel would differ semantically from every other liblouis
build in circulation.

## Design notes

- **The soname macro is untouched.** `_loader["###LIBLOUIS_SONAME###"]` is
  correct for a *system* liblouis, and #641 settled that on ABI grounds. A
  wheel is a different case: it must bind to the exact library it ships,
  because a system copy may be built with a different `charSize` — an ABI
  mismatch, not a translation difference. `build_wheel.py` rewrites that one
  line to resolve the bundled library, and leaves upstream's build alone.
- **UCS-4.** Built with `--enable-ucs4` (upstream default is 16-bit).
  Upstream's own Python tests require 32-bit characters, and 16-bit cannot
  represent anything above the BMP. Since the wheel is self-contained, this
  is purely internal and needs no API surface.
- **Library resolved by glob**, because `auditwheel` may rename bundled
  libraries when it vendors dependencies.

## Known gaps before this is shippable

- **Tables are found via `LOUIS_TABLEPATH`.** `lou_setDataPath` is
  deprecated upstream. The production fix is a custom table resolver via
  `lou_registerTableResolver`, as suggested in #1700 and already done by the
  Java bindings — that also settles #1299 and #1701.
- **`Root-Is-Purelib: true`** in the generated wheel; should be false for a
  wheel carrying a shared library.
- **`manylinux_2_34`** reflects this build host's glibc. Real builds belong
  in a `manylinux` container for a lower floor.
- Only `x86_64` here. `aarch64`, `musllinux`, macOS `universal2` and Windows
  remain — upstream CI already builds the latter two (`mingw.yml`,
  `--enable-macos-universal-binary`).
- Packaging name: `louis` on PyPI is an unrelated 2013 stub; `liblouis`
  appears unregistered.
- No license/attribution bundling for the 475 tables yet.
