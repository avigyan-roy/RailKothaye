"""Vercel-only asset staging; local Flask serves static/ directly."""
from pathlib import Path
from shutil import copy2

ROOT = Path(__file__).resolve().parent
for name in ('stations.json', 'trains.json', 'conditions.json', 'history.csv', 'evaluation.json'):
    if not (ROOT / 'data' / name).exists():
        raise SystemExit('Missing demo data. Run python generate_data.py and python evaluate.py before deploying.')
public = ROOT / 'public'
(public / 'static').mkdir(parents=True, exist_ok=True)
copy2(ROOT / 'static' / 'index.html', public / 'index.html')
for name in ('style.css', 'app.js'):
    copy2(ROOT / 'static' / name, public / 'static' / name)
print('Staged the dashboard in public/ for Vercel CDN delivery.')
