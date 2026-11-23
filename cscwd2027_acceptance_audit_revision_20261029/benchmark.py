"""Public NL4Opt adaptation with explicit variable/objective metadata and offline truth."""
from __future__ import annotations
import hashlib,json,re
from pathlib import Path
import numpy as np
from scipy.optimize import linprog
from scipy.optimize import milp,LinearConstraint,Bounds

ROOT=Path(__file__).resolve().parent
def dump(name,obj):
    p=ROOT/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8')
def load(name):return json.loads((ROOT/name).read_text(encoding='utf-8'))
def digest(x):return hashlib.sha256(json.dumps(x,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def number(x):
    if isinstance(x,(int,float)):return float(x)
    s=str(x).replace(',','').replace('$','').strip()
    if s.endswith('%'):return float(s[:-1])/100
    return float(s)

def sentences(text):
    chunks=re.split(r'(?<=[.!?])\s+(?=[A-Z])',text)
    return [dict(cid=f's{i}',text=t) for i,t in enumerate(chunks) if t]

def to_rows(record):
    n=len(record['vars']);mapping=record['order_mapping'];rows=[]
    for c in record['const_declarations']:
        a=[0.]*n;t=c['type'];b=0.;cmp='le' if c['operator']=='LESS_OR_EQUAL' else 'ge' if c['operator']=='GREATER_OR_EQUAL' else 'eq'
        if t=='linear':
            for key,val in c['terms'].items():a[mapping[key]]+=number(val)
            b=number(c['limit'])
        elif t in ('lowerbound','upperbound'):a[mapping[c['var']]]=1.;b=number(c['limit'])
        elif t=='sum':a=[1.]*n;b=number(c['limit'])
        elif t=='xy':a[mapping[c['x_var']]]=1.;a[mapping[c['y_var']]]-=1.
        elif t=='xby':a[mapping[c['x_var']]]=1.;a[mapping[c['y_var']]]-=number(c['param'])
        elif t=='ratio':
            p=number(c['limit']);a=[-p]*n;a[mapping[c['var']]]+=1.
        else:raise ValueError('unsupported '+t)
        rows.append(dict(a=a,cmp=cmp,b=b,cid='reference'))
    objective=[0.]*n
    for key,val in record['obj_declaration']['terms'].items():objective[mapping[key]]+=number(val)
    return rows,dict(coefficients=objective,sense=record['obj_declaration']['direction'])

def canonical(row):
    a=np.array(row['a'],float);b=float(row['b']);cmp=row['cmp']
    if cmp=='ge':a=-a;b=-b;cmp='le'
    magnitude=max(np.max(np.abs(a)),abs(b),1e-12)
    return np.r_[a,b]/magnitude,cmp

def same_row(a,b):
    va,sa=canonical(a);vb,sb=canonical(b)
    return sa==sb and np.allclose(va,vb,rtol=1e-7,atol=1e-8)

def solve(rows,objective):
    n=len(objective['coefficients']);ub=[];rhs=[];eq=[];erhs=[]
    for r in rows:
        a=np.array(r['a'],float);b=float(r['b']);assert len(a)==n and np.isfinite(a).all() and np.isfinite(b)
        if r['cmp']=='eq':eq.append(a);erhs.append(b)
        else:ub.append(a if r['cmp']=='le' else -a);rhs.append(b if r['cmp']=='le' else -b)
    c=np.array(objective['coefficients'],float)*(-1 if objective['sense']=='maximize' else 1)
    if objective.get('integrality'):
        constraints=[]
        if ub:constraints.append(LinearConstraint(np.array(ub),-np.inf,np.array(rhs)))
        if eq:constraints.append(LinearConstraint(np.array(eq),np.array(erhs),np.array(erhs)))
        sol=milp(c,integrality=np.array(objective['integrality']),bounds=Bounds(np.zeros(n),np.full(n,np.inf)),constraints=constraints,options={'time_limit':10})
    else:sol=linprog(c,A_ub=np.array(ub) if ub else None,b_ub=np.array(rhs) if rhs else None,
        A_eq=np.array(eq) if eq else None,b_eq=np.array(erhs) if erhs else None,bounds=[(0,None)]*n,method='highs')
    return dict(status=int(sol.status),x=None if sol.x is None else sol.x.tolist(),
                value=None if sol.x is None else float(np.dot(sol.x,objective['coefficients'])))

def feasible(rows,x):
    if x is None or any(not np.isfinite(v) or v< -1e-6 for v in x):return False
    for r in rows:
        lhs=float(np.dot(r['a'],x));b=r['b'];tol=1e-5*max(1,abs(b),abs(lhs))
        if r['cmp']=='le' and lhs>b+tol:return False
        if r['cmp']=='ge' and lhs<b-tol:return False
        if r['cmp']=='eq' and abs(lhs-b)>tol:return False
    return True

def score(rows,case):
    solution=solve(rows,case['objective']);x=solution['x'];safe=feasible(case['gold'],x)
    optimum=case['reference']['value'];optimal=safe and abs(solution['value']-optimum)<=1e-4*max(1,abs(optimum))
    matched=set();tp=0
    for r in rows:
        for i,g in enumerate(case['gold']):
            if i not in matched and same_row(r,g):tp+=1;matched.add(i);break
    return dict(solution=solution,valid_optimal=int(optimal),reference_feasible=int(safe),
        invalid=int(x is not None and not safe),no_solution=int(x is None),row_tp=tp,row_fp=len(rows)-tp,row_fn=len(case['gold'])-tp,
        exact_rows=int(tp==len(rows)==len(case['gold'])))

def prepare():
    selected=[];audit=[];seen=set()
    for split,count in [('dev',12),('test',80)]:
        eligible=[];path=ROOT/'vendor/nl4opt/generation_data'/f'{split}.jsonl'
        for line in path.read_text(encoding='utf-8').splitlines():
            key,r=next(iter(json.loads(line).items()));reason=None
            try:
                if len(r['vars'])!=2:raise ValueError('not_two_variables')
                if re.search(r'(?<!no )less than|(?<!no )more than',r['document'],re.I):
                    # Keep conventional non-strict phrases 'no less/more than'; avoid ambiguous strict comparisons.
                    cleaned=re.sub(r'no (?:less|more) than','',r['document'],flags=re.I)
                    if re.search(r'\b(?:less|more) than\b',cleaned,re.I):raise ValueError('strict_wording')
                rows,obj=to_rows(r);ref=solve(rows,obj)
                if ref['status']!=0:raise ValueError('reference_not_finite_feasible')
                textkey=digest(r['document'].strip().lower())
                if textkey in seen:raise ValueError('duplicate_document')
                seen.add(textkey)
                eligible.append(dict(id='nl4opt:'+key,split=split,text=r['document'],sentences=sentences(r['document']),
                    variables=r['vars'],aliases=r['order_mapping'],objective=obj,gold=rows,reference=ref))
            except Exception as e:reason=str(e)
            audit.append(dict(split=split,id=key,eligible=reason is None,exclusion=reason))
        ordered=sorted(eligible,key=lambda r:digest(['residual-20261027',r['id']]))
        assert len(ordered)>=count,(split,len(ordered),count)
        selected.extend(ordered[:count])
    dump('cases.json',selected);dump('selection_audit.json',audit)
    dump('data_manifest.json',dict(upstream_commit='49f1e0d66b7fdcd33305a7f281c2a7c13f5620ea',
        cases_sha256=sha(ROOT/'cases.json'),protocol_sha256=sha(ROOT/'PROTOCOL.md'),benchmark_sha256=sha(__file__),
        dev=12,test=80,selected_ids=[r['id'] for r in selected],
        task='conditional constraint extraction; all methods receive annotated variable schema and objective',
        gold_used_only_for='eligibility,offline scoring and diagnostic oracle control'))
    print(json.dumps({s:dict(total=sum(a['split']==s for a in audit),eligible=sum(a['split']==s and a['eligible'] for a in audit),selected=sum(r['split']==s for r in selected)) for s in ['dev','test']},indent=2))

if __name__=='__main__':prepare()
