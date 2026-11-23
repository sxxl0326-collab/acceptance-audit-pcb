"""Typed acceptance clauses and deterministic evaluation; semantics remain fallible."""
from __future__ import annotations
import math,re
from copy import deepcopy

METRICS=['throughput','cycle_time','wip','placer_time','inspection_time','buffer_capacity']
ALIASES={'throughput':['吞吐量','产出','throughput'], 'cycle_time':['平均周期时间','周期时间','mean cycle time','cycle time','cycle-time'],
 'wip':['平均在制品数量','在制品数量','在制品','WIP','wip'],
 'placer_time':['贴片时间','placer time'], 'inspection_time':['检测时间','inspection dwell time','inspection time'],
 'buffer_capacity':['中间缓冲容量','缓冲容量','buffer capacity','buffer']}
OPS=['absolute','pct_of','increase_pct','decrease_pct','add','subtract']
UNITS=['native','seconds','minutes','milliseconds','boards_per_min','boards_per_hour','boards','percent']

def number_catalog(text):
    pattern=r'two point five|one and a half|\b(?:five|ten|two|one)\b|[-+]?\d+(?:\.\d+)?'
    return [dict(id=f'n{i}',text=m.group(),start=m.start(),end=m.end()) for i,m in enumerate(re.finditer(pattern,text,re.I))]

def literal_number(text):
    s=text.strip().lower().replace('％','%').replace(',','')
    words={'five':5.,'ten':10.,'two':2.,'one':1.,'two point five':2.5,
           'one and a half':1.5,'one and a half minutes':1.5,'five percent':5.,'a five percent':5.}
    if s in words:return words[s]
    match=re.fullmatch(r'([-+]?\d+(?:\.\d+)?)\s*(?:%|percent)?',s)
    if not match:raise ValueError('unsupported numeric literal: '+text)
    return float(match.group(1))

def unit_scale(metric,unit):
    if unit in ('native','boards','percent'):return 1.
    if metric in ('cycle_time','inspection_time','placer_time'):
        return {'seconds':1.,'minutes':60.,'milliseconds':.001}[unit]
    if metric=='throughput':return {'boards_per_min':1.,'boards_per_hour':1/60}[unit]
    raise ValueError('metric/unit mismatch')

def boundary(c,review,latest):
    amount=float(c['amount']);assert math.isfinite(amount)
    metric=c['metric'];op=c['calc']
    if op=='absolute':return amount*unit_scale(metric,c.get('unit','native'))
    assert c['ref'] in ('review','latest')
    base=(review if c['ref']=='review' else latest)[metric]
    if op=='pct_of':return base*amount/100
    if op=='increase_pct':return base*(1+amount/100)
    if op=='decrease_pct':return base*(1-amount/100)
    if op=='add':return base+amount*unit_scale(metric,c.get('unit','native'))
    if op=='subtract':return base-amount*unit_scale(metric,c.get('unit','native'))
    raise ValueError(op)

def holds(contracts,metrics,review,latest,tol=1e-7):
    return all((metrics[c['metric']]>=boundary(c,review,latest)-tol) if c['cmp']=='ge' else
               (metrics[c['metric']]<=boundary(c,review,latest)+tol) for c in contracts)

def compile_output(raw,text,mode,review,latest):
    assert isinstance(raw,dict) and isinstance(raw.get('constraints'),list)
    assert 0<len(raw['constraints'])<=8
    compiled=[]
    for item in raw['constraints']:
        assert item['metric'] in METRICS and item['cmp'] in ('ge','le')
        assert item['calc'] in OPS and item['ref'] in ('absolute','review','latest') and item['unit'] in UNITS
        c={k:item[k] for k in ['metric','cmp','calc','ref','unit']}
        if mode.startswith('span'):
            spans={s['id']:s for s in number_catalog(text)}
            assert item['number_id'] in spans
            span=spans[item['number_id']]
            assert text[span['start']:span['end']]==span['text']
        if mode in ('span_compiled',):c['amount']=literal_number(span['text'])
        else:
            assert type(item['amount']) in (float,int) and math.isfinite(item['amount'])
            c['amount']=float(item['amount'])
        if mode in ('direct_numeric','span_model_numeric'):
            assert type(item['bound']) in (float,int) and math.isfinite(item['bound'])
            c.update(calc='absolute',ref='absolute',unit='native',amount=float(item['bound']))
        if c['calc']=='absolute':c['ref']='absolute'
        else:assert c['ref']!='absolute'
        boundary(c,review,latest)
        compiled.append(c)
    return compiled

def equivalent(a,b,review,latest):
    if a['metric']!=b['metric'] or a['cmp']!=b['cmp']:return False
    # Test the symbolic function at independent reference perturbations, rather than matching one number.
    states=[(review,latest)]
    for key in ['review','latest']:
        r=deepcopy(review);l=deepcopy(latest)
        target=r if key=='review' else l
        target[a['metric']]=target[a['metric']]*1.17+1.3
        states.append((r,l))
    return all(math.isclose(boundary(a,r,l),boundary(b,r,l),abs_tol=1e-6,rel_tol=1e-7) for r,l in states)

def matching_counts(pred,gold,review,latest):
    matched=set();tp=0
    for p in pred:
        for j,g in enumerate(gold):
            if j not in matched and equivalent(p,g,review,latest):matched.add(j);tp+=1;break
    return dict(tp=tp,fp=len(pred)-tp,fn=len(gold)-tp,exact=int(tp==len(pred)==len(gold)))

def choose(table,contracts,context,review,latest):
    eligible=[r for r in table if r['context']==context and holds(contracts,r['metrics'],review,latest)]
    if not eligible:return None
    return min(eligible,key=lambda r:(-r['metrics']['throughput'],r['metrics']['cycle_time'],r['metrics']['wip'],
         abs(r['config']['placer_time']-6)+abs(r['config']['inspection_time']-2.5)+abs(r['config']['buffer_capacity']-3)))

def rule_parse(text):
    """Declared bilingual grammar control. Unsupported/ambiguous clauses abstain."""
    # Strip explicitly withdrawn historical requirement and statements explicitly excluding a hard condition.
    cleaned=text.replace('，',';').replace('。',';').replace('；',';')
    parts=[p.strip() for p in re.split(r'[;\n]|\.\s+(?=[A-Z])',cleaned) if p.strip()]
    parts=[p for p in parts if not re.search(r'旧.*撤销|withdraw.*old|^旧.*是|^old |不设.*硬|不是额外硬|不要把|别按|并非说',p,re.I)]
    out=[]
    for part in parts:
        if re.search(r'希望|目标是|target is|is a preference|期待|期望',part,re.I) and not re.search(r'底线|至少|最多|不低|不得|不能|cap|floor|accept|requires',part,re.I):continue
        metric_hits=[]
        for metric,aliases in ALIASES.items():
            hit=[part.lower().find(a.lower()) for a in aliases if a.lower() in part.lower()]
            if hit:metric_hits.append((min(hit),metric))
        if not metric_hits:continue
        if len(metric_hits)>1:
            # Split conjunctions only when both halves independently name a metric.
            pieces=re.split(r'\s+(?:and|while)\s+|同时|并且',part,flags=re.I)
            if len(pieces)>1:
                for p in pieces:out.extend(rule_parse(p))
                continue
            raise ValueError('multiple metrics in one unsupported clause')
        metric=metric_hits[0][1]
        lower=part.lower()
        if re.search(r'不是额外硬|不设.*硬|no.*hard',lower):continue
        ref='latest' if re.search(r'提交时|提交前最新|latest|commit.time',lower) else 'review' if re.search(r'评审|快照|review',lower) else 'absolute'
        numpattern=r'\d+(?:\.\d+)?|two point five|one and a half|five|ten|two|one'
        nums=list(re.finditer(numpattern,part,re.I))
        if not nums:raise ValueError('missing literal')
        # Preference followed by an explicit boundary: use the last binding quantity.
        # Statements of a nicer value after a semicolon were already split; the nicer-only fragment has no comparator.
        num=nums[-1];amount=literal_number(num.group());suffix=part[num.end():num.end()+30].lower()
        if re.search(r'更|nicer|target',suffix) and len(nums)>1:num=nums[0];amount=literal_number(num.group());suffix=part[num.end():num.end()+30].lower()
        unit='native'
        if re.search(r'毫秒|millisecond',suffix):unit='milliseconds'
        elif re.search(r'分钟|minutes?',suffix) and metric!='throughput':unit='minutes'
        elif re.search(r'块/小时|boards/hour',suffix):unit='boards_per_hour'
        elif re.search(r'秒|seconds?',suffix):unit='seconds'
        elif '%' in suffix or re.search(r'percent',suffix):unit='percent'
        ge=bool(re.search(r'至少|最低|下限|不低于|不能低于|不少于|不能少于|低于.*不|floor|at least|at or above|below|not be less|no fewer|not.*less',lower))
        le=bool(re.search(r'最多|上限|不超过|不得超过|不高于|不能多于|不能超过|不能突破|超过.*不|不接受.*超过|以内|cap|ceiling|up to|no more|no greater|at most|larger.*acceptable',lower))
        op='absolute'
        if ref!='absolute':
            if unit=='percent':
                if re.search(r'下降|少|loss|fall|drop|decrease',lower):op='decrease_pct';ge=True;le=False
                elif re.search(r'增加|increase',lower):op='increase_pct';le=True;ge=False
                else:op='pct_of'
            elif re.search(r'加|多|plus',lower):op='add';le=True
            elif re.search(r'少|minus',lower):op='subtract';ge=True;le=False
            else:raise ValueError('unsupported relative expression')
        if ge==le:raise ValueError('ambiguous or missing comparator')
        out.append(dict(metric=metric,cmp='ge' if ge else 'le',amount=amount,calc=op,ref=ref,unit=unit))
    if not out:raise ValueError('no supported binding clause')
    return out
