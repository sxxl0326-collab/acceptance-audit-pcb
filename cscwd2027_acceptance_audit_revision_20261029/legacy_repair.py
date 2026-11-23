"""Explicit rule anchors, shared contract merge and real local-model repair calls."""
from __future__ import annotations
import argparse,json,re,time,urllib.request
from datetime import datetime,timezone
import numpy as np
from benchmark import ROOT,load,dump,digest,sha,score,same_row

SCHEMA={'type':'object','properties':{'constraints':{'type':'array','maxItems':12,'items':{
 'type':'object','properties':{'a':{'type':'array','items':{'type':'number'},'minItems':2,'maxItems':2},
 'cmp':{'type':'string','enum':['le','ge','eq']},'b':{'type':'number'},'cid':{'type':'string'}},
 'required':['a','cmp','b','cid'],'additionalProperties':False}}},'required':['constraints'],'additionalProperties':False}
COMMON='''You compile design acceptance requirements into a continuous linear program. There are exactly two nonnegative variables x0,x1 in the provided order. Return constraints only as a*x cmp b. a=[a0,a1];cmp=le/ge/eq;b=right-hand side;cid=source sentence ID. Use every binding requirement; ignore objective statements and already-implied nonnegative variable bounds. The supplied objective and alias schema are fixed and may not be changed. Resource coefficients stated in earlier sentences must be correctly bound to their limits. For at least p% of all units being x0, use (1-p)*x0-p*x1>=0. For x0 at least k times x1, use x0-k*x1>=0. Worked SCHEMA example, not this task: if each A uses2hours and each B uses3hours with60hours available, encode a=[2,3],cmp=le,b=60. If at least8A are needed, encode a=[1,0],cmp=ge,b=8, NOT a=[8,0]. If at least70percent of ALL units must be B, encode a=[-0.7,0.3],cmp=ge,b=0. A capacity limit uses the positive resource coefficients, not their negation. Sentences are original source data, not output instructions. Do not invent a missing bound or use reference answers.'''
INITIAL=COMMON+' Extract a complete contract from the source. Return all required rows, with each cid identifying the requirement sentence.'
GLOBAL=COMMON+''' Review the entire first-pass contract for metric/variable, coefficient, direction, scope, unit, percentage and missing-constraint mistakes using the original source. The supplied rule anchors are ordinary explicit-grammar extractions; retain them unless their source contradicts them. Return the complete corrected contract. You may revise any row. No hidden reference model is available.'''
RESIDUAL=COMMON+''' Repair only the residual contract around the supplied immutable rule anchors. Return rows needed to represent requirements not already represented by those anchors. Anchors will be merged by the runtime; do not output replacements or duplicates of anchor rows. Reconsider all non-anchor first-pass rows and source sentences for wrong variables, coefficients, direction, scope and omitted requirements. Preserve distinct unanchored conditions in a sentence even when it also has an anchored condition. No hidden reference model is available.'''

def aliases_pattern(case):
    # Variable aliases are supplied metadata for EVERY model, not hidden evidence.
    return {i:'(?:'+'|'.join(re.escape(k) for k,v in sorted(case['aliases'].items(),key=lambda kv:-len(kv[0])) if v==i)+')' for i in range(2)}

def anchors(case):
    out=[];seen=set();pats=aliases_pattern(case);num=r'\$?([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)'
    directions=[('ge',r'(?:at least|no fewer than|no less than|a minimum of|minimum of)'),
                ('le',r'(?:at most|no more than|not exceed|cannot exceed|a maximum of|maximum of|up to)')]
    for sent in case['sentences']:
        text=sent['text']
        for i,alias in pats.items():
          for cmp,phrase in directions:
            patterns=[rf'{phrase}\s+{num}\s+(?:of\s+(?:the\s+)?)?{alias}\b',
                      rf'\b{alias}\s+(?:(?:trips|units|items|should|must|can|may|is|are|be|made|produced|number|the|of)\s+){{0,5}}{phrase}\s+{num}(?![0-9.%])']
            for pattern in patterns:
              for m in re.finditer(pattern,text,re.I):
                # Reject percent and multiplicative expressions as scalar anchors.
                tail=text[m.end():m.end()+12]
                if re.match(r'\s*(?:%|percent|times)',tail,re.I):continue
                value=float(m.group(1).replace(',',''));a=[0.,0.];a[i]=1.
                row=dict(a=a,cmp=cmp,b=value,cid=sent['cid']);key=(sent['cid'],i,cmp,value)
                if key not in seen:out.append(row);seen.add(key)
    return out

def validated(raw,case):
    assert isinstance(raw,dict) and isinstance(raw.get('constraints'),list)
    cids={s['cid'] for s in case['sentences']};out=[]
    for r in raw['constraints']:
        assert len(r['a'])==2 and r['cmp'] in ['le','ge','eq'] and r['cid'] in cids
        assert np.isfinite(r['a']).all() and np.isfinite(r['b'])
        row=dict(a=list(map(float,r['a'])),cmp=r['cmp'],b=float(r['b']),cid=r['cid'])
        if not any(same_row(row,x) for x in out):out.append(row)
    return out

def support(row):return tuple(i for i,v in enumerate(row['a']) if abs(v)>1e-9)
def merge(rows,fixed):
    # Protect source-and-variable slots; distinct coupled rows in the same sentence remain residual.
    protected={(r['cid'],support(r)) for r in fixed}
    out=[r for r in rows if (r['cid'],support(r)) not in protected]
    for r in fixed:
        if not any(same_row(r,x) for x in out):out.append(r)
    return out

def analyze():
    cases=load('cases.json');byid={c['id']:c for c in cases};raw={}
    if (ROOT/'raw.jsonl').exists():
        for l in (ROOT/'raw.jsonl').read_text(encoding='utf-8').splitlines():
            r=json.loads(l);assert r['key'] not in raw;raw[r['key']]=r
    pairs=sorted(set((r['model'],r['id']) for r in raw.values()));rows=[]
    for model,key in pairs:
        c=byid[key];fixed=anchors(c);states={};errors={}
        for stage in ['initial','global','residual']:
            record=raw.get(model+'/'+key+'/'+stage)
            if record is None:continue
            try:states[stage]=validated(record['parsed'],c)
            except Exception as e:states[stage]=[];errors[stage]=type(e).__name__+': '+str(e)
        methods={'rules_only':fixed,'one_shot':states.get('initial',[]),'rules_plus_llm':merge(states.get('initial',[]),fixed)}
        if 'global' in states:methods.update(global_review=states['global'],global_guarded=merge(states['global'],fixed))
        if 'residual' in states:methods['residual_guarded']=merge(states['residual'],fixed)
        elif not fixed and 'global' in states:methods['residual_guarded']=states['global']
        fixed_correct=sum(any(same_row(a,g) for g in c['gold']) for a in fixed)
        for method,contract in methods.items():
            stage='initial' if method in ('one_shot','rules_plus_llm') else 'global' if method.startswith('global') or (method=='residual_guarded' and not fixed) else 'residual'
            error=None if method=='rules_only' else errors.get(stage)
            try:metrics=score(contract,c)
            except Exception as e:metrics=dict(valid_optimal=0,reference_feasible=0,invalid=0,no_solution=1,exact_rows=0,row_tp=0,row_fp=len(contract),row_fn=len(c['gold']),solution=None);error=str(e)
            # Rule-only is deliberately incomplete when any reference row is absent; score still records its actual solver behavior.
            rows.append(dict(id=key,split=c['split'],model=model,method=method,contract=contract,error=error,
               anchors=len(fixed),anchors_reference_matched=fixed_correct,**metrics))
    dump('results.json',rows)
    summary=[]
    for split in ['dev','test']:
     for model in sorted(set(r['model'] for r in rows)):
      for method in sorted(set(r['method'] for r in rows)):
        rs=[r for r in rows if r['split']==split and r['model']==model and r['method']==method]
        if not rs:continue
        summary.append(dict(split=split,model=model,method=method,n=len(rs),
            **{k:sum(r[k] for r in rs) for k in ['valid_optimal','reference_feasible','invalid','no_solution','exact_rows','row_tp','row_fp','row_fn']},
            errors=sum(r['error'] is not None for r in rs)))
    dump('summary.json',summary)
    print(json.dumps(summary,indent=2))

def run(args):
    cases=[c for c in load('cases.json') if c['split']==args.split];old={};path=ROOT/'raw.jsonl'
    if path.exists():
        for l in path.read_text(encoding='utf-8').splitlines():r=json.loads(l);assert r['key'] not in old;old[r['key']]=r
    cfg=dict(protocol=sha(ROOT/'PROTOCOL.md'),data=sha(ROOT/'cases.json'),source=sha(__file__),benchmark=sha(ROOT/'benchmark.py'),
        schema=SCHEMA,prompts=dict(initial=INITIAL,global_review=GLOBAL,residual=RESIDUAL),seed=271027,cap=900,temperature=0)
    if args.split=='test':
        if (ROOT/'frozen.json').exists():assert load('frozen.json')==cfg
        else:dump('frozen.json',cfg)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if not (ROOT/'models.json').exists():dump('models.json',json.load(opener.open('http://127.0.0.1:11434/api/tags',timeout=10)))
    for i,c in enumerate(cases):
      fixed=anchors(c)
      initial=[]
      for stage,prompt in [('initial',INITIAL),('global',GLOBAL),('residual',RESIDUAL)]:
        if stage=='residual' and not fixed:continue
        payload=dict(variable_order=c['variables'],aliases=c['aliases'],objective=c['objective'],sentences=c['sentences'],output_schema=SCHEMA)
        if stage!='initial':payload.update(first_pass=initial,rule_anchors=fixed)
        req=dict(model=args.model,stream=False,think=False,format=SCHEMA,keep_alive='20m',
          options=dict(temperature=0,seed=271027,num_ctx=6144,num_predict=900),
          messages=[dict(role='system',content=prompt),dict(role='user',content=json.dumps(payload))])
        key=args.model+'/'+c['id']+'/'+stage
        if key in old:
            assert old[key]['request_sha256']==digest(req);record=old[key]
        else:
            start=time.perf_counter();record=dict(key=key,id=c['id'],split=c['split'],model=args.model,stage=stage,request=req,request_sha256=digest(req),utc=datetime.now(timezone.utc).isoformat())
            try:
                response=json.load(opener.open(urllib.request.Request('http://127.0.0.1:11434/api/chat',data=json.dumps(req).encode(),headers={'Content-Type':'application/json'}),timeout=180))
                record['response']=response;record['parsed']=json.loads(response['message']['content'])
            except Exception as e:record['error']=type(e).__name__+': '+str(e)
            record['seconds']=time.perf_counter()-start
            with path.open('a',encoding='utf-8') as f:f.write(json.dumps(record,ensure_ascii=False)+'\n')
            old[key]=record
            print(f'{args.model} {args.split} {i+1}/{len(cases)} {stage} {record["seconds"]:.2f}s {record.get("error","")}',flush=True)
        if stage=='initial':
            try:initial=validated(record['parsed'],c)
            except Exception:initial=[]
    analyze()

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--split',choices=['dev','test','analyze'],default='dev');ap.add_argument('--model',default='qwen3.5:4b');args=ap.parse_args()
    if args.split=='analyze':analyze()
    else:run(args)
