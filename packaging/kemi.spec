# PyInstaller spec — build a standalone Kemi desktop app (no Python needed
# by the end user). Build on each target OS:
#
#   pip install pyinstaller pynacl
#   pyinstaller packaging/kemi.spec
#
# Output: dist/Kemi  (a single launcher that runs `kemi app` and opens the
# dashboard in the browser). On macOS a .app bundle is produced; on Windows a
# .exe; on Linux a self-contained binary. Code-signing/notarisation is left to
# the maintainer (platform-specific, needs developer certificates).
import sys

block_cipher = None

a = Analysis(
    ["entry.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=["kemi", "nacl", "_cffi_backend"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "torch", "transformers"],
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
    name="Kemi",
    console=False,           # GUI-style launch (opens the browser dashboard)
    disable_windowed_traceback=False,
    icon=None,               # set a platform icon here if you have one
)

if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="Kemi.app",
        bundle_identifier="com.kemi.app",
        info_plist={"LSUIElement": False, "NSHighResolutionCapable": True},
    )
