"""Paired local inference with explicit source-grounded controls and no oracle feedback."""
from __future__ import annotations
import argparse,json,time,urllib.request
from datetime import datetime,timezone
from benchmark import ROOT,load,dump,digest,sha,score
import legacy_repair as old
import ledger as L

BASE='''Translate the original source into a complete acceptance constraint set for exactly two nonnegative variables in the supplied order and integrality domain. The objective and variable schema are fixed metadata; return constraints only. Preserve every binding requirement, including coordinated bounds, total budgets and resource limits. Resource capacities generally bind a sum of per-variable usages, not the count of a single product. A number describing how many product types or methods exist is schema metadata, not a lower bound. Use the quantity catalogue to distinguish per-variable resource coefficients, resource capacities, fractions and multiplicative ratios. Source IDs are localization aids, not correctness certificates. Do not invent facts or use reference answers. A quantity with a boundary cue is an audit candidate, not automatically a binding constraint: check its actual meaning in the original sentence. If an audit warning is a false positive, retain the source-faithful model rather than inventing a constraint. Output JSON only.'''
NUMERIC=BASE+' '+old.COMMON
TYPED=BASE+''' Return source-grounded records: kind bound sets x[target] cmp value(rhs); sum sets x0+x1 cmp value(rhs); linear sets value(terms[0])*x0+value(terms[1])*x1 cmp value(rhs); ratio sets x[target] cmp value(rhs)*(x0+x1); multiple sets x[target] cmp value(rhs)*x[1-target]; difference sets x[target] cmp x[1-target]; offset sets x[target]-x[1-target] cmp value(rhs). rhs and terms must be IDs from the quantity catalogue, NEVER literal numbers. terms must contain two IDs for linear and [] otherwise. target is 0 or1 (use0 when irrelevant). The compiler handles ratios and multiples; do not manually negate coefficients. Use the requirement's actual comparison direction. Source number mentions inside variable names are not quantities. Worked interface example: q=s1n0 is2hours for A, q=s2n0 is3hours for B, q=s3n0 is60hours capacity: linear,le,target0,rhs=s3n0,terms=[s1n0,s2n0]. At least8A with q=s4n0: bound,ge,target0,rhs=s4n0,terms=[]. At least70percent B with q=s5n0: ratio,ge,target1,rhs=s5n0,terms=[].'''
REVIEW=' Review the entire first-pass set against the original source and all quantitative clauses. Correct omissions, wrong comparisons, variable bindings, resource coefficients, percentages and scope. Return a COMPLETE corrected set, not a patch. The source-based diagnostics can be fallible. No hidden reference or gold counterexample is available.'

def safe_numeric(parsed,case):
    try:return old.validated(parsed,case),None
    except Exception as e:return [],repr(e)

def safe_typed(parsed):
    try:
        assert isinstance(parsed,dict) and isinstance(parsed['constraints'],list)
        return parsed['constraints'],None
    except Exception as e:return [],repr(e)

def payload(case):
    return dict(variable_order=case['variables'],aliases=case['aliases'],objective=case['objective'],
        sentences=case['sentences'],quantity_catalogue=L.inventory(case),explicit_rule_bounds=L.explicit_bounds(case))

def frozen_config():
    return dict(files={n:sha(ROOT/n) for n in ['PROTOCOL.md','DEVELOPMENT.md','benchmark.py','legacy_repair.py','ledger.py','run.py','test_cases.json','dev_cases.json','external_selection.json','equivalence.py']},
        prompts=dict(numeric=NUMERIC,typed=TYPED,review=REVIEW),seed=271029,temperature=0,context=8192,cap=1400,
        schemas=dict(numeric=old.SCHEMA,typed=L.SCHEMA))

def run(args):
    casefile=args.cases or args.split+'_cases.json';cases=load(casefile);folder=ROOT/args.output;folder.mkdir(exist_ok=True)
    if args.split=='test':
        frozen=ROOT/'frozen.json'
        if frozen.exists():assert load('frozen.json')==frozen_config(),'Frozen source changed'
        else:dump('frozen.json',frozen_config())
        bound=dict(casefile=casefile,sha256=sha(ROOT/casefile))
        datafreeze=folder/'data_freeze.json'
        if datafreeze.exists():assert json.loads(datafreeze.read_text(encoding='utf-8'))==bound
        else:datafreeze.write_text(json.dumps(bound,indent=2),encoding='utf-8')
    path=folder/'raw.jsonl';records={}
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            r=json.loads(line);assert r['key'] not in records;records[r['key']]=r
    byhash={r['request_sha256']:r for r in records.values() if 'shared_from' not in r}
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def call(case,stage,prompt,data,schema):
        req=dict(model=args.model,stream=False,think=False,format=schema,keep_alive='30m',
            options=dict(temperature=0,seed=271029,num_ctx=8192,num_predict=1400),
            messages=[dict(role='system',content=prompt),dict(role='user',content=json.dumps(data))])
        key=args.model+'/'+case['id']+'/'+stage;h=digest(req)
        if key in records:
            assert records[key]['request_sha256']==h;return records[key].get('parsed',{})
        r=dict(key=key,id=case['id'],split=args.split,model=args.model,stage=stage,request=req,request_sha256=h,utc=datetime.now(timezone.utc).isoformat())
        if h in byhash:
            origin=byhash[h];r.update(shared_from=origin['key'],seconds=0.)
            for field in ['response','parsed','error']:
                if field in origin:r[field]=origin[field]
        else:
            started=time.perf_counter()
            try:
                r['response']=json.load(opener.open(urllib.request.Request('http://127.0.0.1:11434/api/chat',data=json.dumps(req).encode(),headers={'Content-Type':'application/json'}),timeout=180))
                r['parsed']=json.loads(r['response']['message']['content'])
            except Exception as e:r['error']=repr(e)
            r['seconds']=time.perf_counter()-started;byhash[h]=r
        with path.open('a',encoding='utf-8') as f:f.write(json.dumps(r,ensure_ascii=False)+'\n')
        records[key]=r
        print(args.model,case['id'],stage,round(r['seconds'],2),r.get('error',''),flush=True)
        return r.get('parsed',{})
    for index,c in enumerate(cases):
        data=payload(c)
        n=call(c,'numeric_initial',NUMERIC,data,old.SCHEMA);nr,_=safe_numeric(n,c)
        call(c,'numeric_review',NUMERIC+REVIEW,dict(data,first_pass=nr,diagnostics=[]),old.SCHEMA)
        call(c,'numeric_feedback',NUMERIC+REVIEW,dict(data,first_pass=nr,diagnostics=L.numeric_diagnostics(nr,c)),old.SCHEMA)
        t=call(c,'typed_initial',TYPED,data,L.SCHEMA);tr,_=safe_typed(t)
        call(c,'typed_review',TYPED+REVIEW,dict(data,first_pass=tr,diagnostics=[]),L.SCHEMA)
        _,issues=L.compile_records(tr,c,checks=True)
        call(c,'typed_feedback',TYPED+REVIEW,dict(data,first_pass=tr,diagnostics=issues),L.SCHEMA)
        print('PROGRESS',index+1,len(cases),flush=True)
    analyze(folder,args.split,casefile)

def analyze(folder,split,casefile=None):
    cases={c['id']:c for c in load(casefile or split+'_cases.json')};raw={r['key']:r for r in [json.loads(l) for l in (folder/'raw.jsonl').read_text(encoding='utf-8').splitlines()]}
    rows=[]
    for model,cid in sorted({(r['model'],r['id']) for r in raw.values()}):
        c=cases[cid];states={}
        for stage in ['numeric_initial','numeric_review','numeric_feedback','typed_initial','typed_review','typed_feedback']:
            rec=raw.get(model+'/'+cid+'/'+stage)
            if rec is not None:states[stage]=rec.get('parsed',{})
        if len(states)!=6:continue
        n,_=safe_numeric(states['numeric_initial'],c);g,_=safe_numeric(states['numeric_review'],c);f,_=safe_numeric(states['numeric_feedback'],c)
        t,_=safe_typed(states['typed_initial']);tg,_=safe_typed(states['typed_review']);tf,_=safe_typed(states['typed_feedback'])
        unchecked,_=L.compile_records(t,c,checks=False);ug,_=L.compile_records(tg,c,checks=False);uf,_=L.compile_records(tf,c,checks=False)
        checked,issues=L.compile_records(tf,c,checks=True);cg,gi=L.compile_records(tg,c,checks=True)
        methods={'rules_only':L.explicit_bounds(c),'numeric_one':n,'legacy_guarded':old.merge(g,old.anchors(c)),
            'numeric_full':g,'rules_numeric_one':L.reconcile(n,c),'rules_numeric_full':L.reconcile(g,c),
            'numeric_feedback':L.reconcile(f,c),'grounded_one':L.reconcile(unchecked,c),
            'grounded_full':L.reconcile(ug,c),'grounded_full_checked':L.reconcile(cg,c),
            'ledger_unchecked':L.reconcile(uf,c),'ledger_full':L.reconcile(checked,c)}
        for method,contract in methods.items():
            metrics=score(contract,c)
            rows.append(dict(id=cid,dataset=c['dataset'],split=split,model=model,method=method,contract=contract,
                diagnostic_count=len(issues) if method.startswith('ledger') else len(gi) if method=='grounded_full_checked' else None,**metrics))
    (folder/'results.json').write_text(json.dumps(rows,indent=2),encoding='utf-8');summary=[]
    for model in sorted({r['model'] for r in rows}):
      for method in sorted({r['method'] for r in rows}):
        rs=[r for r in rows if r['model']==model and r['method']==method]
        summary.append(dict(model=model,method=method,n=len(rs),**{k:sum(r[k] for r in rs) for k in ['valid_optimal','reference_feasible','invalid','no_solution','exact_rows','row_tp','row_fp','row_fn']}))
    (folder/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8');print(json.dumps(summary,indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--split',choices=['dev','test'],default='dev');p.add_argument('--model',default='qwen3.5:4b');p.add_argument('--output',default='development_v1');p.add_argument('--analyze',action='store_true');p.add_argument('--cases');a=p.parse_args()
    if a.analyze:analyze(ROOT/a.output,a.split,a.cases)
    else:run(a)
