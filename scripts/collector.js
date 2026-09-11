/* Runs only inside the user's Diadoc tab. No fetch/XHR/cookies/API calls. */
(() => {
  'use strict';
  if (location.hostname !== 'diadoc.kontur.ru') throw Error('Нужна вкладка Диадока');
  if (window.DDArchive?.busy) throw Error('Сборщик уже работает');
  const previous = window.DDArchive;
  document.getElementById('dd-local-panel')?.remove();
  const run = new Date().toISOString().replace(/[:.]/g, '-');
  const known = window.DD_ARCHIVE_KNOWN || {};
  const txt = e => (e?.innerText || '').trim();
  const q = tid => document.querySelector('[data-tid="' + tid + '"]');
  const rows = () => [...document.querySelectorAll('a[data-tid="singleDocument"]')];
  const key = d => [d.boxId, d.letterId, d.documentId].join('/');
  const panel = document.createElement('aside');
  panel.id = 'dd-local-panel';
  panel.style.cssText = 'position:fixed;right:14px;top:72px;z-index:2147483647;background:#fff;border:2px solid #197a65;border-radius:10px;padding:14px;width:330px;box-shadow:0 4px 20px #0003;font:14px system-ui;color:#182b26';
  const heading = document.createElement('strong'); heading.textContent = 'Локальный архив Диадока'; panel.append(heading);
  const status = document.createElement('p'); status.style.whiteSpace = 'pre-wrap'; panel.append(status);
  const api = {busy:false, stopped:false, run, pages:previous?.pages||[], known, batch:null};
  const say = s => {status.textContent=s; console.info('DD_ARCHIVE',s);};
  const delay = ms => new Promise(r=>setTimeout(r,ms));
  const folder = () => location.pathname.match(/\/Folder\/(Inbox|Outbox|Internal)$/)?.[1];
  const normalize = s => String(s||'').replace(/\s+/g,' ').trim();
  const stableDetails = s => String(s||'').split('\n').map(normalize).filter(s=>s&&!/^\d+$/.test(s)).join('\n');
  const fingerprint = d => JSON.stringify([normalize(d.name),normalize(d.status),stableDetails(d.details),d.dateTime||null,normalize(d.counterparty)]);
  const signature = () => rows().map(a=>a.href).join('\n');
  const save = (name,obj) => {
    const u=URL.createObjectURL(new Blob([JSON.stringify(obj,null,2)],{type:'application/json;charset=utf-8'}));
    const a=document.createElement('a'); a.href=u;a.download=name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(u),60000);
  };
  function read() {
    if (!folder()) throw Error('Откройте один из трёх разделов документов');
    return {schema:1,run,capturedAt:new Date().toISOString(),folder:folder(),boxId:location.pathname.split('/')[1],sourceUrl:location.href,
      page:Number(new URL(location.href).searchParams.get('PageNumber')||1),navigation:txt(q('Navigation')),
      emptyConfirmed:!![...document.querySelectorAll('h2,h3')].find(e=>/документов нет|документы не найдены/i.test(txt(e))),
      documents:rows().map(a=>{const u=new URL(a.href),f=t=>txt(a.querySelector('[data-tid="'+t+'"]')),date=a.querySelector('[data-tid="Date"]');
        const d={folder:folder(),boxId:location.pathname.split('/')[1],letterId:u.searchParams.get('letterId'),documentId:u.searchParams.get('documentId'),url:u.href,
          counterparty:f('CountragentBaseView')||txt(a.closest('[data-tid="singleLetter"]')?.querySelector('[data-tid="CountragentBaseView"]')),
          name:f('documentName')||txt(a.children[1]),status:f('PrimaryDocumentStatus'),dateText:f('Date'),dateTime:date?.getAttribute('datetime')||null,details:txt(a.children[1]),rawText:txt(a)};
        if(!d.letterId||!d.documentId||!d.name) throw Error('Не удалось извлечь идентификатор или название');
        d.key=key(d);d.fingerprint=fingerprint(d); return d;
      })};
  }
  async function stable(oldSignature=null) {
    const deadline=Date.now()+90000; let last='',count=0;
    while(Date.now()<deadline) {
      if(api.stopped) throw Error('Остановлено пользователем');
      const s=signature();
      if(s && s!==oldSignature && s===last) count++; else count=0;
      last=s;
      if(count>=3) return;
      if(!s&&read().emptyConfirmed) return;
      await delay(750);
    }
    throw Error('Список не загрузился или не изменился за 90 секунд');
  }
  async function next() {
    const b=q('showNext');
    if(!b) throw Error('Не найдена кнопка Следующая');
    if(b.disabled||b.getAttribute('aria-disabled')==='true') return false;
    const before=signature(),p=read().page;
    b.click();await stable(before);
    if(read().page!==p+1) throw Error('Не подтверждён переход на следующую страницу');
    return true;
  }
  async function scan() {
    if(api.busy) return;api.busy=true;api.stopped=false;
    const initialFolder=folder();
    try {
      await stable();
      if(read().page===1) api.pages=[];
      else if(api.pages.length!==read().page-1||api.pages.some(p=>p.folder!==initialFolder)) throw Error('Начните с первой страницы или восстановите контрольную точку');
      const seen=new Set(api.pages.map(p=>p.documents.map(d=>d.url).join('\n')));
      for(let i=0;i<2000;i++) {
        if(folder()!==initialFolder) throw Error('Раздел изменился');
        const p=read(),s=signature();if(seen.has(s)) throw Error('Повтор страницы');seen.add(s);api.pages.push(p);
        say(initialFolder+': считано страниц '+api.pages.length+', документов '+api.pages.reduce((n,p)=>n+p.documents.length,0));
        if(p.emptyConfirmed||!(await next())) {
          const result={schema:1,run,folder:initialFolder,complete:true,completedAt:new Date().toISOString(),pages:api.pages};
          save('dd-index-'+initialFolder+'-'+run+'.json',result);say('Реестр '+initialFolder+' завершён: '+api.pages.length+' страниц. JSON сохранён.');return result;
        }
        if(api.pages.length%10===0) save('dd-checkpoint-'+initialFolder+'-'+run+'-'+api.pages.length+'.json',{schema:1,run,folder:initialFolder,complete:false,pages:api.pages});
      }
      throw Error('Превышен предел 2000 страниц');
    } catch(e) {save('dd-interrupted-'+run+'.json',{schema:1,run,complete:false,error:String(e),pages:api.pages});say(String(e));throw e;}
    finally {api.busy=false;}
  }
  async function prepare(internal=false) {
    if(api.busy&&!internal) throw Error('Дождитесь окончания текущей операции');
    await stable();const page=read();
    const missing=page.documents.filter(d=>known[d.key]!==d.fingerprint);
    const selected=new Set(missing.map(d=>d.key));
    rows().forEach((a,i)=>{const input=a.querySelector('[data-tid="documentCheckbox"] input');if(!input) throw Error('Нет флажка документа');const wanted=selected.has(page.documents[i].key);if(input.checked!==wanted) input.click();});
    api.batch={schema:1,run,batchId:run+'-'+page.folder+'-'+page.page,requestedAt:new Date().toISOString(),page,documents:missing,state:'prepared'};
    save('dd-batch-'+api.batch.batchId+'.json',api.batch);
    say('Страница '+page.page+': '+missing.length+' к загрузке, '+(page.documents.length-missing.length)+' уже сохранено.');
    return api.batch;
  }
  async function download() {
    if(!api.batch?.documents.length) throw Error('Сначала подготовьте страницу с недостающими документами');
    const current=read();
    if(current.sourceUrl!==api.batch.page.sourceUrl) throw Error('Страница изменилась после подготовки');
    const b=[...document.querySelectorAll('button')].find(e=>txt(e)==='Скачать');if(!b) throw Error('Нет меню Скачать');b.click();
    await delay(300);
    const item=[...document.querySelectorAll('button,[role="button"]')].find(e=>txt(e).startsWith('Документооборот целиком'));
    if(!item) throw Error('Не найден пункт Документооборот целиком');
    item.click(); api.batch.state='requested';say('Запрошен комплект '+api.batch.documents.length+' документов. Требуется проверка файлов на диске.');
  }
  function closeSuccess() {
    for(const b of document.querySelectorAll('button,[role="button"]')) {
      if(txt(b)!=='Закрыть') continue;
      for(let e=b.parentElement,n=0;e&&n<5;e=e.parentElement,n++) {
        if(txt(e).includes('Архив скачан')&&txt(e).length<1000) {b.click();break;}
      }
    }
  }
  async function archiveFolder(maxPages=100) {
    if(api.busy) return;api.busy=true;api.stopped=false;
    const initialFolder=folder();api.pages=[];const seen=new Set();let requested=0;
    try {
      await stable();
      // A resumed download pass is explicitly marked partial until a complete metadata scan exists locally.
      const startPage=read().page;
      for(let i=0;i<maxPages;i++) {
        if(api.stopped||folder()!==initialFolder) throw Error('Остановка или смена раздела');
        closeSuccess();
        const page=read(),s=signature();if(seen.has(s))throw Error('Повтор страницы');seen.add(s);api.pages.push(page);
        if(!page.emptyConfirmed) {
          const batch=await prepare(true);
          if(batch.documents.length) {
            if(batch.documents.length>50)throw Error('В партии больше 50 документов');
            await download();
            const deadline=Date.now()+10*60*1000;let done=false;
            while(Date.now()<deadline) {
              if(api.stopped)throw Error('Остановлено пользователем');
              const body=txt(document.body);
              if(body.includes('за один раз можно скачать не больше'))throw Error('Сайт отклонил размер партии');
              if(body.includes('Архив скачан')) {done=true;break;}
              const progress=body.match(/Подготовка архива с документами\s*([\d\s]+)%/);
              say(initialFolder+', страница '+page.page+': подготовка '+batch.documents.length+' документов'+(progress?' — '+progress[1].trim()+'%':''));
              await delay(2000);
            }
            if(!done)throw Error('Истекло время подготовки архива');
            requested+=batch.documents.length;
            batch.state='native-download-started';batch.downloadStartedAt=new Date().toISOString();
            save('dd-receipt-'+batch.batchId+'.json',batch);
            // Give Safari time to create the native download before the next manifest is emitted.
            await delay(10000);closeSuccess();
          }
        }
        say(initialFolder+': обработана страница '+page.page+', запрошено файловых комплектов '+requested);
        if(page.emptyConfirmed||!(await next())) {
          save('dd-download-pass-'+initialFolder+'-'+run+'.json',{schema:1,run,folder:initialFolder,complete:startPage===1,nativeDownloadsFinished:true,pages:api.pages,requested});
          say(initialFolder+': загрузки раздела запрошены полностью. Комплектов: '+requested+'. Проверить файлы на диске.');return;
        }
      }
      throw Error('Достигнут предел страниц одного прохода');
    }catch(e){save('dd-download-interrupted-'+run+'.json',{schema:1,run,folder:initialFolder,complete:false,error:String(e),pages:api.pages,requested});say(String(e));throw e;}
    finally{api.busy=false;}
  }
  Object.assign(api,{read,scan,next,prepare,download,archiveFolder,key,fingerprint,save,stop:()=>{api.stopped=true;say('Остановка запрошена');}});
  window.DDArchive=api;
  for(const [title,fn] of [['Считать весь реестр раздела',scan],['Собрать архив раздела',()=>archiveFolder()],['Подготовить текущую страницу',()=>prepare()],['Скачать выбранные комплекты',download],['Следующая страница',next],['Стоп',api.stop]]) {
    const b=document.createElement('button');b.textContent=title;b.style.cssText='display:block;margin:7px 0;width:100%;padding:7px;cursor:pointer';b.onclick=()=>Promise.resolve().then(fn).catch(e=>say(String(e)));panel.append(b);
  }
  document.body.append(panel);say('Готово. Известных сохранённых документов: '+Object.keys(known).length);
})();
