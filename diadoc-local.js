/* Локальное чтение DOM Диадока. Вставить целиком в консоль Safari.
 * Не делает fetch/XHR, не читает cookies, не вызывает API Диадока.
 * Снимает только текущую страницу с текущими фильтрами, НЕ весь архив.
 */
(() => {
  'use strict';
  const text = element => (element?.innerText || '').trim();

  function readPage() {
    if (location.hostname !== 'diadoc.kontur.ru' ||
        !/\/Folder\/(Inbox|Outbox|Internal)$/.test(location.pathname)) {
      throw new Error('Откройте входящие, исходящие или внутренние документы Диадока.');
    }
    const folder = location.pathname.split('/').pop();
    const documents = [...document.querySelectorAll('a[data-tid="singleDocument"]')].map(a => {
      const url = new URL(a.href);
      const field = tid => text(a.querySelector('[data-tid="' + tid + '"]'));
      const date = a.querySelector('[data-tid="Date"]');
      return {
        folder,
        boxId: location.pathname.split('/')[1],
        letterId: url.searchParams.get('letterId'),
        documentId: url.searchParams.get('documentId'),
        url: url.href,
        counterparty: field('CountragentBaseView') || text(
          a.closest('[data-tid="singleLetter"]')?.querySelector('[data-tid="CountragentBaseView"]')
        ),
        name: field('documentName'),
        status: field('PrimaryDocumentStatus'),
        dateText: field('Date'),
        dateTime: date?.getAttribute('datetime') || null,
        details: text(a.children[1]),
        rawText: text(a)
      };
    });
    return {
      capturedAt: new Date().toISOString(),
      sourceUrl: location.href,
      folder,
      scope: 'current-page-current-filters',
      navigation: text(document.querySelector('[data-tid="Navigation"]')),
      documents
    };
  }

  function savePage() {
    const result = readPage();
    if (!result.documents.length) {
      throw new Error('Нет строк: проверьте завершение загрузки и сообщение о пустом разделе. Файл не создан.');
    }
    const blob = new Blob([JSON.stringify(result, null, 2)], {type: 'application/json;charset=utf-8'});
    const objectUrl = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = objectUrl;
    anchor.download = 'diadoc-' + result.folder + '-' + Date.now() + '.json';
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 60000);
    return {count: result.documents.length, filename: anchor.download};
  }

  window.diadocReadPage = readPage;
  window.diadocSavePage = savePage;
  console.info('Установлено: diadocReadPage() — читать; diadocSavePage() — скачать JSON текущей страницы.');
})();
