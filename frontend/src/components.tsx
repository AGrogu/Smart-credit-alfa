import { Children, cloneElement, isValidElement, useId } from 'react';
import type { ReactNode } from 'react';
import { money, date, statusName, statuses } from './api';
import type { Application, Product } from './types';
export function Icon({name,size=20}:{name:string;size?:number}) { const paths:Record<string,ReactNode>={dashboard:<><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></>,applications:<><path d="M8 3h8l4 4v13H4V3h4Z"/><path d="M15 3v5h5M8 12h8M8 16h5"/></>,calculator:<><rect x="5" y="2" width="14" height="20" rx="2"/><path d="M8 6h8M8 11h1M15 11h1M8 15h1M15 15h1M8 19h1M15 19h1"/></>,company:<><path d="M3 21V5h12v16M15 10h6v11M1 21h22M7 9h1M11 9h1M7 13h1M11 13h1M7 17h1M11 17h1"/></>,bell:<><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M9 21h6"/></>,arrow:<path d="m9 5 7 7-7 7"/>,back:<path d="m15 5-7 7 7 7"/>,plus:<path d="M12 5v14M5 12h14"/>,logout:<><path d="M9 3H3v18h6M9 12h12m-4-4 4 4-4 4"/></>,check:<path d="m5 12 4 4L19 6"/>,clock:<><circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/></>,shield:<><path d="m12 2 8 4v6c0 5-8 10-8 10S4 17 4 12V6l8-4Z"/><path d="m8 11 3 3 5-5"/></>,download:<><path d="M12 3v12m-5-5 5 5 5-5M4 17v4h16v-4"/></>,upload:<><path d="M12 16V4m-5 5 5-5 5 5M4 17v4h16v-4"/></>,search:<><circle cx="10" cy="10" r="6"/><path d="m15 15 6 6"/></>,wallet:<><rect x="3" y="5" width="18" height="15" rx="3"/><path d="M3 8V4l14-2v3M21 11h-6v5h6"/></>,info:<><circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7h.01"/></>,close:<path d="m6 6 12 12M6 18 18 6"/>,refresh:<><path d="M20 7a9 9 0 1 0 1 9M20 2v6h-6"/></>,chart:<><path d="M4 3v18h17M8 16v-4M13 16V7M18 16v-7"/></>,menu:<path d="M4 6h16M4 12h16M4 18h16"/>,file:<><path d="M6 2h8l5 5v15H6Z"/><path d="M14 2v6h5M9 12h7M9 16h7"/></>,briefcase:<><rect x="3" y="7" width="18" height="14" rx="2"/><path d="M8 7V3h8v4M3 12a25 25 0 0 0 18 0M10 13h4"/></>};return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name] || paths.file}</svg>; }
export const go = (path:string) => {window.location.hash=path;window.scrollTo({top:0,behavior:'instant'});};
export function Badge({status}:{status:string}) {return <span className={`badge ${statuses[status]?.tone || 'neutral'}`}><span/>{statusName(status)}</span>;}
export function PageTitle({eyebrow,title,description,action}:{eyebrow?:string;title:string;description?:string;action?:ReactNode}) {return <div className="page-title"><div>{eyebrow&&<div className="eyebrow">{eyebrow}</div>}<h1 tabIndex={-1}>{title}</h1>{description&&<p>{description}</p>}</div>{action}</div>;}
export function ErrorBox({message}:{message:string}) {return message?<div className="message error" role="alert"><Icon name="info"/><span>{message}</span></div>:null;}
export function Notice({children,tone='neutral'}:{children:ReactNode;tone?:string}){return <div className={`message ${tone}`}><Icon name="info"/><span>{children}</span></div>;}
export function Spinner({label='Загружаем данные…'}:{label?:string}) {return <div className="loading" role="status"><span className="spinner"/>{label}</div>;}
export function Empty({title,description,action}:{title:string;description:string;action?:ReactNode}){return <div className="empty"><span className="empty-icon"><Icon name="applications" size={28}/></span><h3>{title}</h3><p>{description}</p>{action}</div>;}
type FieldChildProps = { id?: string; children?: ReactNode; 'aria-describedby'?: string };
function findFieldControlId(children:ReactNode):string|undefined {
 for(const child of Children.toArray(children)){
  if(!isValidElement<FieldChildProps>(child))continue;
  if(typeof child.type==='string'&&['input','select','textarea'].includes(child.type))return child.props.id;
  const nested=findFieldControlId(child.props.children);
  if(nested)return nested;
 }
 return undefined;
}
function connectFieldControl(children:ReactNode,id:string,hintId?:string):ReactNode {
 return Children.map(children,child=>{
  if(!isValidElement<FieldChildProps>(child))return child;
  if(typeof child.type==='string'&&['input','select','textarea'].includes(child.type)){
   const description=[child.props['aria-describedby'],hintId].filter(Boolean).join(' ')||undefined;
   return cloneElement(child,{id,'aria-describedby':description});
  }
  if(child.props.children)return cloneElement(child,{children:connectFieldControl(child.props.children,id,hintId)});
  return child;
 });
}
export function Field({label,hint,children}:{label:string;hint?:string;children:ReactNode}){
 const generatedId=useId();
 const id=findFieldControlId(children)||`field-${generatedId}`;
 const hintId=hint?`${id}-hint`:undefined;
 return <div className="field"><label className="field-label" htmlFor={id}>{label}</label>{connectFieldControl(children,id,hintId)}{hint&&<small id={hintId}>{hint}</small>}</div>;
}
export function Metric({label,value,caption,icon='wallet'}:{label:string;value:ReactNode;caption?:string;icon?:string}){return <div className="metric card"><div className="metric-top">{label}<Icon name={icon}/></div><strong>{value}</strong>{caption&&<span className="muted text-small">{caption}</span>}</div>;}
export function ApplicationTable({applications,products,bank=false}:{applications:Application[];products:Product[];bank?:boolean}){return <div className="table-wrap"><table><thead><tr><th>{bank?'Компания / заявка':'Заявка / продукт'}</th><th>Сумма</th><th>Срок</th><th>Статус</th><th>Обновлена</th><th><span className="sr-only">Открыть</span></th></tr></thead><tbody>{applications.map(app=><tr key={app.id}><td><button className="table-link" onClick={()=>go(`application/${app.id}`)}>{bank?app.company_snapshot.name:products.find(p=>p.id===app.product_id)?.name || 'Кредит для бизнеса'}</button><span className="table-sub">{app.number}{bank?` · ИНН ${app.company_snapshot.inn}`:''}</span></td><td className="money-cell">{money(app.amount)}</td><td>{app.months} мес.</td><td><Badge status={app.status}/></td><td className="muted">{date(app.updated_at)}</td><td><button className="icon-button" aria-label={`Открыть заявку ${app.number}`} onClick={()=>go(`application/${app.id}`)}><Icon name="arrow" size={16}/></button></td></tr>)}</tbody></table></div>;}
export function SectionHeading({title,description,action}:{title:string;description?:string;action?:ReactNode}){return <div className="section-heading"><div><h2>{title}</h2>{description&&<p>{description}</p>}</div>{action}</div>;}
export function ResultLine({label,value}:{label:string;value:ReactNode}){return <div className="result-line"><span>{label}</span><strong>{value}</strong></div>;}
