import { test, expect, type Page } from '@playwright/test';
import path from 'node:path';
const shots=path.resolve(import.meta.dirname,'../../docs/screenshots');
const errors=new Map<Page,string[]>();
test.beforeEach(async({page})=>{
  errors.set(page,[]); page.on('pageerror',error=>errors.get(page)!.push(error.message));
  await page.route('**/*',async route=>{
    const url=new URL(route.request().url());
    if(url.origin===new URL(process.env.E2E_BASE_URL||'http://127.0.0.1:8081').origin) await route.continue();
    else {errors.get(page)!.push(`Unexpected external request: ${url.origin}`);await route.abort();}
  });
});
test.afterEach(async({page})=>{expect(errors.get(page)).toEqual([]);});
async function login(page:Page,account='good'){
  await page.goto('/');
  await page.getByLabel('Электронная почта').fill(`${account}@demo.local`);
  await page.getByLabel('Пароль',{exact:true}).fill('Demo2026!');
  await page.getByRole('button',{name:'Войти в кабинет',exact:true}).click();
  await expect(page.getByRole('navigation',{name:'Основная навигация'})).toBeVisible();
  await page.goto(account==='bank'?'/#queue':'/#dashboard');
}
async function logout(page:Page){await page.getByRole('button',{name:'Выйти из кабинета'}).click();await expect(page.getByRole('heading',{name:'Добро пожаловать'})).toBeVisible();}
async function create(page:Page,options:{amount?:string;emptyRevenue?:boolean;restart?:boolean;product?:string}={}){
  await page.getByRole('button',{name:'Новая заявка',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Выберите кредитный продукт'})).toBeVisible();
  if(options.product==='equipment')await page.locator('.wizard-product').filter({hasText:'Кредит на оборудование'}).click();
  await page.getByRole('button',{name:'Продолжить',exact:true}).click();
  await page.getByLabel('Сумма кредита, ₽',{exact:true}).fill(options.amount||'500000');
  await page.getByLabel('Срок, месяцев',{exact:true}).fill('12');
  await page.getByLabel('Цель кредитования',{exact:true}).fill(`Закупка синтетических материалов E2E ${Date.now()}`);
  await page.getByLabel('Выручка в месяц, ₽',{exact:true}).fill(options.emptyRevenue?'':'1000000');
  await page.getByLabel('Расходы в месяц, ₽',{exact:true}).fill('600000');
  await page.getByLabel('Платежи по кредитам, ₽',{exact:true}).fill('20000');
  await page.getByRole('button',{name:'Продолжить',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Добавьте документы'})).toBeVisible();
  await page.locator('input[type=file]').setInputFiles({name:'synthetic.txt',mimeType:'text/plain',buffer:Buffer.from('Синтетический финансовый документ. Не является отчётностью.')});
  await expect(page.locator('.document-row')).toHaveCount(1);
  if(options.restart){
    await page.reload();
    await expect(page.getByRole('heading',{name:'Выберите кредитный продукт'})).toBeVisible();
    await page.getByRole('button',{name:'Продолжить',exact:true}).click();
    await expect(page.getByLabel('Сумма кредита, ₽',{exact:true})).toHaveValue(Number(options.amount||'500000').toFixed(2));
    await page.getByRole('button',{name:'Продолжить',exact:true}).click();
    await expect(page.locator('.document-row')).toHaveCount(1);
  }
  await page.getByRole('button',{name:'Продолжить',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Проверьте данные заявки'})).toBeVisible();
  await page.getByRole('button',{name:'Продолжить',exact:true}).click();
  await page.getByRole('checkbox').check();
  await page.getByRole('button',{name:'Отправить заявку',exact:true}).click();
  await expect(page).toHaveURL(/#application\//);
  await expect(page.locator('.status-banner')).toBeVisible();
  return page.url();
}
async function issue(page:Page){
  await page.getByRole('button',{name:'Принять условия',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Подпишите демонстрационный договор'})).toBeVisible();
  await page.getByRole('checkbox').check();
  await page.getByRole('button',{name:'Подписать в демо-режиме'}).click();
  await page.getByRole('button',{name:'Выполнить демо-выдачу'}).click();
  await expect(page.getByRole('heading',{name:'Демонстрационная выдача завершена'})).toBeVisible();
}
test('новая заявка, восстановление черновика, документ, выдача и история',async({page})=>{
  await login(page);await create(page,{restart:true});
  await expect(page.locator('.status-banner')).toContainText('Одобрена');
  await issue(page);
  await page.getByRole('button',{name:'Документы · 1'}).click();
  const downloadEvent=page.waitForEvent('download');
  await page.getByRole('link',{name:/Скачать document-/}).click();
  const download=await downloadEvent;expect(download.suggestedFilename()).toMatch(/\.txt$/);
  await page.getByRole('button',{name:'История',exact:true}).click();
  await expect(page.locator('.timeline')).toContainText('Подписана');
  await page.screenshot({path:path.join(shots,'entrepreneur.png'),fullPage:true});
  await page.getByRole('button',{name:'Обзор',exact:true}).click();
  await expect(page.getByText('Требуют вашего участия',{exact:true})).toBeVisible();
});
test('неизменяемая кредитная история даёт отказ',async({page})=>{
  await login(page,'risk');await create(page);
  await expect(page.locator('.status-banner')).toContainText('Отказ');
  await expect(page.locator('.decision-record')).toContainText('По результатам проверки кредитной истории');
  await expect(page.getByText('Индекс риска',{exact:false})).toHaveCount(0);
});
test('неполные сведения, ответ предпринимателя и продолжение',async({page})=>{
  await login(page,'info');await create(page,{emptyRevenue:true});
  await expect(page.locator('.status-banner')).toContainText('Нужны сведения');
  await page.getByLabel('Ваш ответ',{exact:true}).fill('Дополняю данные о ежемесячной выручке');
  await page.getByLabel('Выручка в месяц, ₽',{exact:true}).fill('1000000');
  await page.getByRole('button',{name:'Отправить ответ'}).click();
  await expect(page.locator('.status-banner')).toContainText('Одобрена');
});
test('альтернативная сумма, принятие, подписание и демо-выдача',async({page})=>{
  await login(page,'offer');await create(page,{amount:'4500000'});
  await expect(page.locator('.status-banner')).toContainText('Новое предложение');
  await expect(page.locator('.offer-comparison')).toContainText('4');
  await issue(page);
});
test('сбой локального источника и успешная повторная проверка',async({page})=>{
  await login(page,'retry');await create(page);
  await expect(page.locator('.status-banner')).toContainText('Проверка прервана');
  await page.getByRole('button',{name:'Повторить проверку'}).click();
  await expect(page.locator('.status-banner')).toContainText('Одобрена');
});
test('банк: фильтры, проверки, заметка, уточнение и окончательное решение',async({page})=>{
  await login(page,'review');const url=await create(page);
  await expect(page.locator('.status-banner')).toContainText('На рассмотрении');
  await logout(page);await login(page,'bank');
  await page.getByRole('combobox',{name:'Фильтр по статусу'}).selectOption('manual_review');
  await page.getByRole('checkbox',{name:'Только требующие внимания'}).check();
  await expect(page.locator('tbody tr')).not.toHaveCount(0);
  await page.goto(url);
  await expect(page.getByRole('heading',{name:'Проверки и оценка'})).toBeVisible();
  await expect(page.locator('.recommendation')).toContainText('Нужна экспертная оценка');
  await page.getByLabel('Новая заметка',{exact:true}).fill('Только для сотрудников: синтетический контекст');
  await page.getByRole('button',{name:'Добавить заметку'}).click();
  await expect(page.locator('.note-entry')).toContainText('Только для сотрудников');
  await page.getByRole('button',{name:'Запросить сведения',exact:true}).click();
  await page.getByLabel('Какие сведения нужны от клиента').fill('Уточните структуру выручки по каналам');
  await page.getByRole('button',{name:'Отправить запрос',exact:true}).click();
  await expect(page.locator('.status-banner')).toContainText('Нужны сведения');
  await logout(page);await login(page,'review');await page.goto(url);
  await expect(page.getByText('Только для сотрудников: синтетический контекст',{exact:true})).toHaveCount(0);
  await page.getByLabel('Ваш ответ',{exact:true}).fill('Синтетическая выручка поступает от розничных продаж');
  await page.getByRole('button',{name:'Отправить ответ'}).click();
  await expect(page.locator('.status-banner')).toContainText('На рассмотрении');
  await logout(page);await login(page,'bank');await page.goto(url);
  await page.getByRole('button',{name:'Одобрить',exact:true}).click();
  await page.getByLabel('Обоснование решения').fill('Дополнительные сведения приняты; платёж соответствует потоку');
  await page.getByRole('button',{name:'Зафиксировать решение'}).click();
  await expect(page.locator('.status-banner')).toContainText('Одобрена');
  await expect(page.locator('.decision-record')).toContainText('Принято специалистом');
  await expect(page.locator('.recommendation')).toContainText('Нужна экспертная оценка');
  await page.screenshot({path:path.join(shots,'bank.png'),fullPage:true});
  await page.getByRole('button',{name:'Журнал действий',exact:true}).click();
  await expect(page.locator('tbody')).toContainText('Дополнительные сведения приняты');
});
test('калькулятор, оборудование, валидация и профиль',async({page})=>{
  await login(page);await page.getByRole('button',{name:'Калькулятор',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Кредитный калькулятор',exact:true})).toBeVisible();
  await page.locator('.product-switch').getByRole('button',{name:'Кредит на оборудование',exact:true}).click();
  await page.getByLabel('Сумма кредита',{exact:true}).fill('1000000');
  await page.getByLabel('Срок кредита',{exact:true}).fill('24');
  await expect(page.locator('.big-payment')).toContainText('₽');
  await page.getByLabel('Сумма кредита',{exact:true}).fill('1');
  await expect(page.getByRole('alert')).toContainText('Укажите сумму');
  await expect(page.getByRole('button',{name:'Перейти к заявке'})).toBeDisabled();
  await page.getByRole('button',{name:'Моя компания',exact:true}).click();
  await page.getByLabel('Сфера деятельности',{exact:true}).fill('Синтетическое производство');
  await page.getByRole('button',{name:'Сохранить изменения'}).click();
  await expect(page.getByText('Данные сохранены',{exact:true})).toBeVisible();
});
test('узкий экран, клавиатура и локальная документация API',async({page})=>{
  await page.setViewportSize({width:390,height:844});await page.goto('/');
  await expect(page.getByLabel('Электронная почта')).toBeVisible();
  await page.getByLabel('Электронная почта').focus();
  await page.keyboard.press('Tab');
  await expect(page.getByLabel('Пароль',{exact:true})).toBeFocused();
  await page.screenshot({path:path.join(shots,'login-mobile.png'),fullPage:true});
  await login(page);
  await page.getByRole('button',{name:'Открыть меню'}).click();
  await page.getByRole('button',{name:'Мои заявки',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Мои заявки'})).toBeVisible();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
  await page.screenshot({path:path.join(shots,'mobile.png'),fullPage:true});
  await page.goto('/docs');
  await expect(page.getByRole('heading',{name:/Умный кредитный кабинет/})).toContainText('Умный кредитный кабинет');
});

test('потерянный ответ создания восстанавливает точный запрос без дубля',async({page})=>{
  await login(page);const before=(await page.request.get('/api/applications')).json();
  await page.getByRole('button',{name:'Калькулятор',exact:true}).click();
  await page.getByLabel('Сумма кредита',{exact:true}).fill('730000');
  await page.getByLabel('Срок кредита',{exact:true}).fill('18');
  await expect(page.getByRole('button',{name:'Перейти к заявке'})).toBeEnabled();
  await page.getByRole('button',{name:'Перейти к заявке'}).click();
  let lost=false;
  await page.route('**/api/applications',async route=>{
    if(route.request().method()==='POST'&&!lost){lost=true;await route.fetch();await route.abort('failed');}
    else await route.continue();
  });
  await page.getByRole('button',{name:'Продолжить',exact:true}).click();
  await expect(page.getByRole('alert')).toContainText('Не удалось соединиться');
  await page.reload();
  await expect(page.getByLabel('Сумма кредита, ₽',{exact:true})).toHaveValue('730000.00');
  await expect(page.getByLabel('Срок, месяцев',{exact:true})).toHaveValue('18');
  const after=await (await page.request.get('/api/applications')).json();
  expect(after.length).toBe((await before).length+1);
});

test('запоздалое обновление компании не попадает в другую сессию',async({page})=>{
  await login(page);
  await page.getByRole('button',{name:'Моя компания',exact:true}).click();
  await expect(page.getByLabel('Сфера деятельности',{exact:true})).toBeVisible();
  let release!:()=>void;let captured!:()=>void;
  const blocked=new Promise<void>(resolve=>{release=resolve;});
  const arrived=new Promise<void>(resolve=>{captured=resolve;});
  let intercepted=false;
  await page.route('**/api/company',async route=>{
    if(route.request().method()==='GET'&&!intercepted){
      intercepted=true;const response=await route.fetch();captured();await blocked;await route.fulfill({response});
    }else await route.continue();
  });
  await page.getByRole('button',{name:'Сохранить изменения'}).click();await arrived;
  await logout(page);await login(page,'risk');
  await expect(page.getByText('ООО «Северный маршрут»',{exact:true})).toBeVisible();
  const oldResponse=page.waitForResponse(response=>response.url().endsWith('/api/company')&&response.request().method()==='GET');
  release();await (await oldResponse).finished();
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  await expect(page.getByText('ООО «Северный маршрут»',{exact:true})).toBeVisible();
  await expect(page.getByText('ООО «Пекарня Утро»',{exact:true})).toHaveCount(0);
});

test('оборудование: новая заявка и отклонение условий клиентом',async({page})=>{
  await login(page);await create(page,{product:'equipment'});
  await expect(page.getByRole('heading',{name:'Кредит на оборудование',exact:true})).toBeVisible();
  await expect(page.locator('.status-banner')).toContainText('Одобрена');
  await page.getByRole('button',{name:'Отклонить',exact:true}).click();
  await page.getByRole('button',{name:'Подтвердить',exact:true}).click();
  await expect(page.locator('.status-banner')).toContainText('Отклонена клиентом');
});
