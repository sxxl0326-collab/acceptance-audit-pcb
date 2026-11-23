"""Source-quantity ledger, conservative coordinated bounds and binding checks.

Runtime functions accept text/schema only. Reference constraints never enter this module.
"""
from __future__ import annotations
import re,itertools
from benchmark import same_row

WORDS={'one':1.,'two':2.,'three':3.,'four':4.,'five':5.,'six':6.,'seven':7.,'eight':8.,'nine':9.,'ten':10.,
       'half':.5,'twice':2.,'double':2.,'triple':3.}
SIGNAL=re.compile(r'(?P<ge>at least|no fewer than|no less than|(?:a )?minimum(?: of)?)|(?P<le>at most|no more than|not exceed|cannot exceed|(?:a )?maximum(?: of)?|up to)',re.I)
NUM=re.compile(r'(?<!\w)(?:\$?\d+(?:,\d{3})*(?:\.\d+)?(?:\s*(?:%|percent))?|'+ '|'.join(WORDS)+r')\b',re.I)
# A trailing percent is handled explicitly because a word boundary after '%' does not exist.
NUM=re.compile(r'(?<!\w)(?:\$?\d+(?:,\d{3})*(?:\.\d+)?(?:\s*(?:%|percent))?|'+ '|'.join(WORDS)+r')(?!\w)',re.I)

def alias_occurrences(case,text):
    matches=[]
    for alias,var in sorted(case['aliases'].items(),key=lambda p:-len(p[0])):
        alias=alias.rstrip('.')
        if not alias:continue
        flags=0 if len(alias)==1 else re.I
        for m in re.finditer(r'(?<!\w)'+re.escape(alias)+r'(?!\w)',text,flags):
            if len(alias)==1:
                after=text[m.end():]
                before=text[max(0,m.start()-12):m.start()]
                if not (re.match(r'\s+(?:uses?|requires?|costs?|contains?|can|produces?)\b',after,re.I)
                    or re.search(r'\d\s*$',before) or re.search(r'(?:product|fertilizer|type|model|machine)\s*$',before,re.I)):continue
            if not any(m.start()>=a and m.end()<=b and var==v for a,b,v in matches):matches.append((m.start(),m.end(),var))
    return sorted(matches)

def metric(text,start,end):
    tail=text[end:];head=text[:start];found=None
    patterns=[('volume',r'^\s*(?:liters?|litres?|gallons?)\s+(?:of\s+)?([a-z]+)'),
      ('mass',r'^\s*(?:kg|kilograms?|pounds?|lbs?|grams?)\s+(?:of\s+)?([a-z]+)'),
      ('time',r'^\s*(?:hours?|minutes?|seconds?)\s+(?:of\s+)?([a-z]+)'),
      ('count',r'^\s*(?:units?|pieces?)\s+of\s+(vitamin\s+[A-Z]|[a-z]+(?:\s+acid)?)')]
    for kind,pat in patterns:
        m=re.search(pat,tail,re.I)
        if m:found=[kind,m.group(1).lower()];break
    if found and found[1] in {'per','to','on','in','and','are','is','available','needed','required'}:found[1]=''
    if not found:
        if re.match(r'^\s*(?:kg|kilograms?|pounds?|lbs?|grams?)\b',tail,re.I):found=['mass','']
        elif re.match(r'^\s*(?:hours?|minutes?|seconds?)\b',tail,re.I):found=['time','']
        elif re.match(r'^\s*(?:liters?|litres?|gallons?)\b',tail,re.I):found=['volume','']
        elif re.match(r'^\s*(?:units?|pieces?)\b',tail,re.I):
            pre=re.search(r'(vitamin\s+[A-Z]|phosphoric acid|nitrogen)[^.;!?]*$',head,re.I)
            found=['count',pre.group(1).lower() if pre else '']
        elif text[start:end].startswith('$'):found=['money','']
    return found

def inventory(case):
    out=[]
    for sent in case['sentences']:
        text=sent['text'];aliases=alias_occurrences(case,text);vars_here={v for a,b,v in aliases}
        signals=list(SIGNAL.finditer(text))
        for i,m in enumerate(NUM.finditer(text)):
            raw=m.group();s=raw.lower().replace('$','').replace(',','').strip();percent=bool(re.search(r'%|percent',s))
            value=WORDS.get(s)
            if value is None:value=float(re.sub(r'%|percent','',s).strip())/(100 if percent else 1)
            prefix=text[:m.start()];near=[t for t in signals if t.end()<=m.start() and m.start()-t.end()<=95 and not re.search(r'[;.!?]',text[t.end():m.start()])]
            direction=near[-1].lastgroup if near else None
            after=text[m.end():];direct=None
            for a,b,v in aliases:
                if a>=m.end() and re.fullmatch(r'\s*(?:of\s+(?:the\s+)?)?',text[m.end():a]):direct=v;break
            unique=next(iter(vars_here)) if len(vars_here)==1 else None
            out.append(dict(qid=f"{sent['cid']}n{i}",cid=sent['cid'],span=[m.start(),m.end()],surface=raw,value=value,
                percent=percent,direction=direction,metric=metric(text,m.start(),m.end()),
                direct_var=direct,var_hint=direct if direct is not None else unique,
                multiple=bool(re.match(r'\s*times\b',after,re.I)) or s in {'twice','double','triple'}))
    # Metadata counts and explicit availability are separate from coefficient mentions.
    source={s['cid']:s['text'] for s in case['sentences']}
    for q in out:
        tail=source[q['cid']][q['span'][1]:];text=source[q['cid']]
        q['role']='schema_count' if re.match(r'\s+(?:types|methods|projects|products|kinds)\b',tail,re.I) and q['direction'] is None else 'quantity'
        if q['direction'] is None and not q['percent']:
            if re.search(r'\bavailable\b',tail[:90],re.I) and q['metric']:
                q['direction']='le'
            elif q['surface'].startswith('$') and re.search(r'\b(?:invest|budget)\b',text,re.I) and not re.search(r'\b(?:per|cost|profit|return)\b',tail,re.I):
                q['direction']='le'
    return out

def resource_pair(q,catalog):
    if q['direct_var'] is not None or not q['metric'] or not q['metric'][1]:return None
    candidates=[[c for c in catalog if c['qid']!=q['qid'] and c['var_hint']==var and c['direction'] is None and c['metric']==q['metric']] for var in [0,1]]
    return [cs[0]['qid'] for cs in candidates] if all(len(cs)==1 for cs in candidates) else None

def explicit_bounds(case):
    rows=[]
    for q in inventory(case):
        if q['direction'] is None or q['direct_var'] is None or q['percent'] or q['multiple']:continue
        a=[0.,0.];a[q['direct_var']]=1.
        r=dict(a=a,cmp=q['direction'],b=q['value'],cid=q['cid'],qid=q['qid'])
        if not any(same_row(r,s) and r['cid']==s['cid'] for s in rows):rows.append(r)
    return rows

def reconcile(rows,case):
    fixed=explicit_bounds(case);keys={(r['cid'],tuple(i for i,a in enumerate(r['a']) if abs(a)>1e-9)) for r in fixed}
    out=[r for r in rows if (r['cid'],tuple(i for i,a in enumerate(r['a']) if abs(a)>1e-9)) not in keys]
    for r in fixed:
        if not any(same_row(r,s) for s in out):out.append(r)
    return out

SCHEMA={'type':'object','properties':{'constraints':{'type':'array','maxItems':12,'items':{'type':'object','properties':{
    'kind':{'type':'string','enum':['bound','sum','linear','ratio','multiple','difference','offset']},
    'cmp':{'type':'string','enum':['le','ge','eq']},'target':{'type':'integer','minimum':0,'maximum':1},
    'rhs':{'type':'string'},'terms':{'type':'array','items':{'type':'string'},'maxItems':2}},
    'required':['kind','cmp','target','rhs','terms'],'additionalProperties':False}}},'required':['constraints'],'additionalProperties':False}

def compile_records(records,case,checks=True):
    catalog=inventory(case);qs={q['qid']:q for q in catalog};rows=[];issues=[];covered=set()
    for i,r in enumerate(records):
      try:
        q=qs[r['rhs']];v=q['value'];kind=r['kind'];t=int(r['target']);cmp=r['cmp'];a=[0.,0.];b=v
        if kind=='bound':a[t]=1.
        elif kind=='sum':a=[1.,1.]
        elif kind=='linear':
            assert len(r['terms'])==2,'linear requires two source quantities'
            co=[qs[k] for k in r['terms']];a=[c['value'] for c in co]
            if checks:
                for var,c in enumerate(co):
                    assert c['var_hint'] is None or c['var_hint']==var,'coefficient bound to another variable'
                    if c['metric'] and q['metric']:
                        cm,qm=c['metric'],q['metric']
                        assert cm[0]==qm[0],'coefficient/limit dimension mismatch'
                        assert not (cm[1] and qm[1] and cm[1]!=qm[1]),'coefficient/limit resource mismatch'
        elif kind=='ratio':a=[-v,-v];a[t]+=1.;b=0.
        elif kind=='multiple':a[t]=1.;a[1-t]=-v;b=0.
        elif kind=='difference':a[t]=1.;a[1-t]=-1.;b=0.
        elif kind=='offset':a[t]=1.;a[1-t]=-1.;b=v
        else:raise ValueError('unsupported kind')
        if checks:
            assert q.get('role')!='schema_count','number describes the variable schema, not an acceptance limit'
            assert q['direction'] is None or cmp==q['direction'],'comparison contradicts source boundary cue'
            if kind=='bound':
                assert not q['percent'] and not q['multiple'],'fraction/multiple interpreted as scalar bound'
                assert q['var_hint'] is None or q['var_hint']==t,'bound attached to another named variable'
                assert resource_pair(q,catalog) is None,'resource limit has per-variable coefficients and requires an aggregate row'
            if kind=='ratio':
                assert q['percent'] or q['surface'].lower()=='half','ratio requires fraction/percentage source'
                assert q['var_hint'] is None or q['var_hint']==t,'fraction attached to another named variable'
        row=dict(a=a,b=b,cmp=cmp,cid=q['cid'],qid=q['qid'],record=i)
        if not any(same_row(row,s) for s in rows):rows.append(row)
        covered.add(q['qid'])
      except Exception as e:issues.append(dict(record=i,type='binding_or_schema',detail=str(e)))
    for q in catalog:
        if q['direction'] and q['qid'] not in covered:issues.append(dict(qid=q['qid'],type='uncovered_boundary',surface=q['surface'],cid=q['cid']))
    return rows,issues

def numeric_diagnostics(rows,case):
    """Same source catalogue and unit hints for free numeric-row baselines.

    Only uniquely identifiable literal matches are diagnosed; ambiguity is kept visible.
    """
    qs=inventory(case);issues=[];covered=set()
    for i,row in enumerate(rows):
        candidates=[q for q in qs if q['cid']==row['cid'] and abs(q['value']-abs(row['b']))<1e-7]
        for q in candidates:
            covered.add(q['qid'])
            if q.get('role')=='schema_count':issues.append(dict(record=i,type='schema_count_used_as_bound',qid=q['qid']))
            if q['direction'] and q['direction']!=row['cmp']:issues.append(dict(record=i,type='comparison_cue',qid=q['qid']))
            if q['direct_var'] is not None and sum(abs(a)>1e-9 for a in row['a'])==1 and abs(row['a'][q['direct_var']])<1e-9:
                issues.append(dict(record=i,type='variable_binding',qid=q['qid']))
            if sum(abs(a)>1e-9 for a in row['a'])==1 and resource_pair(q,qs):issues.append(dict(record=i,type='resource_requires_aggregate_row',qid=q['qid'],source_coefficients=resource_pair(q,qs)))
            if sum(abs(a)>1e-9 for a in row['a'])==2 and abs(row['b'])>1e-9:
                for var,a in enumerate(row['a']):
                    matches=[c for c in qs if abs(c['value']-abs(a))<1e-7 and c['var_hint'] in [None,var]]
                    if matches and q['metric'] and all(c['metric'] and (c['metric'][0]!=q['metric'][0] or (c['metric'][1] and q['metric'][1] and c['metric'][1]!=q['metric'][1])) for c in matches):
                        issues.append(dict(record=i,type='resource_binding',qid=q['qid'],variable=var))
        if abs(row['b'])<1e-9:
            for q in qs:
                if q['cid']!=row['cid']:continue
                for kind in ['ratio','multiple','difference']:
                    for target in [0,1]:
                        possible,_=compile_records([dict(kind=kind,cmp=row['cmp'],target=target,rhs=q['qid'],terms=[])],case,checks=False)
                        if possible and same_row(possible[0],row):covered.add(q['qid'])
    for q in qs:
        if q['direction'] and q['qid'] not in covered:issues.append(dict(qid=q['qid'],type='uncovered_boundary',surface=q['surface'],cid=q['cid']))
    return issues
