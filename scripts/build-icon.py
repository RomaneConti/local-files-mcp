from pathlib import Path
from local_files_mcp.tray import icon_image

folder = Path(__file__).resolve().parents[1] / "build" / "assets"
folder.mkdir(parents=True, exist_ok=True)
icon_image("#22c55e").save(folder / "icon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
