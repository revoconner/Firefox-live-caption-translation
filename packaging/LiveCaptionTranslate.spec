# PyInstaller spec for the backend. Build with packaging\build.ps1, which runs it and then Inno Setup.
# onedir on purpose: onefile unpacks everything to a temp folder at every login and is the variant antivirus heuristics dislike.
# The dist folder is self contained and portable: _internal carries the nemo-speech runtime (bin), the two GGUF models (models), proc_loopback.exe (native) and the runtime licenses next to the Python side.
from pathlib import Path

root = Path(SPECPATH).parent
backend = root / "backend"
nemo = root / "nemo-speech-0.1.0"  # symlink to the developer install
if not nemo.is_dir():
    nemo = Path(r"C:\PortableProgs\nemo-speech-0.1.0")

datas = [
    (str(backend / "icon.ico"), "."),
    (str(nemo / "bin"), "bin"),
    (str(nemo / "share" / "licenses"), "licenses"),
    (str(backend / "native" / "proc_loopback.exe"), "native"),
]
for name in ("nemotron-3.5-asr-streaming-0.6b.q8_0.gguf", "riva-translate-4b-instruct-v2-q8_0.gguf"):
    datas.append((str(backend / "models" / name), "models"))

a = Analysis(
    [str(backend / "service.py")],
    pathex=[str(backend)],
    datas=datas,
    excludes=["pytest", "_pytest", "PyInstaller", "pip", "setuptools", "pkg_resources", "httpx", "httpcore", "anyio", "pygments", "tkinter", "unittest", "pydoc", "doctest", "lib2to3", "xmlrpc", "distutils"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, exclude_binaries=True, name="LiveCaptionTranslate", icon=str(backend / "icon.ico"), console=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="LiveCaptionTranslate", upx=False)
