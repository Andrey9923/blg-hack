const {JSDOM, VirtualConsole} = require('jsdom');
const {spawn} = require('node:child_process');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const readline = require('node:readline');
const root = require('node:path').resolve(__dirname, '..');
(async () => {
  const server = spawn('python3', ['-u', '-c', `
import tempfile
from pathlib import Path
from web.app import OperatorServer, OperatorService
from web.persistence import Database, SQLiteRunStore
with tempfile.TemporaryDirectory() as directory:
 db = Database(Path(directory)/'test.sqlite3')
 db.set_user('ui-user', 'ui-test-password-123')
 server = OperatorServer(('127.0.0.1',0), OperatorService(SQLiteRunStore(db)), auth_database=db)
 print(server.server_address[1], flush=True)
 server.serve_forever()
`], {cwd:root, stdio:['ignore','pipe','inherit']});
  try {
    const port = await new Promise(resolve => readline.createInterface({input:server.stdout}).once('line',resolve));
    const base = `http://127.0.0.1:${port}`;
    const login = await fetch(base+'/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:'ui-user',password:'ui-test-password-123'})}).then(r=>r.json());
    const html = await fetch(base).then(r=>r.text());
    const errors=[];
    const vc=new VirtualConsole(); vc.on('jsdomError',e=>errors.push(e.message));
    const dom = new JSDOM(html, {url:base, runScripts:'dangerously', pretendToBeVisual:true, virtualConsole:vc, beforeParse(w) {
      w.sessionStorage.setItem('cosmo-token',login.token);
      w.fetch=(url,opts)=>fetch(new URL(url,base),opts);
      w.matchMedia=()=>({matches:false});
      w.HTMLElement.prototype.scrollIntoView=()=>{};
    }});
    const w=dom.window, d=w.document;
    async function waitFor(fn, label) { for(let i=0;i<300;i++){if(fn()) return; await new Promise(r=>setTimeout(r,30));} throw new Error('Timeout: '+label+'; status='+d.getElementById('status').textContent); }
    await waitFor(()=>d.querySelectorAll('#scenario option').length===4,'init');
    d.getElementById('createBtn').click();
    await waitFor(()=>d.getElementById('stepNow').textContent==='0','create');
    const parent=d.getElementById('runIdLabel').textContent;
    d.getElementById('advanceOne').click();
    await waitFor(()=>d.getElementById('stepNow').textContent==='1','advance');
    assert.equal(d.querySelectorAll('#timeline tbody tr').length,16);
    assert.equal(d.querySelectorAll('.timeline-cell').length,16);
    d.querySelector('.timeline-cell').click();
    await waitFor(()=>d.querySelector('#explanation .pill'),'timeline explain');
    d.getElementById('chartSat').value='S01'; d.getElementById('chartSat').dispatchEvent(new w.Event('change'));
    assert.ok(d.querySelector('#telemetryChart svg'));
    d.getElementById('compareBtn').click();
    await waitFor(()=>d.querySelectorAll('.branch-adopt').length===2,'compare');
    d.querySelectorAll('.branch-adopt')[1].click();
    await waitFor(()=>d.getElementById('status').textContent.includes('Выбранная ветвь сохранена'),'adopt');
    assert.equal(d.getElementById('runIdLabel').textContent,parent);
    d.getElementById('openRun').click();
    await waitFor(()=>d.getElementById('status').textContent.includes('Открыта ветвь'),'reopen');
    const scenario=JSON.parse(fs.readFileSync(require('node:path').join(root, 'data/P02_shift.json')));
    scenario.meta.id='ui-upload'; scenario.meta.title='Uploaded scenario';
    Object.defineProperty(d.getElementById('scenarioFile'),'files',{value:[{name:'custom.json',size:JSON.stringify(scenario).length,text:async()=>JSON.stringify(scenario)}],configurable:true});
    d.getElementById('scenarioFile').dispatchEvent(new w.Event('change'));
    await waitFor(()=>d.getElementById('fileInfo').textContent.includes('custom.json'),'upload');
    d.getElementById('createBtn').click();
    await waitFor(()=>d.getElementById('stepNow').textContent==='0' && d.getElementById('savedRuns').value===d.getElementById('runIdLabel').textContent,'custom create');
    assert.ok(d.getElementById('jobsPage').textContent.includes('1208'));
    const first=d.querySelector('#jobs tbody tr td').textContent;
    d.getElementById('jobsNext').click();
    assert.notEqual(d.querySelector('#jobs tbody tr td').textContent,first);
    d.getElementById('timelineNext').click();
    assert.equal(d.getElementById('timelineStart').value,'48');
    assert.equal(d.querySelectorAll('#timeline tbody tr').length,48);
    assert.deepEqual(errors,[]);
    console.log('DOM integration passed: authenticated create, advance, timeline/explain, satellite chart, compare/adopt, reopen, JSON upload, jobs pagination and timeline paging.');
    dom.window.close();
  } finally { server.kill('SIGTERM'); }
})().catch(e=>{console.error(e);process.exitCode=1;});
