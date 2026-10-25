let csrfToken = '';
export function setCsrf(token:string) { csrfToken = token; }
export class ApiError extends Error { status:number; constructor(message:string,status:number) { super(message);this.status=status; } }
export async function api<T>(path:string, options:RequestInit = {}):Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type','application/json');
  if (options.method && options.method !== 'GET' && csrfToken) headers.set('X-CSRF-Token',csrfToken);
  let response:Response;
  try { response = await fetch(`/api${path}`,{...options,headers,credentials:'include'}); }
  catch { throw new ApiError('Не удалось соединиться с сервером. Проверьте подключение и повторите попытку — введённые данные сохранены на экране.',0); }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const detail = payload.detail;
    const message = typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map((entry:{msg?:string;loc?:string[]}) => `${entry.loc?.filter(x=>x!=='body').join('.') || 'Данные'}: ${entry.msg || 'проверьте значение'}`).join('; ') : 'Не удалось выполнить запрос. Повторите попытку.';
    throw new ApiError(message,response.status);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}
export function actionKey(key:string) { const storageKey=`credit-idempotency:${key}`;let value=sessionStorage.getItem(storageKey);if(!value){value=crypto.randomUUID();sessionStorage.setItem(storageKey,value);}return value; }
export function clearKey(key:string) { sessionStorage.removeItem(`credit-idempotency:${key}`); }
export const json = (value:unknown) => JSON.stringify(value);
export const money = (value:string|number|null|undefined, decimals=0) => value == null ? 'Не указано' : new Intl.NumberFormat('ru-RU',{style:'currency',currency:'RUB',maximumFractionDigits:decimals}).format(Number(value));
export const date = (value:string) => new Intl.DateTimeFormat('ru-RU',{day:'numeric',month:'short',year:'numeric'}).format(new Date(value));
export const dateTime = (value:string) => new Intl.DateTimeFormat('ru-RU',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'}).format(new Date(value));
export const percent = (value:string|number) => `${new Intl.NumberFormat('ru-RU',{maximumFractionDigits:2}).format(Number(value))}%`;
export const statuses:Record<string,{label:string;tone:string;description:string}> = {
 draft:{label:'Черновик',tone:'neutral',description:'Продолжите заполнение и отправьте заявку на рассмотрение.'},
 submitted:{label:'Отправлена',tone:'blue',description:'Заявка поступила в банк.'},checking:{label:'Проверка данных',tone:'blue',description:'Банк проверяет сведения о компании.'},scoring:{label:'Оценка заявки',tone:'blue',description:'Банк оценивает параметры финансирования.'},
 needs_info:{label:'Нужны сведения',tone:'amber',description:'Дополните информацию, чтобы банк продолжил рассмотрение.'},manual_review:{label:'На рассмотрении',tone:'blue',description:'Специалист банка рассматривает вашу заявку.'},
 approved:{label:'Одобрена',tone:'green',description:'Банк одобрил заявку. Ознакомьтесь с условиями и подтвердите их.'},rejected:{label:'Отказ',tone:'red',description:'Рассмотрение заявки завершено. Причина указана в решении банка.'},alternative_offered:{label:'Новое предложение',tone:'amber',description:'Банк предложил другие условия. Вы можете принять или отклонить их.'},accepted:{label:'Условия приняты',tone:'green',description:'Условия подтверждены. Осталось подписать демонстрационный договор.'},signed:{label:'Подписана',tone:'green',description:'Договор подписан. Можно выполнить демонстрационную выдачу.'},disbursed:{label:'Кредит выдан',tone:'green',description:'Демонстрационная выдача завершена. Реальные деньги не переводились.'},declined:{label:'Отклонена клиентом',tone:'neutral',description:'Вы отказались от предложенных условий.'},cancelled:{label:'Отменена',tone:'neutral',description:'Заявка отменена.'},check_failed:{label:'Проверка прервана',tone:'amber',description:'Внешний сервис временно недоступен. Повторите проверку.'}
};
export const statusName = (status:string) => statuses[status]?.label || status;
