import os
import sysconfig
from pathlib import Path


project_dir = Path(SPECPATH)

# The tkinterdnd2 hook needs the architecture of the build interpreter.
if os.name != "nt" or sysconfig.get_platform() != "win-amd64":
    raise SystemExit("Build this executable using 64-bit x86 Windows Python.")
os.environ["PROCESSOR_ARCHITECTURE"] = "AMD64"

analysis = Analysis(
    [str(project_dir / "q_bounce_gui.py")],
    datas=[(str(project_dir / "q_bounce_config.json"), ".")],
    excludes=[
        # The app processes local files in a background thread.
        "_hashlib",
        "multiprocessing",
        "ssl",
        # Pillow's NumPy and FreeType integrations are unused. AVIF is not in
        # the file picker's supported formats.
        "numpy",
        "PIL._avif",
        "PIL._imagingft",
    ],
    optimize=2,
)

# Windows 10 and 11 supply these, even if the build machine also has copies
# in a tool directory on PATH. Keep the Visual C++ runtime used by Python.
analysis.binaries = [
    entry for entry in analysis.binaries
    if entry[0].lower() != "ucrtbase.dll"
    and not entry[0].lower().startswith(("api-ms-win-core-", "api-ms-win-crt-"))
]
archive = PYZ(analysis.pure)

exe = EXE(
    archive,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="BouncyMemeGenerator",
    console=False,
    upx=os.environ.get("BOUNCY_MEME_UPX", "1") == "1",
)
