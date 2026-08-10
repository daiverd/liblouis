# Testing the win_amd64 liblouis wheel

Instructions for verifying `liblouis-3.38.0-py3-none-win_amd64.whl` on real
Windows. It is cross-compiled from Linux with mingw-w64, so every run on
Windows is the first real test of it.

Written for an agent working on the Windows side. Please run it as given and
report the full output, pass or fail.

> **Round 2.** Round 1 passed checks 1–4 and failed 5–8 with
> `Cannot resolve table 'en-ueb-g2.ctb'`. Root cause, diagnosed from that
> report: `liblouis.dll` links **msvcrt** and snapshots its own environment
> block, so a **ucrt** Python's `os.environ["LOUIS_TABLEPATH"]` never reached
> the library's `getenv`.
>
> The wheel no longer uses that environment variable to find tables. Bare
> table names are now resolved to absolute paths inside the bundled
> `_tables` directory, in `_createTableBuf`. **Reinstall the wheel before
> retesting** — and please do *not* set `LOUIS_TABLEPATH` in the shell this
> time, since the point is that it should work without it.

## What is being tested, and why it might fail

The wheel bundles `liblouis.dll` and 474 translation tables, so it should
work with no system liblouis and no compiler. Three things could plausibly
break, and the checks below separate them:

1. **The DLL doesn't load.** It imports only `KERNEL32.dll` and `msvcrt.dll`
   (verified with `objdump`), so it should have no MinGW runtime dependency —
   but that was checked statically, never by actually loading it.
2. **The wrong liblouis loads.** NVDA and some other Windows software install
   their own `liblouis.dll`, sometimes in `System32`. The wheel patches the
   bindings to load its bundled copy *by explicit path*, and this must win.
3. **Tables aren't found.** The wheel sets `LOUIS_TABLEPATH` to its own
   directory at import. Path semantics differ on Windows, so this is the
   likeliest failure.

## Prerequisites

- Windows Python **3.10 or newer** (the bindings use PEP 604 unions in
  signatures, which raise on 3.9).
- The wheel at `C:\Users\David\liblouis-3.38.0-py3-none-win_amd64.whl`.
  If it isn't there, it is also in WSL at
  `~/src/liblouis-fork/python/wheel/dist/`.

**Use a throwaway virtualenv.** Do not install into the `cad-braille`
Windows `.venv` — that environment is in use and this wheel is unverified.

## Step 1 — create a clean venv and install

PowerShell:

```powershell
py -3 -m venv $env:TEMP\liblouis-test
& $env:TEMP\liblouis-test\Scripts\python.exe -m pip install --quiet --upgrade pip
& $env:TEMP\liblouis-test\Scripts\python.exe -m pip install C:\Users\David\liblouis-3.38.0-py3-none-win_amd64.whl
```

The install succeeding is itself check #1: it proves pip accepts the
`py3-none-win_amd64` tag on this interpreter.

If pip says the wheel "is not a supported wheel on this platform", report the
output of `python -c "import sysconfig;print(sysconfig.get_platform())"` and
stop — that is a tagging problem, not a runtime one.

## Step 2 — run the checks

Save as `%TEMP%\test_liblouis.py` and run with the venv's python.

Braille characters are printed as `U+XXXX` codepoints rather than glyphs, so a
console that can't render braille won't cause a false failure.

```python
import os, sys, glob

def show(s):
    return " ".join(f"U+{ord(c):04X}" for c in s)

results = []
def check(name, ok, detail):
    results.append((ok, name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")

import louis

# 1. the bindings come from the wheel, not a system install
inside = "site-packages" in louis.__file__.replace("\\", "/")
check("bindings from wheel", inside, louis.__file__)

# 2. the DLL actually loaded is the bundled one, not a system copy
dll = louis.liblouis._name
check("bundled DLL loaded", "site-packages" in dll.replace("\\", "/"), dll)

# 3. is there a competing system liblouis? (informational, not a failure)
sysdll = glob.glob(r"C:\Windows\System32\liblouis*.dll")
print(f"INFO  system liblouis present: {sysdll or 'none'}")

# 4. version and character size
check("version", louis.version().startswith("3.38"), louis.version())
check("charSize is UCS-4", louis.wideCharBytes == 4, str(louis.wideCharBytes))

# 5. tables are found, and contraction works (this exercises LOUIS_TABLEPATH)
tp = os.environ.get("LOUIS_TABLEPATH", "<unset>")
print(f"INFO  LOUIS_TABLEPATH = {tp}")
mode = louis.dotsIO | louis.ucBrl
got = louis.translateString(["en-ueb-g2.ctb"], "knowledge", mode=mode)
check("contracted UEB", got == "\u2805", f"'knowledge' -> {show(got)} (expect U+2805)")

# 6. numbers get a number sign
got = louis.translateString(["en-ueb-g2.ctb"], "123", mode=mode)
check("number sign", got == "\u283C\u2801\u2803\u2809",
      f"'123' -> {show(got)} (expect U+283C U+2801 U+2803 U+2809)")

# 7. a second table, to prove table lookup isn't a one-off
got = louis.translateString(["en-ueb-g1.ctb"], "knowledge", mode=mode)
check("uncontracted table loads", len(got) == 9, f"9 cells expected, got {len(got)}")

# 8. back-translation round-trip
back = louis.backTranslateString(["en-ueb-g2.ctb"], "\u2805", mode=mode)
check("back-translation", "knowledge" in back.lower(), repr(back))

failed = [r for r in results if not r[0]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
```

```powershell
& $env:TEMP\liblouis-test\Scripts\python.exe $env:TEMP\test_liblouis.py
```

## Expected output

All 8 checks `PASS`, ending `8/8 passed`.

Every expected value above was first verified against the **Linux** wheel
built from the same source and the same tables, so a deviation on Windows is
a real platform difference rather than a wrong expectation:

```
knowledge (g2) : U+2805
123 (g2)       : U+283C U+2801 U+2803 U+2809
knowledge (g1) : 9 cells
backTranslate  : 'knowledge'
```
 The two `INFO` lines are context,
not pass/fail — but **please include them in the report**, especially whether
a system liblouis exists, since a pass on a machine that has one is much
stronger evidence than a pass on one that doesn't.

## If something fails

Report the full output plus:

- **`ImportError` / `OSError` on `import louis`** — the DLL failed to load.
  Get the real reason with:
  ```powershell
  & $env:TEMP\liblouis-test\Scripts\python.exe -c "import ctypes,glob,os; p=glob.glob(os.path.join([s for s in __import__('site').getsitepackages() if 'site-packages' in s][0],'louis','_lib','*.dll'))[0]; print(p); ctypes.CDLL(p)"
  ```
  A missing-dependency error here means something MinGW-specific leaked in
  despite `-static-libgcc`.
- **Checks 5–7 fail with a table error** — `LOUIS_TABLEPATH` isn't working on
  Windows. Report `louis.getTable` behaviour and the `INFO` path. The fix is
  a custom table resolver via `lou_registerTableResolver` rather than the
  environment variable.
- **Check 2 fails** — a system liblouis won. Report which path loaded.

## Cleanup

```powershell
Remove-Item -Recurse -Force $env:TEMP\liblouis-test, $env:TEMP\test_liblouis.py
```

Nothing outside `%TEMP%` is modified by any of this.
