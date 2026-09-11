"""Preserve completed native Diadoc ZIPs before Safari auto-expands them.

Only reads local Downloads and writes this project's archive. No browser control.
"""
import hashlib
import json
import shutil
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOWNLOADS = Path.home() / 'Downloads'
DEST = ROOT / 'archive' / 'raw'
DEST.mkdir(parents=True, exist_ok=True)
seen = set()
bindings = {}
print('Watching completed Diadoc ZIP downloads', flush=True)
while True:
    if shutil.disk_usage(ROOT).free < 3 * 1024**3:
        print('STOP: less than 3 GiB free', flush=True)
        break
    candidates = list(DOWNLOADS.glob('Diadoc.Documents.*.zip'))
    candidates += list(DOWNLOADS.glob('Diadoc.Documents.*.zip.download/*.zip'))
    for source in candidates:
        try:
            identity = source.name
            if identity in seen:
                continue
            if identity not in bindings:
                plans = sorted(DOWNLOADS.glob('dd-batch-*.json'), key=lambda p: p.stat().st_mtime)
                if not plans:
                    continue
                bindings[identity] = json.loads(plans[-1].read_text())
            # The central directory is written only at the end of the ZIP.
            if not zipfile.is_zipfile(source):
                continue
            staging = DEST / (identity + '.partial')
            shutil.copyfile(source, staging)
            with zipfile.ZipFile(staging) as z:
                bad = z.testzip()
                if bad:
                    raise ValueError('ZIP CRC error: ' + bad)
                members = len([i for i in z.infolist() if not i.is_dir()])
            hasher = hashlib.sha256()
            with staging.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    hasher.update(block)
            digest = hasher.hexdigest()
            final = DEST / (digest + '.zip')
            staging.replace(final)
            final.chmod(0o600)
            meta = {'sourceName': identity, 'sha256': digest, 'bytes': final.stat().st_size,
                    'memberCount': members, 'capturedAt': time.time(), 'batch': bindings[identity]}
            final.with_suffix('.capture.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2))
            seen.add(identity)
            print(json.dumps({'captured': digest, 'members': members, 'bytes': meta['bytes'],
                              'batch': meta['batch']['batchId']}, ensure_ascii=False), flush=True)
        except (FileNotFoundError, PermissionError, zipfile.BadZipFile, OSError, ValueError) as exc:
            # A moving/growing download is retried; it is never counted as archived.
            continue
    time.sleep(0.25)
