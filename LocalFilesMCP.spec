# Windowed bundle. Runtime configuration and secrets intentionally stay outside it.
from pathlib import Path
from PyInstaller.utils.hooks import copy_metadata

project = Path(SPECPATH)
vendor = project / 'vendor' / 'tunnel-client'
if not (vendor / 'tunnel-client.exe').is_file():
    raise SystemExit('Missing pinned tunnel-client. Run build.ps1.')
# Explicit release files only; no checkout config, runtime, credentials or user data.
vendor_data = [(str(vendor / name), 'vendor/tunnel-client') for name in ('LICENSE', 'NOTICE')]
vendor_data += [(str(path), 'vendor/tunnel-client') for path in vendor.glob('*-licenses.txt')]
analysis = Analysis(
    [str(project / 'scripts' / 'tray-entry.py')],
    pathex=[str(project / 'src')],
    binaries=[(str(vendor / 'tunnel-client.exe'), 'vendor/tunnel-client')],
    datas=copy_metadata('mcp', recursive=True) + vendor_data,
    hiddenimports=[
        'pystray._win32', 'uvicorn.logging', 'uvicorn.loops.auto',
        'uvicorn.protocols.http.auto', 'uvicorn.protocols.http.h11_impl',
        'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan.on',
    ],
    hookspath=[], runtime_hooks=[], excludes=['pytest'], noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz, analysis.scripts, [], exclude_binaries=True,
    name='LocalFilesMCP', debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False, disable_windowed_traceback=True,
    icon=str(project / 'build' / 'assets' / 'icon.ico'),
)
collect = COLLECT(exe, analysis.binaries, analysis.datas,
                  strip=False, upx=False, name='LocalFilesMCP')
