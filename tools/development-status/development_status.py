"""Read-only local development dashboard; no messages, network or task control."""
from __future__ import annotations
import argparse, collections, datetime as dt, json, os, pathlib, re, sqlite3, sys, time
DB = CHECKPOINTS = PLAN = None
TZ = dt.timezone.utc
KNOWN = []
FRIENDLY_PLAN = {}
RULES = []
REPLACEMENTS = {}
PLAN_ID_PATTERN = r"\b[A-Z][A-Z0-9]*-\d+(?:[A-Za-z0-9.-]*)\b"


def configure(config_path):
 """Read one explicitly selected local profile; relative paths belong to it."""
 global DB, CHECKPOINTS, PLAN, TZ, KNOWN, FRIENDLY_PLAN, RULES, REPLACEMENTS, PLAN_ID_PATTERN
 path = pathlib.Path(config_path).expanduser().resolve()
 settings = json.loads(path.read_text(encoding='utf-8-sig'))
 def local_path(key):
  value = settings[key]
  if not isinstance(value, str) or not value: raise ValueError('Missing profile path: ' + key)
  result = pathlib.Path(os.path.expandvars(value)).expanduser()
  return result.resolve() if result.is_absolute() else (path.parent / result).resolve()
 DB, CHECKPOINTS, PLAN = (local_path(k) for k in ('state_db', 'checkpoints', 'plan'))
 TZ = dt.timezone(dt.timedelta(hours=settings.get('timezone_offset_hours', 0)))
 threads = settings.get('threads', [])
 if not isinstance(threads, list): raise ValueError('threads must be a list')
 if not threads and not settings.get('team_registry'): raise ValueError('threads or team_registry is required')
 KNOWN = [(t['id'], t['name'], t.get('scope', '')) for t in threads]
 if any(not isinstance(t[0], str) or not t[0] for t in KNOWN): raise ValueError('thread id is required')
 if len(KNOWN) > 120: raise ValueError('at most 120 root threads')
 if len({t[0] for t in KNOWN}) != len(KNOWN): raise ValueError('duplicate thread id')
 FRIENDLY_PLAN = settings.get('friendly_plan', {})
 REPLACEMENTS = settings.get('text_replacements', {})
 PLAN_ID_PATTERN = settings.get('plan_id_pattern', r"\b[A-Z][A-Z0-9]*-\d+(?:[A-Za-z0-9.-]*)\b")
 re.compile(PLAN_ID_PATTERN)
 RULES = []
 for rule in settings.get('rules', []):
  if not isinstance(rule['summary'], str) or not isinstance(rule['validation'], str): raise ValueError('rule text must be strings')
  if not isinstance(rule['plan_ids'], list) or any(not isinstance(x, str) for x in rule['plan_ids']): raise ValueError('plan_ids must be strings')
  RULES.append((re.compile(rule['pattern']), rule['summary'], rule['plan_ids'], rule['validation']))
 return settings

def parse_at(value):
 try:
  return dt.datetime.fromisoformat(str(value).replace('Z','+00:00')).astimezone(TZ)
 except (ValueError,TypeError):return None

def plain(text):
 text=re.sub(r'\[([^\]]+)\]\([^)]*\)',r'\1',str(text))
 replacements=REPLACEMENTS
 for old,new in replacements.items():text=text.replace(old,new)
 text=re.sub(r'`[^`]*`','',text)
 text=re.sub(r'\b[0-9a-f]{8,64}\b','',text,flags=re.I)
 text=re.sub(r'[A-Z]:[/\\][^\s，。；\n]+','（文件位置见详情）',text)
 text=re.sub(r'\s+',' ',text).strip()
 return text[:180]+('…' if len(text)>180 else '')

def describe(text):
 """Ordered project rules are associations, never assignment or acceptance."""
 for pattern, summary, ids, validation in RULES:
  if pattern.search(text): return summary, list(ids), validation
 explicit = re.findall(PLAN_ID_PATTERN, text)
 readable = plain(text)
 if not readable: readable = '记录中暂时没有可读的工作说明'
 return readable, list(dict.fromkeys(explicit))[:2], '尚无可归纳的验证结论'

def status_from(checkpoint, latest_event, public, now):
 task=str(checkpoint.get('task',''))
 if re.search(r'原Goal仍paused|Goal\s*(?:仍)?paused|已暂停|暂停开发',task,re.I):
  return '已暂停开发','paused'
 cp_at=parse_at(checkpoint.get('updated_at'))
 event_at=parse_at(latest_event.get('at')) if latest_event else None
 if latest_event and latest_event['kind']=='task_started' and (not cp_at or (event_at and event_at>cp_at)):
  return '本轮处理中（有开始记录）','working'
 if re.search(r'待接收|等待.*接收|已交Root|已交付|交Root待',task):
  return '已交付，等待主线接收','delivered'
 if latest_event:
  if latest_event['kind']=='task_complete':
   if latest_event.get('error'):return '本轮已结束，存在异常','ended'
   return '本轮已结束','ended'
  if latest_event['kind']=='turn_aborted':return '本轮已中断','paused'
 if public and public.get('phase')=='final_answer':return '已回复结果','ended'
 if public and re.search(r'会一起修|正在.*修|补取资料',public.get('text','')):return '修正中（最近说明）','working'
 if public and re.search(r'先核对|先检查|只提出.*候选',public.get('text','')):return '检查中（最近说明）','working'
 return '状态未确认（仅有进展记录）','unknown'

class Tail:
 def __init__(self,path,limit):
  self.path=pathlib.Path(path);self.limit=limit;self.offset=None;self.pending=b''
  self.messages=collections.deque(maxlen=6);self.event={};self.read_at=None
 def read(self):
  size=self.path.stat().st_size
  if self.offset==size:return
  dropped=False
  if self.offset is None or self.offset>size or size-self.offset>self.limit:
   start=max(0,size-self.limit);self.pending=b'';dropped=start>0
  else:start=self.offset
  with self.path.open('rb') as f:f.seek(start);data=f.read(self.limit)
  self.offset=start+len(data)
  rows=(self.pending+data).split(b'\n');self.pending=rows.pop()
  if len(self.pending)>self.limit:self.pending=b''
  if dropped and rows:rows=rows[1:]
  for raw in rows:
   # Do not parse or retain reasoning/tool payloads.
   is_public=b'"response_item"' in raw and b'"message"' in raw and (b'"commentary"' in raw or b'"final_answer"' in raw)
   is_event=b'"event_msg"' in raw and any(x in raw for x in (b'"task_started"',b'"task_complete"',b'"turn_aborted"'))
   if not (is_public or is_event):continue
   try:e=json.loads(raw);p=e.get('payload',{})
   except (ValueError,TypeError):continue
   if is_public and e.get('type')=='response_item' and p.get('type')=='message' and p.get('role')=='assistant' and p.get('phase') in ('commentary','final_answer'):
    body='\n'.join(v.get('text','') for v in p.get('content',[]) if v.get('type') in ('text','output_text')).strip()
    if body:
     m={'at':e.get('timestamp'),'phase':p['phase'],'text':body[:5000]}
     if not self.messages or self.messages[-1]!=m:self.messages.append(m)
   if is_event and e.get('type')=='event_msg' and p.get('type') in ('task_started','task_complete','turn_aborted'):
    self.event={'at':e.get('timestamp'),'kind':p['type'],'error':bool(p.get('error'))}


def resolve_local_path(value, base):
 if not isinstance(value,str) or not value:raise ValueError('Local path must be a nonempty string')
 if '://' in value or value.startswith(('\\\\','//')):raise ValueError('Only local filesystem paths are supported')
 result=pathlib.Path(os.path.expandvars(value)).expanduser()
 return result.resolve() if result.is_absolute() else (base/result).resolve()

def read_control_json(path):
 """Bounded, consistent local metadata only; never execute content or follow URLs."""
 before=path.stat()
 if before.st_size>512*1024:raise ValueError('Control JSON exceeds 512 KiB')
 with path.open('rb') as source:data=source.read(512*1024+1)
 if len(data)>512*1024:raise ValueError('Control JSON exceeds 512 KiB')
 after=path.stat()
 if (before.st_mtime_ns,before.st_size)!=(after.st_mtime_ns,after.st_size):raise ValueError('Control JSON changed while reading')
 obj=json.loads(data.decode('utf-8-sig'))
 if not isinstance(obj,dict):raise ValueError('Control JSON must be an object')
 return obj

def participant(obj, role):
 if not isinstance(obj,dict):raise ValueError('Participant must be an object')
 tid=obj.get('threadId')
 if not isinstance(tid,str) or not tid or len(tid)>200 or not re.fullmatch(r'[A-Za-z0-9_-]+',tid):raise ValueError('Invalid participant threadId')
 title=obj.get('title') or tid
 if not isinstance(title,str):raise ValueError('Invalid participant title')
 state=obj.get('state','unknown')
 if state not in ('active','waiting','paused','completed','unknown'):raise ValueError('Invalid participant state')
 binding=obj.get('taskBindingId',obj.get('currentTaskBindingId',''))
 if not isinstance(binding,str):raise ValueError('Invalid task binding')
 return {'id':tid,'name':title,'role':role,'task_binding_id':binding,'runtime_state':state}

def validate_roster(rows):
 if len(rows)>120:raise ValueError('At most 120 registered conversations')
 if len({r['id'] for r in rows})!=len(rows):raise ValueError('Duplicate participant threadId')
 if sum(r['role']=='router' for r in rows)!=1:raise ValueError('Exactly one Router is required')
 return rows

def registry_roster(team):
 entries=team.get('newThreads')
 if not isinstance(entries,dict):raise ValueError('newThreads must be an object')
 result=[]
 for key,item in entries.items():
  if not isinstance(item,dict):raise ValueError('Invalid registry entry')
  role=item.get('role',key)
  if role=='router' or key=='main':result.append(participant(item,'router'))
  elif isinstance(role,str) and role.startswith('worker'):result.append(participant(item,'worker'))
 return validate_roster(result)

def runtime_roster(status):
 if type(status.get('schema')) is not int or status['schema']!=1:raise ValueError('Unsupported runtime schema')
 if type(status.get('developmentEnabled')) is not bool:raise ValueError('developmentEnabled must be boolean')
 workers=status.get('workers')
 if not isinstance(workers,list):raise ValueError('workers must be an array')
 return validate_roster([participant(status.get('router'),'router')]+[participant(w,'worker') for w in workers])

def worker_limit(value):
 if type(value) is not int or not 1<=value<=120:raise ValueError('Invalid maxInUseWorkers')
 return value

class Collector:
 def __init__(self, config_path):
  self.settings=configure(config_path)
  self.profile=pathlib.Path(config_path).expanduser().resolve()
  self.team_path=resolve_local_path(self.settings['team_registry'],self.profile.parent) if self.settings.get('team_registry') else None
  self.status_path=resolve_local_path(self.settings['runtime_status'],self.profile.parent) if self.settings.get('runtime_status') else None
  self.known=list(KNOWN);self.members={};self.control={}
  self.catalog={};self.tails={};self.catalog_at=0;self.catalog_mode='';self.warnings=[]
  self.plan={};self.plan_sig=None
 def team_refresh(self):
  if not self.team_path:return
  team=None;runtime=None;rows=None;limits=[];flags=[];failed=False;source='team_registry'
  status_path=self.status_path
  try:
   team=read_control_json(self.team_path)
   rows=registry_roster(team)
   development=team.get('development',{})
   if not isinstance(development,dict):raise ValueError('Invalid development metadata')
   enabled=development.get('enabled')
   if enabled is not None and type(enabled) is not bool:raise ValueError('Invalid enabled flag')
   if enabled is not None:flags.append(enabled)
   if development.get('state')=='paused_by_user':flags.append(False)
   policy=team.get('workerPolicy',{})
   if not isinstance(policy,dict):raise ValueError('Invalid workerPolicy')
   if 'maxInUseWorkers' in policy:limits.append(worker_limit(policy['maxInUseWorkers']))
   if not status_path and team.get('runtime',{}).get('statusPath'):
    status_path=resolve_local_path(team['runtime']['statusPath'],self.team_path.parent)
  except (OSError,ValueError,KeyError,TypeError,AttributeError):
   failed=True;self.warnings.append('团队登记读取失败；名单与暂停状态可能滞后，不能据此判断开发停止。')
  if status_path:
   try:
    runtime=read_control_json(status_path)
    runtime_rows=runtime_roster(runtime)
    limit=worker_limit(runtime['maxInUseWorkers'])
    rows=runtime_rows;limits.append(limit);flags.append(runtime['developmentEnabled']);source='runtime_status'
   except (OSError,ValueError,KeyError,TypeError):
    runtime=None;failed=True;self.warnings.append('运行状态读取失败；使用团队登记或上次名单，不推断任务完成。')
  if failed and self.control.get('development_enabled') is False:flags.append(False)
  enabled=False if False in flags else (True if flags and not failed else None)
  if len(set(flags))>1:self.warnings.append('开发开关记录不一致；任一显式暂停均优先显示。')
  if len(set(limits))>1:self.warnings.append('Worker上限记录不一致；显示较小值并保留全部成员。')
  limit=min(limits) if limits else self.control.get('worker_limit')
  if rows is not None:
   members={r['id']:r for r in rows}
   known=[(r['id'],r['name'],r['task_binding_id']) for r in rows]
   if members!=self.members:
    self.catalog_at=0
    # Preserve only currently registered roots and their known descendants.
    keep=set(members)
    for _ in range(4):
     keep.update(tid for tid,meta in self.catalog.items() if meta.get('parent') in keep)
    self.catalog={tid:meta for tid,meta in self.catalog.items() if tid in keep}
    self.tails={tid:tail for tid,tail in self.tails.items() if tid in keep}
   self.members=members;self.known=known
  elif not self.members:
   self.known=[]
  in_use=sum(r['role']=='worker' and r['runtime_state']!='completed' for r in self.members.values())
  completed=sum(r['role']=='worker' and r['runtime_state']=='completed' for r in self.members.values())
  if limit is not None and in_use>limit:self.warnings.append(f'在用Worker {in_use} 个，超过登记上限 {limit}；未隐藏超限成员。')
  recorded_at=(runtime or {}).get('updatedAt') or (team or {}).get('updatedAt') or ''
  if isinstance(recorded_at,(int,float)) and not isinstance(recorded_at,bool):
   try:recorded_at=dt.datetime.fromtimestamp(recorded_at,TZ).strftime('%m-%d %H:%M:%S')
   except (ValueError,OverflowError,OSError):recorded_at='时间格式未知'
  self.control={'mode':'dynamic','development_enabled':enabled,'worker_limit':limit,'in_use_workers':in_use,
                'completed_workers':completed,'router_count':sum(r['role']=='router' for r in self.members.values()),
                'source':source if rows is not None else 'last_known','recorded_at':str(recorded_at),
                'read_error':failed,'registry_path':str(self.team_path),'runtime_path':str(status_path or '')}
 def ensure_members(self):
  for tid,name,scope in self.known:
   if tid not in self.catalog and self.team_path:
    self.catalog[tid]={'id':tid,'name':name,'group':name,'kind':'对话','parent':None,'rollout_path':'','agent_path':'','missing_registration':True}
   if tid in self.catalog:self.catalog[tid].update(name=name,group=name)
 def plans(self):
  sig=(PLAN.stat().st_mtime_ns,PLAN.stat().st_size)
  if sig!=self.plan_sig:
   obj=json.loads(PLAN.read_text(encoding='utf-8-sig'));index={}
   def add(t):
    index[t['id']]={k:t.get(k) for k in ['id','title','status','acceptance']}
    for sub in t.get('subtasks',[]):add(sub)
   for t in obj['tasks']:add(t)
   self.plan=index;self.plan_sig=sig
 def catalog_refresh(self):
  if self.catalog_at and time.monotonic()-self.catalog_at<30:return
  try:
   conn=sqlite3.connect(DB.as_uri()+'?mode=ro',uri=True,timeout=1)
   conn.execute('PRAGMA query_only=ON');self.catalog_mode='只读实时登记'
  except sqlite3.Error:
   conn=sqlite3.connect(DB.as_uri()+'?mode=ro&immutable=1',uri=True,timeout=1)
   self.catalog_mode='只读磁盘快照（发现新智能体可能延迟）'
  try:
   conn.row_factory=sqlite3.Row
   keys='id,rollout_path,source,agent_path,agent_nickname,updated_at,archived'
   new={};queue=collections.deque()
   for tid,name,scope in self.known:
    r=conn.execute('SELECT '+keys+' FROM threads WHERE id=?',(tid,)).fetchone()
    if r:
     row=dict(r);row.update(name=name,group=name,kind='对话',parent=None);new[tid]=row;queue.append((tid,name,0))
   while queue and len(new)<120:
    parent,group,depth=queue.popleft()
    if depth>=4:continue
    rows=conn.execute('SELECT '+keys+' FROM threads WHERE source LIKE ? ORDER BY updated_at DESC LIMIT 121',('%'+parent+'%',)).fetchall()
    for raw in rows:
     if len(new)>=120: break
     r=dict(raw)
     try:spawn=json.loads(r['source'])['subagent']['thread_spawn']
     except (ValueError,KeyError,TypeError):continue
     if spawn.get('parent_thread_id')!=parent or r['id'] in new:continue
     r.update(name='子智能体 · '+str(r.get('agent_nickname') or '未命名'),group=group,kind='子智能体',parent=parent)
     new[r['id']]=r;queue.append((r['id'],group,depth+1))
  finally:
   conn.close()
  self.catalog=new;self.catalog_at=time.monotonic()
  if len(new)>=120:self.warnings.append('已达到120条读取上限，清单可能不完整。')
 def snapshot(self):
  now=dt.datetime.now(TZ);self.warnings=[]
  self.team_refresh()
  try:self.plans()
  except (OSError,ValueError,KeyError):self.warnings.append('开发计划暂时无法读取；关联结果保留为待确认。')
  try:self.catalog_refresh()
  except (OSError,sqlite3.Error):self.warnings.append('对话登记暂时无法读取；保留上次清单。')
  self.ensure_members()
  current=[];history=[];used={}
  for tid,meta in self.catalog.items():
   cp={};cp_path=CHECKPOINTS/(tid+'.json')
   if cp_path.exists():
    try:
     if cp_path.stat().st_size<=64*1024:cp=json.loads(cp_path.read_text(encoding='utf-8-sig'))
    except (OSError,ValueError):pass
   tail=self.tails.get(tid);source_error=not bool(meta.get('rollout_path'))
   if not source_error:
    if tail is None or str(tail.path)!=meta['rollout_path']:
     tail=Tail(meta['rollout_path'],2*1024*1024 if meta['kind']=='对话' else 256*1024);self.tails[tid]=tail
    try:tail.read()
    except OSError:source_error=True
   messages=tail.messages if tail else []
   latest_event=tail.event if tail else {}
   public=messages[-1] if messages else {}
   public_at=parse_at(public.get('at'));cp_at=parse_at(cp.get('updated_at'))
   # The current checkpoint is the task, newer public messages can supersede it.
   text=cp.get('task','')
   source_at=cp_at
   if public_at and (not cp_at or public_at>cp_at):text=public['text'];source_at=public_at
   if not text:text='暂时没有可读的工作说明'
   brief,ids,validation=describe(text)
   context_used=False
   if not ids and public and public.get('text')!=text:
    turn_start=parse_at(latest_event.get('at')) if latest_event.get('kind')=='task_started' else None
    if not turn_start or (public_at and public_at>=turn_start):
     contextual=describe(text+'\n'+public['text'])
     if contextual[1]:brief,ids,validation=contextual;context_used=True
   if not ids:
    # An implementation-progress note may omit the subject. Keep the recorded context visible.
    candidates=[m['text'] for m in reversed(messages) if m['text']!=text]
    if cp.get('task') and cp['task']!=text:candidates.append(cp['task'])
    for candidate in candidates:
     previous=describe(candidate)
     if previous[1]:
      ids=previous[1];context_used=True
      brief=previous[0]+'（本条进展见下方详情）'
      validation='计划依据最近工作上下文关联，当前细节见原始说明'
      break
   status,state=status_from(cp,latest_event,public,now)
   event_at=parse_at(latest_event.get('at'))
   # A fresh start without a new work description is not evidence the previous job resumed.
   if state=='working' and event_at and source_at and event_at>source_at+dt.timedelta(seconds=1):
    brief='新一轮已开始，具体工作内容还没有更新';validation='当前工作待更新；所列计划是上一条记录的关联';context_used=True;source_at=event_at
   member=self.members.get(tid,{})
   registered_state=member.get('runtime_state','unknown')
   if registered_state=='completed':
    status,state='任务已完成（登记）','completed'
   elif member and self.control.get('development_enabled') is False:
    status,state='待命：用户暂停开发','paused'
   elif member and self.control.get('read_error'):
    status,state='状态未确认（沿用可读登记）','unknown'
   elif registered_state=='paused':status,state='任务已暂停（登记）','paused'
   elif registered_state=='waiting':status,state='等待依赖（登记）','waiting'
   elif registered_state=='active':status,state='任务执行中（状态记录）','working'
   elif member and source_error:
    status,state='状态未确认（记录缺失）','unknown'
   matched=[]
   for item in ids:
    if item in self.plan:
     task=self.plan[item];matched.append({'id':item,'title':FRIENDLY_PLAN.get(item,task['title']),'original_title':task['title'],'status':task['status'],'acceptance':task['acceptance']});used[item]=matched[-1]
   if matched:
    plan_label='\n'.join(p['id']+'\n'+p['title'] for p in matched)
    association=('按最近工作上下文关联，当前记录未明确写计划编号。' if context_used else '按记录内容关联。')+' 不改动原计划状态或派工关系。'
   else:plan_label='待关联到开发计划';association='现有记录不足，尚未确定对应条目。'
   age=int(max(0,(now-source_at).total_seconds()/60)) if source_at else None
   updated=source_at.strftime('%m-%d %H:%M') if source_at else '未记录'
   if age is not None:updated+='\n'+(str(age)+'分钟前' if age<60 else str(age//60)+'小时'+str(age%60)+'分前')
   raw='最近工作记录：\n'+text
   if public and public.get('text')!=text:raw+='\n\n最近公开说明：\n'+public['text']
   if cp:raw+='\n\n检查点中的下一步：\n'+'\n'.join(cp.get('next_actions',[]))
   if member:
    raw+='\n\n团队登记：'+member['role']+'；任务绑定：'+(member['task_binding_id'] or '未记录')+'；状态：'+registered_state
    raw+='\n开发开关和状态文件不是进程停止证明；宿主本轮结束不等于任务完成。'
   if source_error:raw+='\n\n公开进展记录暂时不可读。'
   row={'id':tid,'name':meta['name'],'group':meta['group'],'kind':meta['kind'],'agent_path':meta.get('agent_path') or '',
        'plan':plan_label,'plan_entries':matched,'association':association,'work':brief,'validation':validation,
        'status':status,'state':state,'updated':updated,'source_at':source_at.isoformat() if source_at else '',
        'raw':raw,'source_error':source_error,'parent':meta['parent'],'role':member.get('role',''),
        'task_binding_id':member.get('task_binding_id',''),'registered_state':registered_state}
   last=max([x for x in [source_at,event_at] if x],default=None)
   ancestor=tid
   for _ in range(5):
    if ancestor in self.members or ancestor not in self.catalog:break
    ancestor=self.catalog[ancestor].get('parent')
   completed_owner=self.members.get(ancestor,{}).get('runtime_state')=='completed'
   if completed_owner:history.append(row)
   elif meta['kind']=='对话' or (last and last.date()==now.date()):current.append(row)
   else:history.append(row)
  order={tid:i for i,(tid,_,_) in enumerate(self.known)}
  current.sort(key=lambda x:(0,order[x['id']]) if x['id'] in order else (1,x['group'],x['name']))
  history.sort(key=lambda x:x['source_at'],reverse=True)
  return {'at':now.isoformat(),'rows':current,'history':history,'plans':list(used.values()),'warnings':self.warnings,
          'catalog_mode':self.catalog_mode,'known_count':len(self.known),'control':self.control,'agent_count':sum(r['kind']=='子智能体' for r in current),
          'history_count':len(history),'policy':'只读状态记录；开发开关不是进程停止证明，本轮结束或宿主空闲不等于任务完成。'}

def main():
 parser = argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--config', required=True, help='Explicit machine-local project profile JSON')
 parser.add_argument('--once', action='store_true', help='Print one read-only snapshot')
 args = parser.parse_args()
 collector = Collector(args.config)
 if args.once:
  print(json.dumps(collector.snapshot(), ensure_ascii=False)); return
 for command in sys.stdin:
  if command.strip() == 'quit': break
  try: result = collector.snapshot()
  except Exception as e: result = {'error': type(e).__name__, 'message': '暂时无法读取分工记录。'}
  print(json.dumps(result, ensure_ascii=False), flush=True)
if __name__ == '__main__': main()
