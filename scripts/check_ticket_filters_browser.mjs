import { createServer } from 'node:http';
import { readFileSync, mkdtempSync, rmSync } from 'node:fs';
import { once } from 'node:events';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn, execFileSync } from 'node:child_process';
import assert from 'node:assert/strict';

const sleep = ms => new Promise(r => setTimeout(r, ms));
const requests = [];
const uploads = [];
// Run with: CHROME_BIN=/path/to/chrome node scripts/check_ticket_filters_browser.mjs
const chromeBin = process.env.CHROME_BIN;
if (!chromeBin) throw new Error('Set CHROME_BIN to a Chromium executable.');
const root = fileURLToPath(new URL('../', import.meta.url));
const opsRows = execFileSync(root + '.venv/bin/python', ['-c', `
from datetime import datetime, timezone
from types import SimpleNamespace
from jinja2 import Environment, FileSystemLoader, select_autoescape
from app.i18n import get_translator
env = Environment(loader=FileSystemLoader('app/templates'), autoescape=select_autoescape())
ticket = SimpleNamespace(id='one', reference='T-000059', title='Título longo ' * 30, urgent=True, status='waiting_on_user', updated_at=datetime.now(timezone.utc))
row = dict(ticket=ticket, creator=SimpleNamespace(display_name='Maria'), assignee=None, updated_for_user=True, pending_draft=True, route_target_display=SimpleNamespace(id='ti',label='TI'))
print(env.get_template('ops_ticket_rows.html').render(rows=[row], list_previews={'one': {'message': {'author_name':'Maria','author_type':'operator','visibility':'internal','excerpt':'<script>alert(1)</script> ' + 'Mensagem longa ' * 15}, 'summary':'Resumo interno ' * 40}}, t=get_translator('pt-BR'), ops_status_label=lambda _: 'Aguardando usuário', ops_author_label=lambda _: 'IA', unknown_label='Desconhecido', unassigned_label='Não atribuído', format_datetime_iso=lambda value:value.isoformat(), format_datetime_utc=lambda _: '09/09/2026 14:32'))
`], {cwd:root, encoding:'utf8'});
const requesterPages = JSON.parse(execFileSync(root + '.venv/bin/python', ['-c', `
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from jinja2 import Environment, FileSystemLoader, select_autoescape
from app.i18n import get_translator
env = Environment(loader=FileSystemLoader('app/templates'), autoescape=select_autoescape())
now = datetime.now(timezone.utc)
ticket = SimpleNamespace(reference='T-000059', title='Título longo ' * 12, urgent=True, status='waiting_on_user')
timeline = [dict(kind='message', id='one', author_type='requester', source='web', author_label='Maria', created_at=now, body_html='<p>Mensagem pública</p><pre>' + 'long_code_' * 80 + '</pre>', attachments=[]), dict(kind='message', id='two', author_type='ai', source='ai', author_label='IA', created_at=now, body_html='<p>Resposta pública</p>', attachments=[]), dict(kind='status_change', lane_label='Status', created_at=now, summary='Aguardando sua resposta')]
pages = {}
for preview in (False, True):
    pages['preview' if preview else 'requester'] = env.get_template('requester_ticket_detail.html').render(ticket=ticket, timeline=timeline, requester_actions_enabled=not preview, requester_live_updates_enabled=False, interaction_mode='admin_preview_readonly' if preview else 'requester', reply_body='', t=get_translator('pt-BR'), ui_locale='pt-BR', ui_locale_links={'en':'#','pt-BR':'#'}, csrf_token='test-csrf', requester_status_label=lambda _: 'Aguardando sua resposta', format_datetime_iso=lambda value:value.isoformat(), format_datetime_utc=lambda _: '09/09/2026 14:32')
for ops in (False, True):
    pages['new-ops' if ops else 'new-requester'] = env.get_template('requester_ticket_new.html').render(is_ops_user=ops, ticket_list_path='/ops' if ops else '/app/tickets', ticket_create_action='/ops/tickets/new' if ops else '/app/tickets/new', t=get_translator('pt-BR'), ui_locale='pt-BR', ui_locale_links={'en':'#','pt-BR':'#'}, csrf_token='test-csrf')
print(json.dumps(pages))
`], {cwd:root, encoding:'utf8'}));
function fragment(q = '', selected = true) {
 return `<div id="results"><form data-ticket-filters hx-get="/tickets" hx-target="#results" hx-swap="outerHTML" hx-trigger="submit, ticket-filter-submit" hx-sync="this:replace"><input id="search" type="search" name="q" hx-preserve value="${q}"><input type="checkbox" name="state" value="waiting_reply" ${selected ? 'checked' : ''}><select name="status"><option value="">Any</option><option value="resolved">Resolved</option></select><button type="submit">Apply</button></form><p class="ticket-filter-feedback" hidden><span data-filter-error hidden>Error</span></p><div data-ticket-filter-results style="min-height:200px"><div class="ticket-filter-overlay" data-filter-loading role="status" hidden><span class="ticket-filter-spinner"></span><span class="visually-hidden">Loading</span></div><div id="answer">${q}</div></div></div>`;
}
const server = createServer((req, res) => {
 const url = new URL(req.url, 'http://localhost');
 if(req.method === 'POST' && ['/ops/tickets/new','/app/tickets/new'].includes(url.pathname)) {
  const chunks=[];
  req.on('data',chunk=>chunks.push(chunk));
  req.on('end',()=>{uploads.push({path:url.pathname,body:Buffer.concat(chunks).toString('utf8')});res.end('Created');});return;
 }
 if(url.pathname === '/new-ops' || url.pathname === '/new-requester') {
  res.end(requesterPages[url.pathname.slice(1)]);return;
 }
 if(url.pathname === '/requester-layout' || url.pathname === '/preview-layout') {
  res.end(requesterPages[url.pathname === '/preview-layout' ? 'preview' : 'requester']);return;
 }
 if(url.pathname === '/ops-layout') {
  res.end('<html><head><link rel="stylesheet" href="/static/app.css"><link rel="stylesheet" href="/static/board.css"><link rel="stylesheet" href="/static/ticket-list.css"><script src="/static/ops-list.js" defer></script></head><body><main class="page page--ops-index">'+opsRows+'</main></body></html>');return;
 }
 if(url.pathname.startsWith('/static/')) {
  res.setHeader('Content-Type',url.pathname.endsWith('.css') ? 'text/css' : 'text/javascript');
  res.end(readFileSync(root + '/app' + url.pathname)); return;
 }
 if(req.headers['hx-request']) {
  requests.push({q:url.searchParams.get('q') || '', state:url.searchParams.get('state'), auto:req.headers['x-ticket-auto-filter']});
  const q=url.searchParams.get('q') || '';
  setTimeout(() => {
   if(q==='error') {res.statusCode=500;res.end('failed');return;}
   res.setHeader('HX-Replace-Url','/tickets?q='+q);
   res.end(fragment(q));
  },q==='slow' ? 700 : 30);
  return;
 }
 res.end(`<html><head><link rel="stylesheet" href="/static/app.css"><script src="/static/htmx.min.js"></script><script src="/static/ticket-filters.js" defer></script></head><body>${fragment()}</body></html>`);
});
await new Promise(r=>server.listen(0,'127.0.0.1',r));
const profile=mkdtempSync(join(tmpdir(),'autosac-filter-browser-'));
const chrome=spawn(chromeBin,['--headless','--no-sandbox','--disable-gpu','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],{stdio:'ignore'});
let ws;
try {
 let port;
 for(let i=0;i<100;i++) {try {port=readFileSync(join(profile,'DevToolsActivePort'),'utf8').split('\n')[0];break;} catch {await sleep(50);}}
 const target=await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`,{method:'PUT'})).json();
 ws=new WebSocket(target.webSocketDebuggerUrl);
 await new Promise(r=>ws.onopen=r);
 let id=0; const pending=new Map();
 ws.onmessage=e=>{const m=JSON.parse(e.data);if(pending.has(m.id)){pending.get(m.id)(m);pending.delete(m.id);}};
 const send=(method,params={})=>new Promise(r=>{pending.set(++id,r);ws.send(JSON.stringify({id,method,params}));});
 const evaluate=async expression=>{const m=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(m.result.exceptionDetails)throw Error(JSON.stringify(m.result.exceptionDetails));return m.result.result.value;};
 await send('Page.navigate',{url:`http://127.0.0.1:${server.address().port}/tickets`});
 await sleep(350);
 assert.equal(await evaluate(`document.querySelector('form').classList.contains('ticket-filters--automatic')`),true);
 const type=async q=>evaluate(`document.querySelector('#search').focus();document.querySelector('#search').value=${JSON.stringify(q)};document.querySelector('#search').dispatchEvent(new Event('input',{bubbles:true}));`);
 await evaluate(`window.savedSearch=document.querySelector('#search'); window.initialHistory=history.length;`);
 await type('a');await sleep(80);await type('ab');await sleep(80);await type('abc');await sleep(450);
 assert.deepEqual(requests.map(x=>x.q),['abc']);
 assert.equal(await evaluate(`document.querySelector('#search')===window.savedSearch && document.activeElement===window.savedSearch`),true);
 assert.equal(await evaluate(`history.length===window.initialHistory`),true);
 assert.equal(requests[0].auto,'true');
 const beforeLoading=await evaluate(`document.querySelector('[data-ticket-filter-results]').getBoundingClientRect().top`);
 await type('slow');await sleep(360);
 assert.equal(await evaluate(`document.querySelector('[data-ticket-filter-results]').getBoundingClientRect().top`),beforeLoading);
 assert.equal(await evaluate(`getComputedStyle(document.querySelector('button[type=submit]')).display !== 'none'`),true);
 assert.equal(await evaluate(`!document.querySelector('[data-filter-loading]').hidden && getComputedStyle(document.querySelector('[data-filter-loading]')).position === 'absolute' && document.querySelector('.ticket-filter-feedback').hidden`),true);
 await type('latest');await sleep(800);
 assert.equal(await evaluate(`document.querySelector('#answer').textContent`),'latest');
 assert.equal(await evaluate(`location.search`),'?q=latest');
 assert.equal(await evaluate(`document.querySelector('[data-filter-loading]').hidden`),true);
 await evaluate(`const c=document.querySelector('[name=state]'); c.checked=false;c.dispatchEvent(new Event('change',{bubbles:true}));`);
 await sleep(120);
 assert.equal(requests.at(-1).state,null);
 assert.equal(await evaluate(`document.querySelector('[name=state]').checked`),true);
 await evaluate(`document.querySelector('[name=status]').value='resolved';document.querySelector('[name=state]').dispatchEvent(new Event('change',{bubbles:true}));`);
 await sleep(120);
 assert.equal(await evaluate(`document.querySelector('[name=status]').value`),'');
 await type('error');await sleep(450);
 assert.equal(await evaluate(`document.querySelector('form').classList.contains('ticket-filters--error') && !document.querySelector('[data-filter-error]').hidden`),true);
 await type('retry');await sleep(450);
 assert.equal(await evaluate(`document.querySelector('#answer').textContent`),'retry');
 // Measure the real page classes against the production styles at both breakpoints.
 const pageClasses = ['ops_board.html', 'ops_ticket_list.html'].map(name =>
  readFileSync(root + '/app/templates/' + name, 'utf8').match(/block page_class %}([^]*?){% endblock/)[1].trim());
 const header = readFileSync(root + '/app/templates/ops_workspace_header.html', 'utf8')
  .replace('{{ t("eyebrow.dev_ti_view") }}', 'Visão Dev/TI')
  .replace('{{ t("button.open_ticket") }}', 'Abrir ticket');
 await evaluate(`Promise.all(['board.css','ticket-list.css'].map(name => new Promise(resolve => {
  const link=document.createElement('link');link.rel='stylesheet';link.href='/static/'+name;link.onload=resolve;document.head.append(link);
 })))`);
 for (const width of [390, 800, 1440, 1920]) {
  await send('Emulation.setDeviceMetricsOverride', {width, height:900, deviceScaleFactor:1, mobile:false});
  const boxes = await evaluate(`(() => {
   return ${JSON.stringify(pageClasses)}.map((classes, index) => {
    const page=document.createElement('main');page.className='page '+classes;
    const workspace=document.createElement('div');workspace.className='workspace-view '+(index===0?'board-workspace':'ticket-index');
    workspace.innerHTML=${JSON.stringify(header)}.replace('{{ t(heading_key) }}',index===0?'Quadro operacional':'Fila de tickets')
     +(index===0?'<p class="visually-hidden" role="status"></p><div class="board-feedback" hidden></div>':'')
     +'<div id="ops-workspace-results"><section class="ops-toolbar">Filters</section></div>';
    page.append(workspace);document.body.append(page);
    const box=page.querySelector('.ops-toolbar').getBoundingClientRect();
    const headerBox=page.querySelector('header').getBoundingClientRect();
    const top=page.getBoundingClientRect().top;
    const geometry={left:box.left,width:box.width,top:box.top-top,headerTop:headerBox.top-top,headerHeight:headerBox.height};
    page.remove();return geometry;
   });
  })()`);
  assert.deepEqual(boxes[0], boxes[1], 'Ops toolbar geometry must match at '+width+'px');
 }
 await send('Page.navigate',{url:`http://127.0.0.1:${server.address().port}/ops-layout`});
 await sleep(350);
 assert.equal(await evaluate(`document.querySelector('.ops-list-summary').hidden`),true);
 assert.equal(await evaluate(`document.querySelector('.ops-list-message script') === null && document.querySelector('.ops-list-message').textContent.includes('<script>')`),true);
 await evaluate(`document.querySelector('[data-ops-summary-toggle]').click()`);
 assert.equal(await evaluate(`!document.querySelector('.ops-list-summary').hidden`),true);
 await evaluate(`const results=document.querySelector('#ops-results');results.outerHTML=results.outerHTML;document.dispatchEvent(new Event('htmx:load'));`);
 assert.equal(await evaluate(`document.querySelector('[data-ops-summary-toggle]').checked && !document.querySelector('.ops-list-summary').hidden`),true);
 await send('Page.reload');await sleep(350);
 assert.equal(await evaluate(`document.querySelector('[data-ops-summary-toggle]').checked && !document.querySelector('.ops-list-summary').hidden`),true);
 for (const width of [390, 800, 1440, 1920]) {
  await send('Emulation.setDeviceMetricsOverride', {width,height:900,deviceScaleFactor:1,mobile:false});
  assert.equal(await evaluate(`(() => {
   const row=document.querySelector('.ticket-index-row--ops');
   const title=row.querySelector('strong');
   const primary=row.querySelector('.ticket-index-row__primary').getBoundingClientRect();
   const state=row.querySelector('.ticket-index-row__state').getBoundingClientRect();
   return primary.bottom <= state.top && getComputedStyle(title).whiteSpace==='nowrap' && title.clientHeight < 30 && document.documentElement.scrollWidth <= innerWidth;
  })()`),true,'Ops row must retain one-line full-width title without overflow at '+width+'px');
  assert.equal(await evaluate(`(() => {
   const box=document.querySelector('#ops-results').getBoundingClientRect();
   return box.width <= 1200 && Math.abs(box.left - (innerWidth-box.right)) < 1;
  })()`),true,'Ops results must be centered and capped at 1200px at '+width+'px');
  if (width >= 800) {
   assert.equal(await evaluate(`(() => {
    const row=document.querySelector('.ticket-index-row--ops');
    const state=row.querySelector('.ticket-index-row__state').getBoundingClientRect();
    const people=row.querySelector('.ticket-index-row__people').getBoundingClientRect();
    const message=row.querySelector('.ops-list-message').getBoundingClientRect();
    const rem=parseFloat(getComputedStyle(document.documentElement).fontSize);
    return state.width <= 10*rem && people.width <= 12*rem && message.width > Math.max(state.width,people.width);
   })()`),true,'Last message must receive more width than either metadata column at '+width+'px');
  }
 }
 for (const view of ['requester', 'preview']) {
  await send('Page.navigate',{url:`http://127.0.0.1:${server.address().port}/${view}-layout`});
  await sleep(300);
  const readonly=view==='preview';
  assert.equal(await evaluate(`document.querySelector('textarea').disabled`),readonly);
  assert.equal(await evaluate(`document.querySelector('.requester-preview-banner') !== null`),readonly);
  assert.equal(await evaluate(`document.querySelector('#ticket-composer-region form').hasAttribute('action')`),!readonly);
  assert.equal(await evaluate(`document.querySelector('#ticket-composer-region [name=csrf_token]') !== null`),!readonly);
  for (const width of [390, 800, 1440]) {
   await send('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:false});
   assert.equal(await evaluate(`(() => {
    const header=document.querySelector('#ticket-status-region').getBoundingClientRect();
    const ledger=document.querySelector('#ticket-ledger-region').getBoundingClientRect();
    const composer=document.querySelector('#ticket-composer-region').getBoundingClientRect();
    const message=getComputedStyle(document.querySelector('.message-card--requester'));
    const status=getComputedStyle(document.querySelector('.timeline-status__text'));
    const statusBox=document.querySelector('.timeline-status').getBoundingClientRect();
    return Math.abs(header.width-ledger.width)<1 && Math.abs(composer.width-ledger.width)<1 && ledger.width<=800
     && message.fontSize==='14px' && message.justifySelf==='end' && status.fontSize==='11px'
     && status.textAlign==='center' && statusBox.width<=ledger.width
     && Math.abs((statusBox.left+statusBox.right)-(ledger.left+ledger.right))<2
     && (innerWidth<=560 || statusBox.width<ledger.width)
     && document.documentElement.scrollWidth<=innerWidth;
   })()`),true,'Compact aligned '+view+' conversation without overflow at '+width+'px');
  }
 }
 for (const view of ['new-requester','new-ops']) {
  await send('Page.navigate',{url:`http://127.0.0.1:${server.address().port}/${view}`});await sleep(250);
  assert.equal(await evaluate(`!document.querySelector('[data-attachment-selection]').hidden`),true);
  await evaluate(`(() => {const data=new DataTransfer();data.items.add(new File(['reset'],'reset.txt'));const input=document.querySelector('[name=attachments]');input.files=data.files;input.dispatchEvent(new Event('change',{bubbles:true}));document.querySelector('[data-ticket-attachments]').reset();})()`);
  await sleep(30);
  assert.equal(await evaluate(`document.querySelector('[name=attachments]').files.length===0 && document.querySelector('[data-attachment-list]').children.length===0`),true);
  await evaluate(`(() => {
   const input=document.querySelector('[name=attachments]');
   const picker=new DataTransfer();picker.items.add(new File(['picker-content'],'picked.txt',{type:'text/plain'}));
   input.files=picker.files;input.dispatchEvent(new Event('change',{bubbles:true}));
   const drop=new DataTransfer();drop.items.add(new File(['dropped-content'],'<img src=x>.txt',{type:'text/plain'}));
   document.querySelector('[name=description]').dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:drop}));
   const paste=new DataTransfer();paste.items.add(new File(['pasted-content'],'image.png',{type:'image/png'}));
   document.querySelector('[name=description]').dispatchEvent(new ClipboardEvent('paste',{bubbles:true,cancelable:true,clipboardData:paste}));
  })()`);
  assert.deepEqual(await evaluate(`Array.from(new FormData(document.querySelector('[data-ticket-attachments]')).getAll('attachments'),file=>file.name)`),['picked.txt','<img src=x>.txt','image.png']);
  assert.equal(await evaluate(`document.querySelector('[data-attachment-list] img')===null`),true);
  assert.equal(await evaluate(`(() => {
   const data=new DataTransfer();data.setData('text/plain','normal text');
   const event=new ClipboardEvent('paste',{bubbles:true,cancelable:true,clipboardData:data});
   document.querySelector('[name=description]').dispatchEvent(event);return !event.defaultPrevented;
  })()`),true);
  await evaluate(`document.querySelector('[data-attachment-list] button').click()`);
  assert.deepEqual(await evaluate(`Array.from(document.querySelector('[name=attachments]').files,file=>file.name)`),['<img src=x>.txt','image.png']);
  // A second picker selection appends; it must not lose dragged/pasted files.
  await evaluate(`(() => {const data=new DataTransfer();data.items.add(new File(['second-content'],'second.txt'));const input=document.querySelector('[name=attachments]');input.files=data.files;input.dispatchEvent(new Event('change',{bubbles:true}));})()`);
  assert.equal(await evaluate(`document.querySelector('[name=attachments]').files.length`),3);
  await evaluate(`document.querySelector('[name=description]').value='Test description';document.querySelector('[data-ticket-attachments]').requestSubmit()`);
  await sleep(300);
  assert.equal(uploads.at(-1).path,view==='new-ops'?'/ops/tickets/new':'/app/tickets/new');
  for (const text of ['dropped-content','pasted-content','second-content','test-csrf','Test description']) assert.ok(uploads.at(-1).body.includes(text));
 }
 await send('Page.enable');
 const unsupported = await send('Page.addScriptToEvaluateOnNewDocument',{source:'Object.defineProperty(window,"DataTransfer",{value:undefined,configurable:true});'});
 await send('Page.navigate',{url:`http://127.0.0.1:${server.address().port}/new-requester`});await sleep(250);
 assert.equal(await evaluate(`document.querySelector('[data-attachment-selection]').hidden && !document.querySelector('[name=attachments]').disabled && document.querySelector('[name=attachments]').multiple`),true,'Unsupported browsers must retain the native picker');
 await send('Page.removeScriptToEvaluateOnNewDocument',{identifier:unsupported.result.identifier});
 console.log('PASS: filters, responsive layouts, readonly preview, attachment picker/drop/paste accumulation, removal, text paste and multipart submissions');
} finally {
 if(chrome.exitCode === null && chrome.signalCode === null) {
  const exited=once(chrome,'exit');
  // Let Chromium flush storage and stop its children before removing the profile.
  if(ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({id:999999,method:'Browser.close'}));
  else chrome.kill('SIGTERM');
  const fallback=setTimeout(()=>chrome.kill('SIGTERM'),5000);
  try { await exited; } finally { clearTimeout(fallback); }
 }
 if(ws) ws.close();
 server.close();
 rmSync(profile,{recursive:true,force:true,maxRetries:5,retryDelay:100});
}
