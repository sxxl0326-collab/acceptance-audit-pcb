"""New contracts over the real PCB/SQLite backend; all strong execution controls kept."""
import sys,json,copy,itertools
from pathlib import Path
from benchmark import ROOT,load,dump,digest,sha,feasible
sys.path.insert(0,str(ROOT/'backend'))
from runtime import SimulationCache,Session
from simulator import DEFAULT,simulate

def unit_rules(case):
    import re,ledger
    out=[]
    for q in ledger.inventory(case):
        if q['direction'] is None:continue
        text=next(s['text'] for s in case['sentences'] if s['cid']==q['cid']);tail=text[q['span'][1]:]
        var=q['var_hint']
        if var is None:
            if re.match(r'\s*boards?\s+per\s+minute',tail,re.I):var=0
            elif re.match(r'\s*seconds?',tail,re.I):var=1
        if var is None:continue
        a=[0.,0.];a[var]=1.;out.append(dict(a=a,b=q['value'],cmp=q['direction'],cid=q['cid']))
    return out

class RowCache:
    def __init__(self,base,rows):
        self.rows_def=copy.deepcopy(rows);self.identity=digest(dict(base=base.identity,linear_contract=rows))
        self.table=copy.deepcopy(base.table);self.rows={}
        for row in self.table:
            metrics=row['metrics'];x=[metrics['throughput'],metrics['cycle_time']]
            for i,r in enumerate(rows):metrics[f'contract_lhs_{i}']=sum(a*v for a,v in zip(r['a'],x))
            self.rows[(row['context'],digest(row['config']))]=row
    def lookup(self,config,context,identity):
        if identity!=self.identity:raise ValueError('contract/cache provenance mismatch')
        return self.rows[(context,digest(config))]
    def contracts(self):
        out=[]
        for i,r in enumerate(self.rows_def):
            for cmp in ['le','ge'] if r['cmp']=='eq' else [r['cmp']]:
                out.append(dict(metric=f'contract_lhs_{i}',cmp=cmp,amount=float(r['b']),calc='absolute',ref='absolute',unit='native'))
        return out

def actual_ok(base,config,context,gold,race):
    m=base.lookup(config,context,base.identity)['metrics']
    return feasible(gold,[m['throughput'],m['cycle_time']]) and (race!='boundary_revision' or m['inspection_time']>=3.5-1e-8)

def main():
    cases=load('engineering_cases.json');base=SimulationCache();dest=ROOT/'engineering_execution';dest.mkdir(exist_ok=True)
    if (dest/'provenance.json').exists() and (dest/'summary.json').exists():
        print('Complete engineering results already recorded; preserving SQLite effects.');return
    inference=json.loads((ROOT/'engineering_language/results.json').read_text(encoding='utf-8'))
    formula=json.loads((ROOT/'formulas_engineering/results.json').read_text(encoding='utf-8'))
    all_contracts={(r['model'],r['id'],r['method']):r['contract'] for r in inference}
    all_contracts.update({(r['model'],r['id'],r['method']):r['contract'] for r in formula if r['method'] in ['literal_full','parametric_full']})
    models=sorted({k[0] for k in all_contracts});assert len(models)==2
    methods=['numeric_full','numeric_feedback','literal_full','parametric_full','explicit_unit_rules','gold']
    races=['clean','boundary_revision','packing_slow','duplicate'];controllers=['global_cas','complete_readset','semantic_guard','compiled_contract','central_fresh']
    records=[];new_designs={}
    for case in cases:
      for model in models:
       for method in methods:
        rows=case['gold'] if method=='gold' else unit_rules(case) if method=='explicit_unit_rules' else all_contracts[model,case['id'],method]
        cache=RowCache(base,rows);contracts=cache.contracts()
        review=cache.lookup(DEFAULT,'nominal',cache.identity)['metrics']
        for race in races:
         for controller in controllers:
            op=case['id']+'/'+model+'/'+method+'/'+race+'/'+controller
            db=dest/(digest(op)[:16]+'.sqlite');assert not db.exists(),'Partial prior execution exists; preserve it and use a fresh output directory'
            session=Session(cache,contracts,review,db=db)
            proposal=session.prepare(op);session.perturb(race,op);before=session.state()
            feasible_gold=any(r['context']==before['context'] and actual_ok(base,r['config'],r['context'],case['gold'],race) for r in base.table)
            if proposal is None and controller=='central_fresh':
                proposal=dict(op_id=op,base_version=before['version'],read_versions=copy.deepcopy(before['versions']),patch={},certificate_identity=cache.identity)
            result=None if proposal is None else session.commit(proposal,controller)
            committed=result is not None and result['status']=='COMMITTED';after=session.state()
            valid=committed and not result['duplicate_before'] and actual_ok(base,after['config'],after['context'],case['gold'],race)
            row=dict(id=case['id'],model=model,method=method,race=race,controller=controller,
                status='NO_PROPOSAL' if result is None else result['status'],committed=int(committed),valid=int(valid),invalid=int(committed and not valid),
                source_feasible=int(feasible_gold),duplicate_new_effect=int(committed and result['duplicate_before']),
                config=after['config'],context=after['context'],database=db.name)
            records.append(row);session.close()
            if committed:new_designs[digest([after['config'],after['context']])]=dict(config=after['config'],context=after['context'])
    dump('engineering_execution/results.json',records)
    path=dest/'fresh_runs.jsonl';fresh={}
    if path.exists():fresh={r['key']:r for r in [json.loads(l) for l in path.read_text(encoding='utf-8').splitlines()]}
    for design in new_designs.values():
      for seed in [271031,271037]:
        key=digest([design,seed])
        if key not in fresh:
            r=dict(key=key,**design,**{'simulation':simulate(design['config'],design['context'],seed)})
            with path.open('a',encoding='utf-8') as f:f.write(json.dumps(r)+'\n')
            fresh[key]=r
    summary=[]
    for model,method,controller in itertools.product(models,methods,controllers):
        rs=[r for r in records if (r['model'],r['method'],r['controller'])==(model,method,controller)]
        summary.append(dict(model=model,method=method,controller=controller,n=len(rs),**{k:sum(r[k] for r in rs) for k in ['committed','valid','invalid','source_feasible','duplicate_new_effect']}))
    dump('engineering_execution/summary.json',summary)
    dump('engineering_execution/provenance.json',dict(actual_sqlite_scenarios=len(records),unique_committed_design_contexts=len(new_designs),fresh_simulations=len(fresh),fresh_seeds=[271031,271037],
        planning_table_sha256=sha(ROOT/'backend/simulation_table.json'),planning_simulator_sha256=sha(ROOT/'backend/simulator.py'),case_sha256=sha(ROOT/'engineering_cases.json')))
    print('Executed',len(records),'SQLite outcomes and',len(fresh),'fresh actual FactorySimPy simulations.')

if __name__=='__main__':main()
