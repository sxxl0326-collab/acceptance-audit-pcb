"""Safe source-parameter arithmetic IR. Parses a restricted AST; never executes code."""
import ast,re
import numpy as np
import ledger as L

SCHEMA={'type':'object','properties':{'constraints':{'type':'array','maxItems':12,'items':{'type':'object','properties':{
    'formula':{'type':'string'},'cid':{'type':'string'}},'required':['formula','cid'],'additionalProperties':False}}},
    'required':['constraints'],'additionalProperties':False}

def symbols(case):
    out=[]
    for q in L.inventory(case):
        name=case['variables'][q['var_hint']] if q['var_hint'] is not None else 'limit'
        if q['metric']:name+='_'+(q['metric'][1] or q['metric'][0])
        if q['percent']:name+='_fraction'
        name=re.sub(r'[^a-zA-Z0-9]+','_',name).strip('_')[:45] or 'quantity'
        out.append(dict(q,symbol='q_'+name+'_'+q['qid']))
    return out

def scalar(v=0.,deps=None):return dict(a=np.zeros(2),b=float(v),co=[set(),set()],const=set(deps or []))
def all_deps(z):return z['co'][0]|z['co'][1]|z['const']
def add(x,y,sign=1):return dict(a=x['a']+sign*y['a'],b=x['b']+sign*y['b'],co=[x['co'][i]|y['co'][i] for i in [0,1]],const=x['const']|y['const'])
def scale(z,v,extra=None):
    extra=set(extra or []);return dict(a=z['a']*v,b=z['b']*v,
        co=[z['co'][i]|extra if abs(z['a'][i])>1e-12 else set() for i in [0,1]],const=z['const']|extra if abs(z['b'])>1e-12 else set())

def parse(node,qs,literal):
    if isinstance(node,ast.Constant):
        assert isinstance(node.value,(int,float)) and (literal or node.value in [0,1]),'free coefficient: use a source symbol'
        return scalar(node.value)
    if isinstance(node,ast.Name):
        if node.id in ['x0','x1']:
            z=scalar();z['a'][int(node.id[1])]=1.;return z
        assert node.id in qs,'unknown source symbol'
        q=qs[node.id];return scalar(q['value'],[q['qid']])
    if isinstance(node,ast.UnaryOp) and isinstance(node.op,(ast.USub,ast.UAdd)):
        return scale(parse(node.operand,qs,literal),-1 if isinstance(node.op,ast.USub) else 1)
    assert isinstance(node,ast.BinOp),'only affine arithmetic is supported'
    x,y=parse(node.left,qs,literal),parse(node.right,qs,literal)
    if isinstance(node.op,ast.Add):return add(x,y)
    if isinstance(node.op,ast.Sub):return add(x,y,-1)
    if isinstance(node.op,ast.Mult):
        if np.any(x['a']):
            assert not np.any(y['a']),'nonlinear product';return scale(x,y['b'],all_deps(y))
        assert not np.any(x['a']);return scale(y,x['b'],all_deps(x))
    if isinstance(node.op,ast.Div):
        assert not np.any(y['a']) and abs(y['b'])>1e-12,'nonconstant or zero divisor'
        return scale(x,1/y['b'],all_deps(y))
    raise ValueError('unsupported arithmetic')

def compile_formulas(items,case,literal=False,checks=True):
    catalogue=symbols(case);qs={q['symbol']:q for q in catalogue};byid={q['qid']:q for q in catalogue}
    cids={s['cid'] for s in case['sentences']};rows=[];issues=[];covered=set()
    for i,item in enumerate(items):
      try:
        assert item['cid'] in cids,'unknown source sentence'
        tree=ast.parse(item['formula'],mode='eval').body
        assert isinstance(tree,ast.Compare) and len(tree.ops)==len(tree.comparators)==1,'one comparison per row'
        op=tree.ops[0];cmp='le' if isinstance(op,ast.LtE) else 'ge' if isinstance(op,ast.GtE) else 'eq' if isinstance(op,ast.Eq) else None
        assert cmp is not None,'use <=, >= or =='
        z=add(parse(tree.left,qs,literal),parse(tree.comparators[0],qs,literal),-1)
        a=z['a'];b=-z['b'];deps=all_deps(z)
        if checks and not literal:
            for qid in z['const']:
                q=byid[qid]
                assert q['role']!='schema_count','schema count used as limit'
                if sum(abs(v)>1e-9 for v in a)==1 and L.resource_pair(q,catalogue):raise ValueError('resource capacity requires aggregate use')
            for var in [0,1]:
              for qid in z['co'][var]:
                q=byid[qid]
                if q['percent'] or q['multiple']:continue
                assert q['var_hint'] is None or q['var_hint']==var,'coefficient attached to another variable'
                for boundid in z['const']:
                    bound=byid[boundid]
                    if q['metric'] and bound['metric']:
                        cm,bm=q['metric'],bound['metric']
                        assert cm[0]==bm[0],'resource dimension mismatch'
                        assert not(cm[1] and bm[1] and cm[1]!=bm[1]),'resource name mismatch'
            for qid in deps:
                q=byid[qid]
                if q['direction'] and not q['percent'] and not q['multiple'] and qid not in z['const']:
                    issues.append(dict(record=i,type='boundary_used_as_coefficient',qid=qid))
        row=dict(a=a.tolist(),b=float(b),cmp=cmp,cid=item['cid'],source_qids=sorted(deps),formula=item['formula'])
        rows.append(row);covered|=deps
      except Exception as e:issues.append(dict(record=i,type='formula_binding',detail=str(e)))
    if not literal:
        for q in catalogue:
            if q['direction'] and q['qid'] not in covered:issues.append(dict(type='uncovered_boundary',qid=q['qid'],surface=q['surface'],cid=q['cid']))
    return rows,issues
