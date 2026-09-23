"""Vercel-only asset staging; local Flask serves static/ directly."""
from pathlib import Path
from shutil import copy2

ROOT = Path(__file__).resolve().parent
public = ROOT / 'public'
(public / 'static').mkdir(parents=True, exist_ok=True)
copy2(ROOT / 'static' / 'index.html', public / 'index.html')
for name in ('style.css', 'app.js'):
    copy2(ROOT / 'static' / name, public / 'static' / name)
print('Staged the dashboard in public/ for Vercel CDN delivery.')
