/* Manus-style single-application UI: real API records, never fabricated task status. */
(() => {
  'use strict';
  let state = {tasks:[],projects:[],agents:[],files:[],automations:[],computer:{}};
  let selectedTab = 'tasks', currentTask = null, timer = null, filter = '';
  const $ = (selector) => document.querySelector(selector);
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const ago = (time) => {const secs = Math.max(0,Math.floor(Date.now()/1000-time));return secs<60?'just now':secs<3600?`${Math.floor(secs/60)}m ago`:secs<86400?`${Math.floor(secs/3600)}h ago`:`${Math.floor(secs/86400)}d ago`;};
  const token = () => sessionStorage.getItem('manus-access-token') || '';
  let toastTimer;
  const toast = msg => {const el=$('#toast');el.textContent=msg;el.classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.classList.remove('show'),3500);};
  async function api(url,method='GET',body=null,upload=false){
    const headers = token() ? {'Authorization':`Bearer ${token()}`} : {};
    if(body !== null && !upload)headers['Content-Type']='application/json';
    const response = await fetch(url,{method,headers,body:body===null?undefined:upload?body:JSON.stringify(body),cache:'no-store'});
    if(response.status===401){if(!$('#overlay').classList.contains('hidden') && $('#sheet-title').textContent==='Unlock workspace')throw Error('Incorrect access token');unlock();throw Error('Workspace needs unlocking');}
    if(!response.ok){let message=`HTTP ${response.status}`;try{message=(await response.json()).detail||message;}catch{}throw Error(message);}
    if(response.headers.get('content-type')?.includes('application/json'))return response.json();
    return response;
  }
  const toastError = error => toast(error?.message || 'Request failed');
  function sheet(title,html){clearInterval(timer);timer=null;$('#sheet-title').textContent=title;$('#sheet-body').innerHTML=html;$('#overlay').classList.remove('hidden');}
  function close(){clearInterval(timer);timer=null;currentTask=null;$('#overlay').classList.add('hidden');}
  function field(label,name,type='text',placeholder='',value=''){
    const control = type==='textarea'?`<textarea name="${name}" placeholder="${esc(placeholder)}" required>${esc(value)}</textarea>`:`<input name="${name}" type="${type}" placeholder="${esc(placeholder)}" value="${esc(value)}" required>`;
    return `<div class="field"><label>${esc(label)}</label>${control}</div>`;
  }
  function render(){
    $('#health-indicator').className='dot '+(state.computer?.ready?'ready':'offline');
    const tasks=state.tasks.filter(x=>x.prompt.toLowerCase().includes(filter.toLowerCase()));
    const pinned=[...state.tasks.filter(x=>x.pinned).map(x=>({...x,_type:'task'})),...state.agents.filter(x=>x.pinned).map(x=>({...x,_type:'agent'})),...state.projects.filter(x=>x.pinned).map(x=>({...x,_type:'project'}))];
    $('#pinned-list').innerHTML=pinned.length?pinned.map(x=>entry(x,x._type)).join(''):'<p class="empty">No pinned items. Pin a task, saved agent or project to find it here.</p>';
    $('#task-list').innerHTML=tasks.length?tasks.map(t=>entry(t,'task')).join(''):'<p class="empty">No tasks yet. Tap + to give your agent its first job.</p>';
    $('#task-count').textContent=state.tasks.length?`${state.tasks.length} total`:'';
    $('#agent-list').innerHTML=state.agents.map(a=>`<button class="agent-card" data-open="agent" data-id="${esc(a.id)}"><div class="symbol">✦</div><strong>${esc(a.name)}</strong><p>${esc(a.instructions.slice(0,105))}${a.instructions.length>105?'…':''}</p></button>`).join('');
  }
  function entry(x,type){
    const label=type==='task'?x.prompt:x.name;
    const icon=type==='task'?'✦':type==='agent'?'✧':'▱';
    const sub=type==='task'?ago(x.updated):type==='agent'?'Saved agent':'Project';
    return `<button class="entry" data-open="${type}" data-id="${esc(x.id)}"><span class="entry-icon">${icon}</span><span class="entry-main"><span class="entry-name">${esc(label)}</span><span class="entry-sub">${esc(sub)}</span></span>${type==='task'?`<span class="status ${esc(x.status)}">${esc(x.status)}</span>`:''}</button>`;
  }
  async function refresh(){state=await api('/api/bootstrap');render();}
  function tab(value){selectedTab=value;$('#tasks-screen').classList.toggle('hidden',value!=='tasks');$('#agents-screen').classList.toggle('hidden',value!=='agents');document.querySelectorAll('[data-tab]').forEach(b=>{b.classList.toggle('selected',b.dataset.tab===value&&b.closest('.segmented'));b.classList.toggle('active',b.dataset.tab===value);b.setAttribute('aria-selected',b.dataset.tab===value?'true':'false');});}
  function compose(agentId='default',projectId=''){
    sheet('New task',`<p class="helper">Describe what you want done. Planning, coding and tool selection are automatic.</p><form id="task-form">${field('What should your agent do?','prompt','textarea','Build an app, research a topic, automate a workflow…')}
    <div class="quick-prompts" role="group" aria-label="Task suggestions"><button type="button" data-template="Create or improve code in a software project. Implement, test, and report the result.">Code</button><button type="button" data-template="Research this topic carefully with sources and provide a structured report: ">Research</button><button type="button" data-template="Create a complete, polished document on this topic: ">Document</button><button type="button" data-template="Design and implement an automated workflow for: ">Automate</button></div>
    <input type="hidden" name="agent_id" value="${esc(agentId)}"><input type="hidden" name="project_id" value="${esc(projectId)}">
    <button class="button" type="submit" style="width:100%">Start task ✦</button></form>`);
    $('#sheet-body textarea')?.focus();
  }
  async function task(id){
    currentTask=id;
    try{
      const x=await api('/api/tasks/'+encodeURIComponent(id));
      const events=await api('/api/tasks/'+encodeURIComponent(id)+'/events');
      const html=`<div class="label">${esc(x.status)}</div><h3>${esc(x.prompt)}</h3><p class="meta">Created ${ago(x.created)} · Updated ${ago(x.updated)}</p>
      <div class="buttons"><button class="button secondary" data-action="pin" data-type="tasks" data-id="${esc(id)}" data-value="${x.pinned?0:1}">${x.pinned?'Unpin':'Pin'}</button>
      ${['running','queued'].includes(x.status)?`<button class="button secondary" data-action="stop" data-id="${esc(id)}">Stop</button>`:`<button class="button secondary" data-action="retry" data-id="${esc(id)}">Run again</button>`}</div>
      <h3>Activity</h3>${events.length?events.map(e=>`<div class="event">${esc(e.message)} <span class="meta">· ${ago(e.created)}</span></div>`).join(''):'<p class="helper">No activity yet</p>'}
      ${x.result?`<h3>Result</h3><div class="result">${esc(x.result)}</div>`:''}${x.error?`<h3 class="danger">Execution error</h3><div class="result danger">${esc(x.error)}</div>`:''}`;
      if($('#overlay').classList.contains('hidden'))sheet('Task activity',html);else if(currentTask===id && $('#sheet-title').textContent==='Task activity')$('#sheet-body').innerHTML=html;
      if(['running','queued'].includes(x.status)&&!timer){timer=setInterval(()=>task(id).catch(toastError),2500);}
      else if(!['running','queued'].includes(x.status)&&timer){clearInterval(timer);timer=null;}
    }catch(e){toastError(e);}
  }
  function unlock(){sheet('Unlock workspace',`<p class="helper">Your workspace is protected. Enter its access token once for this session. The server and model are configured automatically by the owner.</p><form id="unlock-form">${field('Access token','token','password','Workspace token')}<button class="button" type="submit">Unlock</button></form>`);}
  function agent(id){let a=state.agents.find(x=>x.id===id);if(!a)return;
    sheet(a.name,`<p class="helper">Saved agent instructions</p><div class="result">${esc(a.instructions)}</div><div class="buttons" style="margin-top:20px"><button class="button" data-action="use-agent" data-id="${esc(id)}">Give agent a task</button><button class="button secondary" data-action="pin" data-type="agents" data-id="${esc(id)}" data-value="${a.pinned?0:1}">${a.pinned?'Unpin':'Pin'}</button></div>`);}
  function project(id){let p=state.projects.find(x=>x.id===id);if(!p)return;
    sheet(p.name,`<p class="helper">${esc(p.description||'Saved workspace')}</p><div class="buttons"><button class="button" data-action="project-task" data-id="${esc(id)}">New task in project</button><button class="button secondary" data-action="pin" data-type="projects" data-id="${esc(id)}" data-value="${p.pinned?0:1}">${p.pinned?'Unpin':'Pin'}</button></div><h3>Project tasks</h3>${state.tasks.filter(t=>t.project_id===id).map(t=>entry(t,'task')).join('')||'<p class="helper">No project tasks yet.</p>'}`);
  }
  function view(which){
    if(which==='compose')return compose();
    if(which==='new-agent')return sheet('Create saved agent',`<p class="helper">This agent is reusable for future tasks.</p><form id="agent-form">${field('Name','name','text','Coding specialist')}${field('Instructions','instructions','textarea','Role, tools, expected output…')}<button class="button">Save agent</button></form>`);
    if(which==='projects')return sheet('Projects',`<form id="project-form">${field('New project','name','text','Project name')}<button class="button">Create project</button></form><h3>My projects</h3>${state.projects.length?state.projects.map(x=>`<div class="sheet-row"><div><strong>${esc(x.name)}</strong><small>${esc(x.description)}</small></div><button data-open="project" data-id="${esc(x.id)}">Open</button></div>`).join(''):'<p class="helper">Keep tasks and files together in a project.</p>'}`);
    if(which==='library')return sheet('Library',`<form id="file-form" enctype="multipart/form-data"><div class="field"><label>Add a file</label><input type="file" name="file" required></div><button class="button">Upload to library</button></form><h3>Saved files</h3>${state.files.length?state.files.map(x=>`<div class="sheet-row"><div><strong>${esc(x.name)}</strong><small>${(x.size/1024).toFixed(1)} KB · ${ago(x.created)}</small></div><button data-action="download" data-id="${esc(x.id)}" data-filename="${esc(x.name)}">Download</button></div>`).join(''):'<p class="helper">No uploaded files yet.</p>'}`);
    if(which==='automations')return sheet('Automations',`<p class="helper">Saved recurring tasks run on the cloud server while its scheduler is online.</p><form id="automation-form">${field('Automation name','name','text','Daily research')}${field('Task','prompt','textarea','What should happen automatically?')}
    <div class="field"><label>Repeat every</label><select name="interval_minutes"><option value="60">Hour</option><option value="1440">Day</option><option value="10080">Week</option></select></div><button class="button">Save automation</button></form><h3>Scheduled tasks</h3>${state.automations.length?state.automations.map(x=>`<div class="sheet-row"><div><strong>${esc(x.name)}</strong><small>Every ${x.interval_minutes} minutes · ${x.enabled?'Active':'Paused'}</small></div><button data-action="toggle" data-id="${esc(x.id)}" data-enabled="${x.enabled?0:1}">${x.enabled?'Pause':'Resume'}</button></div>`).join(''):'<p class="helper">No automations scheduled.</p>'}`);
    if(which==='computers')return sheet('Computers',`<div class="sheet-row"><div><strong>Cloud Computer</strong><small>${esc(state.computer?.mode||'OpenManus Docker workspace')}</small></div><span class="status ${state.computer?.ready?'completed':'failed'}">${state.computer?.ready?'Ready':'Offline'}</span></div><p class="helper">${esc(state.computer?.reason||'No cloud computer connected.')}</p><p class="helper">A real sandbox requires a Docker-capable cloud host; the app will not run commands directly on its API server as an unsafe substitute.</p>`);
    if(which==='pinned')return sheet('Pinned',state.tasks.filter(x=>x.pinned).map(t=>entry(t,'task')).join('')||'<p class="helper">Pin tasks or agents to show them here.</p>');
    if(which==='search'){sheet('Search tasks',`<div class="field"><input id="search-input" placeholder="Search task history" autofocus></div><div id="search-results"></div>`);return searchResults('');}
  }
  function searchResults(query){$('#search-results').innerHTML=state.tasks.filter(x=>x.prompt.toLowerCase().includes(query.toLowerCase())).map(x=>entry(x,'task')).join('')||'<p class="helper">No matching tasks</p>';}
  document.addEventListener('click',async ev=>{
    const target=ev.target.closest('[data-view],[data-tab],[data-open],[data-action],[data-template]');if(!target)return;
    if(target.dataset.template){const box=$('#task-form textarea');if(box){box.value=target.dataset.template;box.focus();box.setSelectionRange(box.value.length,box.value.length);}return;}
    if(target.dataset.tab){tab(target.dataset.tab);return;}
    if(target.dataset.view){view(target.dataset.view);return;}
    if(target.dataset.open){const x=target.dataset.open; if(x==='task')task(target.dataset.id);if(x==='agent')agent(target.dataset.id);if(x==='project')project(target.dataset.id);return;}
    if(!target.dataset.action)return;
    try{
      const action=target.dataset.action,id=target.dataset.id;
      if(action==='account'){return unlock();}
      if(action==='use-agent')return compose(id);
      if(action==='project-task')return compose('default',id);
      if(action==='pin')await api(`/api/pins/${target.dataset.type}/${id}`,'POST',{pinned:!!Number(target.dataset.value)});
      if(action==='stop')await api(`/api/tasks/${id}/stop`,'POST');
      if(action==='retry'){const t=await api(`/api/tasks/${id}/retry`,'POST');close();await refresh();return task(t.id);}
      if(action==='toggle')await api(`/api/automations/${id}/toggle?enabled=${Number(target.dataset.enabled)?'true':'false'}`,'POST');
      if(action==='download'){const response=await api(`/api/files/${id}`);const blob=await response.blob();const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=target.dataset.filename;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);return;}
      await refresh();if(action==='pin'&&currentTask)await task(currentTask);else if(action==='toggle')view('automations');else if(action==='stop')task(id);
    }catch(e){toastError(e);}
  });
  document.addEventListener('submit',async ev=>{
    if(!ev.target.id)return;ev.preventDefault();const form=ev.target;const v=Object.fromEntries(new FormData(form));const btn=form.querySelector('[type=submit],button');if(btn)btn.disabled=true;
    try{
      if(form.id==='task-form') {const t=await api('/api/tasks','POST',{prompt:v.prompt,agent_id:v.agent_id||null,project_id:v.project_id||null});close();await refresh();return task(t.id);}
      if(form.id==='agent-form')await api('/api/agents','POST',v);
      if(form.id==='project-form')await api('/api/projects','POST',v);
      if(form.id==='automation-form')await api('/api/automations','POST',{...v,interval_minutes:Number(v.interval_minutes)});
      if(form.id==='file-form'){await api('/api/files','POST',new FormData(form),true);}
      if(form.id==='unlock-form'){sessionStorage.setItem('manus-access-token',v.token);await refresh();close();toast('Workspace unlocked');return;}
      const area={'agent-form':'agents','project-form':'projects','automation-form':'automations','file-form':'library'}[form.id];
      await refresh();if(area==='agents'){tab('agents');close();}else if(area)view(area);toast('Saved');
    }catch(e){toastError(e);if(form.id==='unlock-form')sessionStorage.removeItem('manus-access-token');}
    finally{if(btn)btn.disabled=false;}
  });
  document.addEventListener('input',ev=>{if(ev.target.id==='search-input')searchResults(ev.target.value);});
  $('#close-sheet').addEventListener('click',close);
  $('#overlay').addEventListener('click',e=>{if(e.target===$('#overlay'))close();});
  document.addEventListener('keydown',e=>{if(e.key==='Escape')close();});
  async function boot(){try{await refresh();}catch(e){if(e.message!=='Workspace needs unlocking')toastError(e);}}
  boot();
  setInterval(()=>{if($('#overlay').classList.contains('hidden'))refresh().catch(()=>{});},15000);
})();
