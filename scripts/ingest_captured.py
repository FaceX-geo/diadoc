"""Import completed local download captures while the browser collects batches."""
import json
import time
from pathlib import Path
from archive import ARCHIVE, connect, import_batch, build_index

db=connect()
failed={}
print('Waiting for verified Diadoc download captures',flush=True)
while True:
    for meta in sorted((ARCHIVE/'raw').glob('*.capture.json')):
        try:
            data=json.loads(meta.read_text())
            batch=data['batch']['batchId']
            if db.execute('SELECT 1 FROM batches WHERE id=?',(batch,)).fetchone():continue
            if failed.get(str(meta),0)>time.time()-120:continue
            source=meta.with_name(data['sha256']+'.zip')
            result=import_batch(db,meta,source)
            build_index(db)
            print(json.dumps(result,ensure_ascii=False),flush=True)
        except Exception as e:
            db.rollback()
            failed[str(meta)]=time.time()
            print(json.dumps({'error':str(e),'capture':meta.name},ensure_ascii=False),flush=True)
    time.sleep(1)
