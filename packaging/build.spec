# PyInstaller spec for the ERP -> WooCommerce sync tool.
#
# Build (on Windows, with requirements-dev.txt installed):
#   pyinstaller packaging/build.spec
#
# Before building, drop a 64-bit fbclient.dll (matching your Python's
# bitness) into packaging/fbclient/ -- get it from a Firebird "client-only"
# install kit at https://firebirdsql.org, do not source it from a random
# DLL-download site. It is intentionally not committed to this repo.

import os

block_cipher = None
spec_dir = os.path.dirname(os.path.abspath(SPEC))
fbclient_dll = os.path.join(spec_dir, "fbclient", "fbclient.dll")

binaries = []
if os.path.isfile(fbclient_dll):
    binaries.append((fbclient_dll, "."))
else:
    print(f"WARNING: {fbclient_dll} not found -- the built exe will not be "
          f"able to connect to Firebird until it's added and rebuilt.")

a = Analysis(
    [os.path.join(spec_dir, "..", "app", "main.py")],
    pathex=[os.path.join(spec_dir, "..")],
    binaries=binaries,
    datas=[],
    hiddenimports=["fdb"],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# Passing a.binaries/a.zipfiles/a.datas directly into EXE (rather than a
# separate COLLECT step) is what produces a single-file ("onefile") exe.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="erp-woocommerce-sync",
    console=False,
)
