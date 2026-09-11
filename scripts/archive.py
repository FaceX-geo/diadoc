#!/usr/bin/env python3
"""Local Diadoc archive: SQLite, immutable files, verified imports and offline index."""
import argparse
import hashlib
import html
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import tempfile
import unicodedata
import zipfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / 'archive'

def now():
    return datetime.now(timezone.utc).isoformat()

def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()

def fingerprint(d):
    def normalize(s):return re.sub(r'\s+',' ',str(s or '')).strip()
    lines=[normalize(s) for s in (d.get('details') or '').split('\n')]
    details='\n'.join(s for s in lines if s and not s.isdigit())
    return json.dumps([normalize(d.get('name')),normalize(d.get('status')),details,
      d.get('dateTime') or None,normalize(d.get('counterparty'))],ensure_ascii=False,separators=(',',':'))

def normalize_old_fingerprint(value):
    parts=json.loads(value)
    return fingerprint(dict(zip(('name','status','details','dateTime','counterparty'),parts)))

def connect(base=ARCHIVE):
    base.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(base / 'archive.sqlite3')
    db.row_factory = sqlite3.Row
    db.executescript('''
      PRAGMA foreign_keys=ON;
      CREATE TABLE IF NOT EXISTS documents(
        key TEXT PRIMARY KEY, folder TEXT NOT NULL, box_id TEXT NOT NULL,
        letter_id TEXT NOT NULL, document_id TEXT NOT NULL, name TEXT NOT NULL,
        counterparty TEXT, status TEXT, fingerprint TEXT NOT NULL,
        archived_fingerprint TEXT, metadata TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS history(
        id INTEGER PRIMARY KEY, document_key TEXT NOT NULL REFERENCES documents(key),
        observed_at TEXT NOT NULL, metadata TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS versions(
        id TEXT PRIMARY KEY, document_key TEXT NOT NULL REFERENCES documents(key),
        fingerprint TEXT NOT NULL, imported_at TEXT NOT NULL, source TEXT NOT NULL,
        directory TEXT NOT NULL, batch_id TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS files(
        version_id TEXT NOT NULL REFERENCES versions(id), relative_path TEXT NOT NULL,
        sha256 TEXT NOT NULL, size INTEGER NOT NULL, stored_path TEXT NOT NULL,
        PRIMARY KEY(version_id,relative_path));
      CREATE TABLE IF NOT EXISTS scans(
        run TEXT NOT NULL, folder TEXT NOT NULL, complete INTEGER NOT NULL,
        pages INTEGER NOT NULL, documents INTEGER NOT NULL, imported_at TEXT NOT NULL,
        manifest TEXT NOT NULL, PRIMARY KEY(run,folder));
      CREATE TABLE IF NOT EXISTS batches(
        id TEXT PRIMARY KEY, expected INTEGER NOT NULL, verified INTEGER NOT NULL,
        imported_at TEXT NOT NULL, manifest TEXT NOT NULL, source TEXT NOT NULL);
    ''')
    # Normalize older snapshots that included purely visual row counters.
    for row in db.execute('SELECT key,metadata,archived_fingerprint FROM documents').fetchall():
        fp=fingerprint(json.loads(row['metadata']))
        archived=normalize_old_fingerprint(row['archived_fingerprint']) if row['archived_fingerprint'] else None
        db.execute('UPDATE documents SET fingerprint=?,archived_fingerprint=? WHERE key=?',(fp,archived,row['key']))
    db.commit()
    return db

def upsert(db, d):
    key = '/'.join([d['boxId'], d['letterId'], d['documentId']])
    fp = fingerprint(d)
    old = db.execute('SELECT fingerprint,metadata FROM documents WHERE key=?', (key,)).fetchone()
    d=dict(d)
    if old:
        prior=json.loads(old['metadata'])
        for field in ('listingPage','listingPosition','listedAt'):
            if field not in d and field in prior:d[field]=prior[field]
    metadata = json.dumps(d, ensure_ascii=False)
    db.execute('''INSERT INTO documents(key,folder,box_id,letter_id,document_id,name,counterparty,status,fingerprint,metadata,first_seen,last_seen)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
      folder=excluded.folder,name=excluded.name,counterparty=excluded.counterparty,status=excluded.status,
      fingerprint=excluded.fingerprint,metadata=excluded.metadata,last_seen=excluded.last_seen''',
      (key,d['folder'],d['boxId'],d['letterId'],d['documentId'],d['name'],d.get('counterparty'),d.get('status'),fp,metadata,now(),now()))
    if not old or old['fingerprint'] != fp:
        db.execute('INSERT INTO history(document_key,observed_at,metadata) VALUES(?,?,?)', (key,now(),metadata))
    return key, fp

def store_manifest(base, name, data):
    path = base / 'manifests' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2))
    path.chmod(0o600)
    return str(path.relative_to(base))

def import_index(db, source, base=ARCHIVE):
    data = json.loads(source.read_text())
    pages = data.get('pages', [])
    if not pages:
        raise ValueError('Index has no observed pages')
    folders = {p['folder'] for p in pages}
    if len(folders) != 1:
        raise ValueError('Mixed folders')
    folder = next(iter(folders))
    seen = set()
    page_numbers = []
    for page in pages:
        page_numbers.append(page['page'])
        for position,doc in enumerate(page['documents']):
            key, _ = upsert(db, {**doc,'listingPage':page['page'],'listingPosition':position,'listedAt':page.get('capturedAt')})
            seen.add(key)
    complete = bool(data.get('complete'))
    if complete and page_numbers != list(range(1,len(pages)+1)):
        raise ValueError('Non-contiguous full scan')
    if not seen and not pages[0].get('emptyConfirmed'):
        raise ValueError('Zero records without an explicit empty-state confirmation')
    name = store_manifest(base, source.name, data)
    db.execute('INSERT OR REPLACE INTO scans VALUES(?,?,?,?,?,?,?)',
      (data['run'],folder,int(complete),len(pages),len(seen),now(),name))
    db.commit()
    return {'folder':folder,'pages':len(pages),'documents':len(seen),'complete':complete}

def safari_name(name):
    name = unicodedata.normalize('NFC', name)
    try:
        return name.encode('mac_cyrillic').decode('cp866')
    except UnicodeError:
        return name

def safe_path(name):
    path = PurePosixPath(name.replace('\\','/'))
    if path.is_absolute() or '..' in path.parts or '\0' in name:
        raise ValueError('Unsafe archive path')
    return path

def expand_zip(source, dest):
    with zipfile.ZipFile(source) as z:
        bad = z.testzip()
        if bad:
            raise ValueError('ZIP CRC mismatch: ' + bad)
        if sum(i.file_size for i in z.infolist()) > shutil.disk_usage(dest).free - 3*1024**3:
            raise ValueError('Not enough disk space to extract archive')
        for info in z.infolist():
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('Symlinks are not allowed in document archives')
            name = info.filename
            if not info.flag_bits & 0x800:
                name = name.encode('cp437').decode('cp866')
            target = dest.joinpath(*safe_path(name).parts)
            if info.is_dir():
                target.mkdir(parents=True,exist_ok=True)
            else:
                target.parent.mkdir(parents=True,exist_ok=True)
                with z.open(info) as a, open(target,'wb') as b:
                    shutil.copyfileobj(a,b)

def protocol_id(path):
    from pypdf import PdfReader
    reader = PdfReader(path)
    text = '\n'.join(p.extract_text() or '' for p in reader.pages)
    match = re.search(r'Идентификатор файла\s+([a-fA-F0-9-]{32,36})', text)
    return match[1].replace('-','').lower() if match else None

def import_batch(db, manifest, source, base=ARCHIVE):
    if shutil.disk_usage(base).free < 3*1024**3:
        raise ValueError('Less than 3 GiB free')
    data = json.loads(manifest.read_text())
    data = data.get('batch',data)
    if not data['documents']:
        return {'batch':data['batchId'],'verified':0,'skipped':True}
    if db.execute('SELECT 1 FROM batches WHERE id=?',(data['batchId'],)).fetchone():
        return {'batch':data['batchId'],'already_imported':True}
    expected = {d['documentId'].replace('-','').lower():d for d in data['documents']}
    if len(expected) != len(data['documents']):
        raise ValueError('Duplicate document IDs in batch')
    with tempfile.TemporaryDirectory(prefix='dd-import-',dir=base) as temp:
        extracted = Path(temp)
        source_is_zip = source.is_file()
        if source_is_zip:
            expand_zip(source,extracted)
            content = extracted
            display_name = lambda s:s
        else:
            content = source
            display_name = safari_name
        matched = {}
        for protocol in content.rglob('*.pdf'):
            if display_name(protocol.name).lower() != 'протокол.pdf':
                continue
            identifier = protocol_id(protocol)
            if identifier in expected:
                if identifier in matched:
                    raise ValueError('Duplicate protocol for document ' + identifier)
                matched[identifier] = protocol.parent
        if set(matched) != set(expected):
            raise ValueError(f'Batch coverage mismatch: expected {len(expected)}, matched {len(matched)}, missing {sorted(set(expected)-set(matched))}')
        # Validate complete coverage before committing any batch as complete.
        count_files=0
        for identifier, folder in matched.items():
            doc=expected[identifier]
            entries=[]
            for file in sorted(folder.rglob('*')):
                if file.is_symlink():
                    raise ValueError('Symlink in expanded archive')
                if not file.is_file() or file.name=='.DS_Store':
                    continue
                relative='/'.join(display_name(p) for p in file.relative_to(folder).parts)
                safe_path(relative)
                if file.stat().st_size==0:
                    raise ValueError('Empty document file: '+relative)
                entries.append((relative,digest(file),file.stat().st_size,file))
            if len({e[0] for e in entries}) != len(entries):
                raise ValueError('Filename collision after encoding recovery')
            if not any(e[0].lower().endswith(('.sgn','.sig','.p7s')) for e in entries):
                raise ValueError('No signatures in full exchange bundle')
            key,fp=upsert(db,doc)
            version=hashlib.sha256((key+json.dumps([(e[0],e[1]) for e in entries],ensure_ascii=False)).encode()).hexdigest()
            directory=Path('documents')/doc['folder']/doc['documentId']/version[:16]
            (base/directory).mkdir(parents=True,exist_ok=True)
            db.execute('INSERT OR IGNORE INTO versions VALUES(?,?,?,?,?,?,?)',
              (version,key,fp,now(),str(source),str(directory),data['batchId']))
            for relative,sha,size,file in entries:
                obj=base/'objects'/sha[:2]/sha
                obj.parent.mkdir(parents=True,exist_ok=True)
                corrupt = obj.exists() and (obj.stat().st_size != size or digest(obj)!=sha)
                if not obj.exists() or corrupt:
                    tmp=obj.with_suffix('.partial');shutil.copyfile(file,tmp)
                    if digest(tmp)!=sha:
                        raise ValueError('Copy hash mismatch')
                    if corrupt:
                        quarantine=base/'quarantine'/(sha+'-'+datetime.now().strftime('%Y%m%dT%H%M%S%f'))
                        quarantine.parent.mkdir(parents=True,exist_ok=True)
                        obj.replace(quarantine)
                    tmp.replace(obj);obj.chmod(0o600)
                    if corrupt:
                        for old in db.execute('SELECT stored_path FROM files WHERE sha256=?',(sha,)):
                            old_path=base/old['stored_path']
                            if old_path.exists() or old_path.is_symlink():old_path.unlink()
                            old_path.parent.mkdir(parents=True,exist_ok=True)
                            os.link(obj,old_path)
                target=base/directory/relative
                target.parent.mkdir(parents=True,exist_ok=True)
                if target.exists() and digest(target)!=sha:
                    quarantine=base/'quarantine'/(sha+'-link-'+datetime.now().strftime('%Y%m%dT%H%M%S%f'))
                    quarantine.parent.mkdir(parents=True,exist_ok=True)
                    target.replace(quarantine)
                if not target.exists():os.link(obj,target)
                if digest(target)!=sha:raise ValueError('Document link hash mismatch')
                db.execute('INSERT OR IGNORE INTO files VALUES(?,?,?,?,?)',(version,relative,sha,size,str(target.relative_to(base))))
                count_files+=1
            db.execute('UPDATE documents SET archived_fingerprint=? WHERE key=?',(fp,key))
        stored=store_manifest(base,'batch-'+data['batchId']+'.json',data)
        db.execute('INSERT INTO batches VALUES(?,?,?,?,?,?)',(data['batchId'],len(expected),len(matched),now(),stored,str(source)))
        db.commit()
    return {'batch':data['batchId'],'verified':len(matched),'files':count_files}

def status(db):
    return {'documents':[dict(r) for r in db.execute('''SELECT folder,COUNT(*) total,
      SUM(archived_fingerprint IS NOT NULL) archived,
      COALESCE(SUM(archived_fingerprint=fingerprint),0) current FROM documents GROUP BY folder''')],
      'scans':[dict(r) for r in db.execute('SELECT folder,complete,pages,documents,imported_at FROM scans ORDER BY imported_at')],
      'files':db.execute('SELECT COUNT(*) FROM files').fetchone()[0],
      'unique_files':db.execute('SELECT COUNT(DISTINCT sha256) FROM files').fetchone()[0],
      'unique_bytes':db.execute('SELECT COALESCE(SUM(size),0) FROM (SELECT sha256,MAX(size) size FROM files GROUP BY sha256)').fetchone()[0],
      'batches':db.execute('SELECT COUNT(*) FROM batches').fetchone()[0]}

def verify(db,base=ARCHIVE):
    issues=[];checked=set();checked_inodes={};bad_documents=set()
    for row in db.execute('SELECT stored_path,sha256,size FROM files'):
        file=base/row['stored_path']
        if file.is_symlink() or not file.is_file() or file.stat().st_size != row['size']:
            issues.append(row['stored_path']);continue
        stat=file.stat();inode=(stat.st_dev,stat.st_ino)
        if inode not in checked_inodes:checked_inodes[inode]=digest(file)
        if checked_inodes[inode]!=row['sha256']:issues.append(row['stored_path'])
        checked.add(row['sha256'])
    for path in issues:
        for row in db.execute('SELECT DISTINCT v.document_key FROM files f JOIN versions v ON v.id=f.version_id WHERE f.stored_path=?',(path,)):
            bad_documents.add(row['document_key'])
    return {'checked_hashes':len(checked),'issues':issues,'affected_documents':sorted(bad_documents)}

def build_index(db,base=ARCHIVE):
    esc=html.escape
    labels={'Inbox':'Входящие','Outbox':'Исходящие','Internal':'Внутренние'}
    records=[]
    for d in db.execute("SELECT * FROM documents ORDER BY folder,CAST(json_extract(metadata,'$.listingPage') AS INTEGER),CAST(json_extract(metadata,'$.listingPosition') AS INTEGER),name"):
        versions=[]
        for v in db.execute('SELECT * FROM versions WHERE document_key=? ORDER BY imported_at DESC',(d['key'],)):
            files=[dict(f) for f in db.execute('SELECT relative_path,stored_path,size FROM files WHERE version_id=? ORDER BY relative_path',(v['id'],))]
            versions.append({'date':v['imported_at'],'files':files})
        metadata=json.loads(d['metadata'])
        records.append({'key':d['key'],'folder':labels[d['folder']],'name':d['name'],'counterparty':d['counterparty'],'status':d['status'],'date':metadata.get('dateTime') or metadata.get('dateText') or '',
                        'archived':bool(d['archived_fingerprint']),'current':d['archived_fingerprint']==d['fingerprint'],'versions':versions})
    summary=status(db)
    payload=json.dumps({'records':records,'summary':summary,'generatedAt':now()},ensure_ascii=False).replace('<','\\u003c')
    template='''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Локальный архив Диадока</title><style>
*{box-sizing:border-box}body{margin:0;background:#f3f5f4;color:#172b25;font:15px system-ui,sans-serif}main{max-width:1280px;margin:36px auto;padding:0 24px}h1{margin-bottom:6px;font-size:32px}.muted{color:#61776d}header{margin-bottom:28px}.stats{display:flex;gap:14px;flex-wrap:wrap;margin:24px 0}.card{padding:18px 24px;background:white;border:1px solid #dce5df;border-radius:12px}.card b{display:block;font-size:27px}input,select{padding:13px;border:1px solid #bfd0c5;border-radius:8px;font:inherit}input{flex:1;min-width:220px}.filters{display:flex;gap:12px;margin:24px 0}table{width:100%;border-collapse:collapse;background:white}th,td{padding:15px;text-align:left;border-bottom:1px solid #e1e8e3;vertical-align:top}th{color:#61776d;font-weight:500}small{color:#61776d}a{color:#126c52}summary{cursor:pointer}.ok{color:#157348}.missing{color:#a15014}.filelist{max-height:380px;overflow:auto;max-width:520px;word-break:break-word}li{margin:9px 0}.scroll{overflow:auto}button{padding:9px 16px;background:#fff;border:1px solid #bfd0c5;border-radius:7px;cursor:pointer}footer{margin:20px 0}.badge{white-space:nowrap}@media(max-width:650px){main{padding:0 12px}.filters{flex-wrap:wrap}td,th{padding:10px}}</style>
<main><header><p class="muted">НА КОМПЬЮТЕРЕ · БЕЗ ПОДКЛЮЧЕНИЯ К ДИАДОКУ</p><h1>Архив документов</h1><div id="updated" class="muted"></div></header><div class="stats" id="stats"></div><p id="coverage" class="muted"></p><div class="filters"><input id="query" placeholder="Контрагент, номер, название или дата"><select id="folder"><option>Все разделы</option><option>Входящие</option><option>Исходящие</option><option>Внутренние</option></select><select id="state"><option>Все документы</option><option>Есть файлы</option><option>Нужна загрузка</option></select></div><p id="count" class="muted"></p><div class="scroll"><table><thead><tr><th>Документ / контрагент</th><th>Раздел</th><th>Дата в реестре</th><th>Статус в Диадоке</th><th>Локальные файлы</th></tr></thead><tbody id="rows"></tbody></table></div><footer><button id="prev">Назад</button> <span id="pagination"></span> <button id="next">Далее</button></footer><p class="muted">Обновление запускается отдельно после вашего подтверждения. Эта страница не обращается к Диадоку. Оригиналы и подписи сохранены без изменения содержимого.</p></main>
<script id="data" type="application/json">PAYLOAD</script><script>
const data=JSON.parse(document.getElementById('data').textContent);let page=0;const $=id=>document.getElementById(id);const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const total=data.records.length,archived=data.records.filter(x=>x.archived).length;
$('updated').textContent='Состояние архива: '+new Date(data.generatedAt).toLocaleString('ru-RU');$('stats').innerHTML=[['Документов',total],['С файлами',archived],['Ожидают загрузки',total-archived],['Файлов',data.summary.files]].map(([s,n])=>'<div class="card"><b>'+n+'</b>'+s+'</div>').join('');
$('coverage').textContent=['Входящие','Исходящие','Внутренние'].map((name,i)=>{const folder=['Inbox','Outbox','Internal'][i];const scans=data.summary.scans.filter(s=>s.folder===folder);const scan=scans[scans.length-1];return name+': '+(!scan?'реестр ещё не собран':!scan.complete?'реестр неполный':scan.documents===0?'документов нет':'реестр собран ('+scan.documents+')')}).join(' · ');
function render(){let q=$('query').value.toLowerCase(),f=$('folder').value,s=$('state').value;let all=data.records.filter(d=>(f==='Все разделы'||d.folder===f)&&(s==='Все документы'||(s==='Есть файлы'?d.archived:!d.archived))&&[d.name,d.counterparty,d.status,d.date].join(' ').toLowerCase().includes(q));let n=Math.max(1,Math.ceil(all.length/40));page=Math.min(page,n-1);$('count').textContent='Найдено: '+all.length;$('pagination').textContent=(page+1)+' / '+n;$('prev').disabled=page===0;$('next').disabled=page>=n-1;
$('rows').innerHTML=all.slice(page*40,page*40+40).map(d=>'<tr><td><strong>'+esc(d.name)+'</strong><br><small>'+esc(d.counterparty)+'</small></td><td>'+esc(d.folder)+'</td><td>'+esc(d.date)+'</td><td>'+esc(d.status)+'</td><td>'+(d.archived?'<span class="ok">Сохранено</span>'+(!d.current?'<br><small>Есть изменения в реестре</small>':'')+d.versions.map((v,i)=>'<details><summary>'+(i?'Предыдущая версия':'Открыть файлы')+' ('+v.files.length+')</summary><ul class="filelist">'+v.files.map(f=>'<li><a href="'+f.stored_path.split('/').map(encodeURIComponent).join('/')+'" target="_blank">'+esc(f.relative_path)+'</a> <small>'+Math.ceil(f.size/1024)+' КБ</small></li>').join('')+'</ul></details>').join(''):'<span class="missing">Не загружено</span>')+'</td></tr>').join('');}
for(const id of ['query','folder','state'])$(id).oninput=()=>{page=0;render()};$('prev').onclick=()=>{page--;render()};$('next').onclick=()=>{page++;render()};render();</script></html>'''
    (base/'index.html').write_text(template.replace('PAYLOAD',payload))
    return {'index':str(base/'index.html'),'documents':len(records)}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest='command',required=True)
    i=sub.add_parser('import-index');i.add_argument('source',type=Path,nargs='+')
    b=sub.add_parser('import-batch');b.add_argument('manifest',type=Path);b.add_argument('source',type=Path)
    sub.add_parser('status');sub.add_parser('verify');sub.add_parser('build-index');sub.add_parser('make-launch');sub.add_parser('requeue-damaged')
    args=p.parse_args();db=connect()
    if args.command=='import-index':result=[import_index(db,s) for s in args.source]
    elif args.command=='import-batch':result=import_batch(db,args.manifest,args.source)
    elif args.command=='status':result=status(db)
    elif args.command=='verify':result=verify(db)
    elif args.command=='build-index':result=build_index(db)
    elif args.command=='requeue-damaged':
        result=verify(db)
        for key in result['affected_documents']:
            db.execute('UPDATE documents SET archived_fingerprint=NULL WHERE key=?',(key,))
        db.commit()
    else:
        check=verify(db)
        affected=set(check['affected_documents'])
        known={r['key']:r['archived_fingerprint'] for r in db.execute('SELECT key,archived_fingerprint FROM documents WHERE archived_fingerprint IS NOT NULL') if r['key'] not in affected}
        launch='window.DD_ARCHIVE_KNOWN='+json.dumps(known,ensure_ascii=False)+';\n'+(ROOT/'scripts/collector.js').read_text()
        (ARCHIVE/'launch.js').write_text(launch)
        result={'launch':str(ARCHIVE/'launch.js'),'known':len(known),'repair_required':len(affected)}
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
