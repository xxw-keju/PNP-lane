const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const safeURL = value => /^https:\/\/github\.com\//.test(value || '') ? value : '#';
const clock = value => value ? new Date(value).toLocaleTimeString('en-GB', {timeZone:'UTC',hour12:false}) : '—';
const date = value => value ? `${new Date(value).toLocaleDateString('en-GB',{timeZone:'UTC',day:'2-digit',month:'2-digit'})} ${clock(value)}` : '—';
const ago = value => { if(!value) return '暂无记录'; const s=Math.max(0,(Date.now()-Date.parse(value))/1000); return s<60 ? `${Math.floor(s)} 秒前` : s<3600 ? `${Math.floor(s/60)} 分钟前` : `${Math.floor(s/3600)} 小时前`; };
const badge = (text, color='neutral') => `<span class="badge ${color}">${esc(text)}</span>`;
const states = {open:['开放','green'], merged:['已合并','purple'], closed:['已关闭','neutral']};
const agentStates = {running:['进行中','green'],recent:['近期活动','green'],idle:['空闲','neutral'],unconfirmed:['需确认','amber'],unknown:['状态待确认','neutral']};
let data=null, paused=false, inflight=false, prFilter='all', prLimit=10, taskLimit=10, taskScope='all', disconnected=false;

function adoption(a) {
  if(a.adoption.includes('run')) return '已实际运行 fastlane';
  if(a.adoption.includes('prepare')) return '已准备 fastlane 任务';
  if(a.adoption.includes('tasks')) return '已使用共享任务清单';
  if(a.adoption.includes('read')) return '已读取使用说明';
  return '未见设施调用记录';
}
function setHTML(id, html) { if($(id).innerHTML!==html) $(id).innerHTML=html; }
function renderAgents() {
  $('agent-count').textContent=data.agents.length;$('agent-nav-count').textContent=data.agents.length;
  $('repository-link').href=safeURL('https://github.com/'+data.repository);
  setHTML('agent-grid',data.agents.map(a=>{const state=agentStates[a.state]||agentStates.unknown;return `<article class="agent-card">
    <div class="agent-head"><span class="agent-symbol ${a.engine==='Claude'?'claude':''}">${a.engine==='Claude'?'✳':'⌘'}</span><div><div class="agent-name">${esc(a.name)}</div><div class="engine">${esc(a.engine)}</div></div>${badge(...state)}</div>
    <p class="agent-description">${esc(a.engine==='Claude' && a.last_tool ? a.last_tool : a.summary || a.last_tool || '等待会话记录')}</p>
    <div class="agent-usage ${a.adoption.some(x=>x!=='read')?'used':''}"><span class="tiny-dot"></span>${adoption(a)}</div>
    <div class="agent-foot"><span>${ago(a.last_activity)}</span><button class="text-button" data-agent="${esc(a.id)}">查看记录 ↗</button></div></article>`}).join(''));
}
function laneResult(j) {
  const linked=data.prs.filter(p=>j.linked_prs.some(link=>link.url===p.url));
  const pr=j.pr_url ? data.prs.find(p=>p.url===j.pr_url) : linked.length===1 ? linked[0] : null;
  const waiting={proposed:'提案已登记，等待证明与正式上架',precompiled:'预编译通过，等待发布时核对资格',activated:'上架资格已核对，准备发布'};
  const local=j.accepted ? '完整本地验证通过' : waiting[j.state] || (j.state==='stopped' ? '本地流水线已中断' : j.state==='prepared' ? '已准备，等待证明就绪' : j.state==='full_check' ? '完整本地验证进行中' : '本地流水线执行中');
  let label=j.accepted?'本地验证通过':({proposed:'提案预研',precompiled:'预编译通过',activated:'准备发布'}[j.state] || (j.state==='stopped'?'运行已中断':j.state==='prepared'?'已准备':'执行中'));
  let color=j.accepted?'green':j.state==='stopped'?'red':'purple';
  const notes=[];
  if(j.compiled_reused) notes.push('已复用 lane 预编译记录。');
  if(j.release_to_pr_seconds!=null) notes.push(`发布阶段 ${j.release_to_pr_seconds.toFixed(2)} 秒。`);
  if(pr) {
    const refused=['REFUSED','REJECTED'].includes(pr.verdict);
    if(pr.state==='merged') { label='PR 已合并';color='purple';notes.push(`PR #${pr.number} 已合并到主分支。`); }
    else if(pr.state==='closed') { label='PR 已关闭 · 未合并';color='neutral';notes.push(`PR #${pr.number} 已关闭，未合并。`); }
    else { label=refused?'PR 开放 · 官方拒绝':pr.verdict==='ACCEPTED'?'PR 开放 · 官方接受':'PR 开放';color=refused?'red':'green'; }
    if(pr.first_state==='merged' && pr.first_pr!==pr.number) notes.push(`同题 #${pr.first_pr} 已合并。`);
    if(pr.reason) notes.push(`最近官方检查：${pr.reason}`);
  } else if(j.pr_url) {
    label='PR 状态待同步';color='amber';notes.push('PR 已创建，等待同步 GitHub 当前状态。');
  }
  if(j.error) notes.push(j.error.includes('could not read Username')?'历史运行在推送认证处中断。认证配置现已修复，该运行记录保留原状态。':j.error.split('\n')[0]);
  return {pr,local,label,color,note:notes.join(' '),terminal:pr?.state==='closed'||pr?.state==='merged'};
}
function renderLane() {
  $('lane-count').textContent=`${data.jobs.length} 个作业 · ${data.counts.lane_published} 次发布`;
  const jobs=[...data.jobs].sort((a,b)=>(b.updated_at||b.created_at||'').localeCompare(a.updated_at||a.created_at||''));const job=jobs[0];
  const steps=[['prepare','准备'],['guard','规则检查'],['compile','普通编译'],['push','安全推送'],['create_pr','创建 PR'],['full_check','完整验证']];
  setHTML('pipeline',`<div class="pipeline">${steps.map(([key,label],i)=>{let status='',duration='待执行';
    const s=job?.stages.filter(s=>s.name===key).at(-1);
    if(job && key==='prepare'){status='done';duration='已准备'}
    if(s){status=s.exit_code===0?'done':s.end?'failed':'running';duration=s.seconds!=null?`${s.seconds.toFixed(2)}s`:'进行中'}
    return `<div class="stage ${status}"><div class="stage-circle">${status==='done'?'✓':status==='failed'?'!':i+1}</div><div class="stage-name">${label}</div><div class="stage-time">${duration}</div></div>`}).join('')}</div>`);
  setHTML('jobs',jobs.length ? jobs.slice(0,4).map(j=>{const r=laneResult(j);return `<div class="job ${r.terminal?'job-finished':j.state==='stopped'?'job-failed':'job-active'}"><div class="job-top"><span class="job-task">${esc(j.task)} <span class="muted">/ ${esc(j.agent||'未归属')}</span></span>${badge(r.label,r.color)}</div>
    ${r.note?`<p>${esc(r.note)}</p>`:''}<p class="job-local">本地记录：${esc(r.local)}。</p>
    <div class="job-bottom"><span>${r.pr?`<a class="job-pr" href="${safeURL(r.pr.url)}" target="_blank" rel="noreferrer">PR #${r.pr.number} ↗</a> · `:''}${j.pr_url?'由 fastlane 发布':j.linked_prs.length?'关联 PR 由其他流程发布':'尚无关联 PR'}</span><button class="text-button" data-job="${esc(j.id)}">阶段详情 ↗</button></div></div>`;}).join(''):'<div class="empty">尚无 fastlane 运行记录</div>');
}
function renderSources(){const h=data.health;setHTML('watcher-badge',badge(h.watcher_alive?'刷新器运行中':'刷新器未运行',h.watcher_alive?'green':'red'));
  setHTML('sources',[
    ['▤','公开任务与权重',`目标间隔 ${h.task_interval??'—'} 秒`,h.task_checked_at],
    ['◇','公开未上架提案',`题面待补全 ${h.proposal_details_pending??'—'} 项`,h.proposal_checked_at],
    ['↗','PR 与己方分支',`目标间隔 ${h.pr_interval??'—'} 秒`,h.pr_checked_at],
    ['✓','官方检查评论','独立读取 · 60 秒',h.feedback_checked_at]
  ].map(([icon,label,sub,t])=>`<div class="source-row"><span class="source-icon">${icon}</span><div><strong>${label}</strong><small>${sub}</small></div><div class="source-time">${ago(t)}<small>${clock(t)} UTC</small></div></div>`).join(''));
}
function renderPRs(){const query=$('pr-search').value.toLowerCase();const prs=data.prs.filter(p=>(prFilter==='all'||p.state===prFilter)&&`${p.number} ${p.task} ${p.agent} ${p.title}`.toLowerCase().includes(query));$('pr-count').textContent=data.prs.length;$('pr-showing').textContent=`显示 ${Math.min(prLimit,prs.length)} / ${prs.length} 条`;$('more-prs').hidden=prs.length<=prLimit;
  setHTML('pr-body',prs.length?prs.slice(0,prLimit).map(p=>{const state=states[p.state]||['未知','neutral'];const refused=['REFUSED','REJECTED'].includes(p.verdict);return `<tr><td><a class="pr-title" href="${safeURL(p.url)}" target="_blank" rel="noreferrer">${esc(p.task||'证明提交')} <span class="pr-number">#${p.number}</span></a></td>
    <td>${esc(p.agent||'未确认归属')}<span class="table-sub">${p.route==='fastlane'?'fastlane 发布':p.route==='fallback'?'其他流程发布':'未记录到 fastlane'}</span></td><td>${p.weight??'—'}</td><td><span class="rank ${p.rank===1?'first':''}">${p.rank?`第 ${p.rank} 个`:'—'}</span></td>
    <td>${badge(...state)} ${p.verdict?badge(refused?'官方拒绝':p.verdict==='ACCEPTED'?'官方接受':p.verdict,refused?'red':'green'):''}<span class="table-sub" title="${esc(p.reason||'')}">${esc(p.reason?.replace(/^(REFUSED before any build|REJECTED|ACCEPTED):?\s*/, '')|| (p.first_state==='merged'&&p.first_pr!==p.number?'同题已有其他 PR 合并':'暂无已记录的官方结论'))}</span></td>
    <td>${date(p.created_at)}</td><td><button class="text-button" data-pr="${p.number}">详情 ↗</button></td></tr>`}).join(''):'<tr><td colspan="7" class="empty">没有符合筛选条件的 PR</td></tr>');
}
function taskAssessment(t){const a=t.assessment; if(!a)return '';
  const statusNames={active:'当前有效',superseded:'已更新',retracted:'已撤回',stale:'题面已变，失效',evidence_stale:'依据已变，待复核'};
  const feasibilityNames={unknown:'未评估',viable:'有证明路线',blocked:'暂时受阻',refuted:'有反证依据'};
  return field('难度',a.difficulty==null?'未评分（按 3 档排序）':`${a.difficulty}/5 · ${a.difficulty_label}`)+field('可证明性评估',a.feasibility_label)+
    (a.difficulty_disputed?'<p class="detail-paragraph">难度评分有分歧，排序暂取当前有效评分中的最高分。</p>':'')+
    (a.basis==='agent_report'?'<p class="muted">以下为 agent 的评估；反证引用已检查其来自已验证题目，逻辑蕴含关系仍是评估者的判断。</p>':'')+
    (a.automatic_evidence||[]).map(e=>`<p class="detail-paragraph">已验证题面与目标命题正好互为否定：<a class="detail-link" href="${safeURL(e.url)}" target="_blank" rel="noreferrer">${esc(e.task)} ↗</a></p>`).join('')+
    (a.history?.length?`<details class="assessment-history" open><summary>评估记录 · ${a.history.length} 条${a.stale_count?` · ${a.stale_count} 条已失效`:''}</summary>${a.history.map(r=>`<article class="assessment-entry"><strong>${esc(r.agent)} · ${esc(statusNames[r.status]||r.status)}</strong><small>${date(r.at)} UTC · 难度 ${r.difficulty??'未评分'} · ${esc(feasibilityNames[r.feasibility]||'已撤回')}${r.estimated_minutes?` · 预计 ${r.estimated_minutes} 分钟`:''}</small><p class="detail-paragraph">${esc(r.reason)}</p>${r.evidence_task?`<p class="muted">引用已验证题目：${esc(r.evidence_task)}</p>`:''}${r.evidence?`<p class="detail-paragraph">依据：${safeURL(r.evidence)!=='#'?`<a class="detail-link" href="${safeURL(r.evidence)}" target="_blank" rel="noreferrer">${esc(r.evidence)}</a>`:esc(r.evidence)}</p>`:''}</article>`).join('')}</details>`:'<p class="muted">尚无 agent 提交评估。</p>');
}
function renderTasks(){
  const query=$('task-search').value.toLowerCase();
  const proposed=t=>t.task_state==='proposed';
  const tasks=data.tasks.filter(t=>(taskScope==='all'||(proposed(t)?'proposed':'open')===taskScope)
    &&(!$('eligible-only').checked||t.eligible)&&`${t.id} ${t.title}`.toLowerCase().includes(query));
  const byWeight=$('task-sort')?.value==='weight';
  tasks.sort((a,b)=>{
    if(proposed(a)!==proposed(b))return proposed(a)?-1:1;
    if(proposed(a))return (b.public_updated_at||'').localeCompare(a.public_updated_at||'')||a.id.localeCompare(b.id);
    return (byWeight?0:(a.difficulty??3)-(b.difficulty??3))||(b.weight??0)-(a.weight??0)||a.id.localeCompare(b.id);
  });
  $('task-total').textContent=`已上架 ${data.counts.listed??data.counts.tasks} · 未上架 ${data.counts.proposed??0}`;
  for(const b of document.querySelectorAll('[data-task-scope]')){
    const scope=b.dataset.taskScope,count=scope==='all'?data.counts.tasks:scope==='open'?(data.counts.listed??data.counts.tasks):(data.counts.proposed??0);
    b.textContent=({all:'全部',open:'已上架',proposed:'未上架提案'}[scope])+` (${count})`;
    b.classList.toggle('selected',scope===taskScope);
  }
  setHTML('task-list',tasks.length?tasks.slice(0,taskLimit).map(t=>{
    const a=t.assessment,isProposal=proposed(t);
    const state=isProposal?badge('未上架 · 可预研','amber'):a?.excluded?badge(a.feasibility==='needs_review'?'反证待复核':'有反证依据','red'):t.pr_check_pending?badge('PR 待核对','amber'):t.eligible?badge('可初选','green'):t.has_pr?badge(`${t.pr_count||'已有'} PR`):t.own_branch?badge('己方分支'):badge('暂不可选');
    const participants=t.participants?.length?`<div class="task-reviewers">已登记：${esc(t.participants.join('、'))}</div>`:'';
    return `<div class="task-row"><div class="weight-box" title="${isProposal?'权重尚未公布':'权重'}">${t.weight??'—'}</div><div class="task-info"><div class="task-id">${esc(t.id)} <span class="task-difficulty" title="${esc(a?.difficulty_label||'未评分')}">${t.difficulty==null?'未评分':`难度 ${t.difficulty}`}</span></div><div class="task-title" title="${esc(t.title)}">${esc(t.title)}</div>${participants}${a?.reports?.length?`<div class="task-reviewers">${esc(a.reports.map(r=>r.agent).join('、'))} · ${esc(a.feasibility_label)}${a.difficulty_disputed?' · 评分有分歧':''}</div>`:''}</div>${state}<button class="text-button" data-task="${esc(t.id)}">↗</button></div>`;
  }).join(''):'<div class="empty">没有符合筛选条件的任务</div>');
  $('more-tasks').hidden=tasks.length<=taskLimit;$('more-tasks').textContent=`显示更多 · 还有 ${Math.max(0,tasks.length-taskLimit)} 个`;
}
function taskDetails(t){
  if(t.task_state==='proposed'){
    const link=/^https:\/\/data\.pequalnp\.com\/data\/lemma\/L-[a-z0-9-]+\.[a-f0-9]+\.json$/.test(t.source_url||'')?`<a class="detail-link" href="${t.source_url}" target="_blank" rel="noreferrer">查看公开提案 ↗</a>`:'';
    return field('状态','尚未上架，可预研，暂不可提交')+field('权重','尚未公布')+field('已登记 agent',t.participants?.join('、')||'暂无')+field('已索引同题 PR',t.pr_count)+field('公开更新时间',date(t.public_updated_at)+' UTC')+`<p class="detail-paragraph">${esc(t.title)}</p><pre class="detail-pre">${esc(t.statement||'题面正在后台读取，请稍后刷新。')}</pre>`+link+`<p class="muted">公开提案不保证以后上架。允许 imports、证明方向及发布资格需等正式任务公布后核对。</p>`;
  }
  return field('状态','已上架')+field('权重',t.weight)+field('同题 PR',t.pr_count)+field('己方同名分支',t.own_branch?'存在':'无')+field('初选条件',t.pr_check_pending?'等待 PR 核对':t.eligible?'符合':'暂不符合')+`<p class="detail-paragraph">${esc(t.title)}</p>`+taskAssessment(t)+`<p class="muted">难度：1 直接可证明；2 可以证明；3 需要一点难度；4 需要很长枚举；5 开放问题。“可初选”不等于已经有证明。</p>`;
}

function renderEvents(){setHTML('timeline',data.events.slice(0,12).map(e=>`<div class="timeline-item ${e.category==='pr'?'pr':''}"><span class="timeline-dot"></span><div class="timeline-copy"><div class="timeline-meta"><span>${clock(e.at)}</span><span>${esc(e.agent)}</span></div><p title="${esc(e.text)}">${esc(e.text)}</p></div></div>`).join('')||'<div class="empty">暂无活动</div>');}
function render(){for(const k of ['total','open','merged','eligible'])$(`metric-${k}`).textContent=data.counts[k];$('updated').textContent=clock(data.generated_at);renderAgents();renderLane();renderSources();renderPRs();renderTasks();renderEvents();renderAlert();}
function renderAlert(){const errors=disconnected?['无法连接本地服务，当前显示的是上次成功读取的快照。']:[...(data?.errors||[])];if(data?.health.feedback_error)errors.push('官方评论暂未更新：'+data.health.feedback_error);$('alert').textContent=errors.join('\n');$('alert').classList.toggle('hidden',!errors.length);}
function field(label,value){return `<div class="detail-field"><span>${esc(label)}</span>${esc(value??'—')}</div>`;}
function show(title,html){$('detail-title').textContent=title;$('detail-body').innerHTML=html;$('detail').showModal();}
document.addEventListener('click',event=>{const b=event.target.closest('button');if(!b||!data)return;
  if(b.dataset.pr){const p=data.prs.find(x=>x.number===Number(b.dataset.pr));show(`${p.task} · PR #${p.number}`,field('状态',states[p.state]?.[0])+field('Agent',p.agent||'未确认归属')+field('原始权重',p.weight)+field('提交排名',p.rank?`第 ${p.rank} 个`:'未知')+field('发布时间',date(p.created_at)+' UTC')+`<a class="detail-link" href="${safeURL(p.url)}" target="_blank" rel="noreferrer">在 GitHub 打开 PR ↗</a><p class="detail-paragraph">${esc(p.reason||'尚无已记录的官方检查结论。')}</p>`+(p.verdict_url?`<a class="detail-link" href="${safeURL(p.verdict_url)}" target="_blank" rel="noreferrer">查看官方评论 ↗</a>`:''));}
  if(b.dataset.agent){const a=data.agents.find(x=>x.id===b.dataset.agent);show(`${a.name} · ${a.engine}`,field('活动状态',agentStates[a.state]?.[0])+field('最近活动',date(a.last_activity)+' UTC')+field('设施使用',adoption(a))+`<p class="detail-paragraph">最近工具：${esc(a.last_tool||'无记录')}</p><p class="detail-paragraph">最近公开说明：\n${esc(a.summary||'暂无记录')}</p><p class="muted">空闲状态来自会话结束事件；最近工具和说明可能属于上一轮任务。</p>`);}
  if(b.dataset.job){const j=data.jobs.find(x=>x.id===b.dataset.job);const r=laneResult(j);show(`${j.task} · 运行记录`,field('执行者',j.agent)+field('当前结果',r.label)+field('本地记录',r.local)+field('作业更新于',date(j.updated_at||j.created_at)+' UTC')+(r.pr?field('PR 更新于',date(r.pr.updated_at)+' UTC')+`<a class="detail-link" href="${safeURL(r.pr.url)}" target="_blank" rel="noreferrer">查看 PR #${r.pr.number} ↗</a>`:'')+`<p class="detail-paragraph">${esc(r.note)}</p><pre class="detail-pre">${esc(j.stages.map(s=>`${s.name.padEnd(13)} ${s.seconds??'—'}s   exit=${s.exit_code??'运行中'}`).join('\n')||'尚未执行编译及提交流水线')}</pre>`+(j.linked_prs.length?`<p class="detail-paragraph">关联 PR：${j.linked_prs.map(p=>`#${p.number}`).join('、')}。${j.pr_url?'由此流水线创建。':'该流水线没有记录成功创建 PR，属于其他发布流程。'}</p>`:''));}
  if(b.dataset.task){const t=data.tasks.find(x=>x.id===b.dataset.task);show(t.id,taskDetails(t));}
  if(b.dataset.taskScope){taskScope=b.dataset.taskScope;taskLimit=10;$('eligible-only').checked=false;$('eligible-only').disabled=taskScope==='proposed';renderTasks();}
});
$('close-detail').addEventListener('click',()=>$('detail').close());$('detail').addEventListener('click',e=>{if(e.target===$('detail')){const r=$('detail').getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)$('detail').close();}});
document.querySelectorAll('[data-pr-filter]').forEach(b=>b.addEventListener('click',()=>{prFilter=b.dataset.prFilter;prLimit=10;document.querySelectorAll('[data-pr-filter]').forEach(x=>x.classList.toggle('selected',x===b));if(data)renderPRs();}));
$('pr-search').addEventListener('input',()=>{prLimit=10;if(data)renderPRs();});$('more-prs').addEventListener('click',()=>{prLimit+=20;renderPRs();});$('task-search').addEventListener('input',()=>{taskLimit=10;if(data)renderTasks();});$('eligible-only').addEventListener('change',()=>{taskLimit=10;if(data)renderTasks();});$('more-tasks').addEventListener('click',()=>{taskLimit+=20;renderTasks();});
$('task-sort')?.addEventListener('change',()=>{taskLimit=10;if(data)renderTasks();});
document.querySelectorAll('.nav-link').forEach(a=>a.addEventListener('click',()=>{document.querySelectorAll('.nav-link').forEach(x=>x.classList.toggle('active',x===a));}));
$('pause').addEventListener('click',()=>{paused=!paused;$('pause').textContent=paused?'继续刷新':'暂停刷新';$('connection-label').textContent=paused?'已暂停':'实时更新';$('connection-dot').classList.toggle('pause-dot',paused);if(!paused)load();});
async function load(){if(paused||inflight||document.hidden)return;inflight=true;try{const response=await fetch('/api/status',{signal:AbortSignal.timeout(12000)});if(!response.ok)throw Error(response.status);data=await response.json();disconnected=false;$('connection-label').textContent='实时更新';$('connection-dot').classList.remove('connection-error');render();}catch(e){disconnected=true;$('connection-label').textContent='连接中断';$('connection-dot').classList.add('connection-error');renderAlert();}finally{inflight=false;}}
document.addEventListener('visibilitychange',()=>{if(!document.hidden)load();});load();setInterval(load,1000);
