"""New SQLite replays under frozen, source-only request gates; no new inference."""
import copy,json,sys,hashlib,sqlite3,itertools,csv
from collections import Counter
from pathlib import Path
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parent
SCI=ROOT.parent/'cscwd2027_acceptance_audit_revision_20261029'
sys.path.insert(0,str(SCI));sys.path.insert(0,str(SCI/'backend'))
from engineering_execute import RowCache,unit_rules,actual_ok
from runtime import SimulationCache,Session
from simulator import DEFAULT
import ledger as L
import formulas as F
from run import safe_numeric
from benchmark import digest
import offline_regions as E

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def load(n):return json.loads((SCI/n).read_text(encoding='utf-8'))
def dump(n,v):
 p=ROOT/n;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(v,ensure_ascii=False,indent=2),encoding='utf-8')

def final_diagnostics(case,model,method,rows,raw):
    if method in ['gold','explicit_unit_rules']:return [],'not_applicable_no_model_review'
    kind='parametric' if method=='parametric_full' else 'literal' if method=='literal_full' else 'numeric'
    stage=kind+'_review' if kind!='numeric' else 'numeric_feedback' if method=='numeric_feedback' else 'numeric_review'
    r=raw[model+'/'+case['id']+'/'+stage];issues=[]
    if r.get('error'):issues.append(dict(type='response_failure',detail=r['error']))
    if kind in ['literal','parametric']:
        _,compile_issues=F.compile_formulas(r.get('parsed',{}).get('constraints',[]),case,literal=kind=='literal')
        issues += [v for v in compile_issues if v['type']!='uncovered_boundary']
    else:
        _,error=safe_numeric(r.get('parsed',{}),case)
        if error:issues.append(dict(type='schema_failure',detail=error))
    if kind=='parametric':
        covered={q for row in rows for q in row.get('source_qids',[])}|{row['qid'] for row in rows if 'qid' in row}
        for q in L.inventory(case):
            if q['direction'] and q['qid'] not in covered:issues.append(dict(type='uncovered_boundary',qid=q['qid'],cid=q['cid'],surface=q['surface']))
    else:issues+=L.numeric_diagnostics(rows,case)
    return issues,'post_review_source_only'

def decision(policy,rows,issues):
    if policy=='empty_defer' and not rows:return 'DEFER_EMPTY_REQUIRED_CONTRACT'
    if policy=='warning_defer' and issues:return 'DEFER_UNRESOLVED_DIAGNOSTICS'
    return None

def gated_commit(session,proposal,controller,reason):
    if not reason:return session.commit(proposal,controller)
    session.db.execute('BEGIN IMMEDIATE')
    try:
        before=session.state();prior=session.db.execute('SELECT1FROM operations WHERE op_id=?'.replace('SELECT1FROM','SELECT 1 FROM'),(proposal['op_id'],)).fetchone() is not None
        status='ALREADY_APPLIED' if prior else reason
        after=session.state();session.db.execute('COMMIT')
        assert before==after
        return dict(status=status,before=before,after=after,duplicate_before=prior,candidate=None)
    except BaseException:session.db.execute('ROLLBACK');raise

def outcome(r):
    if r['committed']:return 'valid_commit' if r['valid'] else 'invalid_commit'
    if r['status'].startswith('DEFER_'):return 'deferred'
    if r['status']=='ALREADY_APPLIED':return 'idempotent_return'
    if r['status'] in ['VERSION_CONFLICT','DEPENDENCY_CONFLICT']:return 'version_dependency_rejection'
    if r['status']=='ACCEPTANCE_REVALIDATION_FAILED':return 'acceptance_rejection'
    if r['status'] in ['NO_PROPOSAL','NO_FEASIBLE_GRID_DESIGN']:return 'no_design'
    return 'other_failure'

def main():
    dest=ROOT/'policy_replay';dest.mkdir(exist_ok=True);dbdir=dest/'databases';dbdir.mkdir(exist_ok=True)
    sources=['engineering_cases.json','engineering_language/results.json','formulas_engineering/results.json','engineering_execution/results.json','backend/runtime.py','backend/contracts.py','backend/simulation_table.json','formulas.py','ledger.py']
    frozen=dict(protocol_sha256=sha(ROOT/'POLICY_PROTOCOL.md'),runner_sha256=sha(Path(__file__)),source_hashes={n:sha(SCI/n) for n in sources},utc=datetime.now(timezone.utc).isoformat())
    freeze=dest/'frozen.json'
    if freeze.exists():
        prior=json.loads(freeze.read_text());assert all(prior[k]==frozen[k] for k in ['protocol_sha256','runner_sha256','source_hashes'])
    else:freeze.write_text(json.dumps(frozen,indent=2),encoding='utf-8')
    cases=load('engineering_cases.json');base=SimulationCache()
    raw={r['key']:r for n in ['engineering_language','formulas_engineering'] for r in [json.loads(s) for s in (SCI/n/'raw.jsonl').read_text(encoding='utf-8').splitlines()]}
    scored=load('engineering_language/results.json')+load('formulas_engineering/results.json')
    contracts={(r['model'],r['id'],r['method']):r['contract'] for r in scored}
    old={(r['id'],r['model'],r['method'],r['race'],r['controller']):r for r in load('engineering_execution/results.json')}
    models=['qwen3.5:4b','qwen3.5:9b'];methods=['numeric_full','numeric_feedback','literal_full','parametric_full','explicit_unit_rules','gold']
    policies=['advisory','empty_defer','warning_defer'];controllers=['global_cas','complete_readset','semantic_guard','compiled_contract','central_fresh']
    log=dest/'records.jsonl';records=[];done={}
    if log.exists():
        for line in log.read_text(encoding='utf-8').splitlines():
            r=json.loads(line);assert r['key'] not in done;done[r['key']]=r;records.append(r)
    contract_flags=[]
    for case,model,method in itertools.product(cases,models,methods):
        rows=case['gold'] if method=='gold' else unit_rules(case) if method=='explicit_unit_rules' else contracts[model,case['id'],method]
        cache=RowCache(base,rows);issues,scope=final_diagnostics(case,model,method,rows,raw)
        contract_flags.append(dict(id=case['id'],model=model,method=method,contract=rows,final_diagnostics=issues,diagnostic_scope=scope))
        equal=E.check(rows,case['gold'])['region_equal']
        review=cache.lookup(DEFAULT,'nominal',cache.identity)['metrics']
        for race,controller,policy in itertools.product(['clean','boundary_revision','packing_slow','duplicate'],controllers,policies):
            original=old[case['id'],model,method,race,controller];op='/'.join([case['id'],model,method,race,controller]);key=policy+'/'+op
            if key in done:continue
            db=dbdir/(digest(key)[:24]+'.sqlite');assert not db.exists(),'Preserve orphan DB for forensic recovery: '+str(db)
            session=Session(cache,cache.contracts(),review,db=db);proposal=session.prepare(op);session.perturb(race,op);before=session.state()
            if proposal is None and controller=='central_fresh':proposal=dict(op_id=op,base_version=before['version'],read_versions=copy.deepcopy(before['versions']),patch={},certificate_identity=cache.identity)
            reason=decision(policy,rows,issues)
            result=None if proposal is None else gated_commit(session,proposal,controller,reason)
            committed=bool(result and result['status']=='COMMITTED');after=session.state();status='NO_PROPOSAL' if result is None else result['status']
            valid=committed and not result['duplicate_before'] and actual_ok(base,after['config'],after['context'],case['gold'],race)
            r=dict(key=key,id=case['id'],model=model,method=method,race=race,controller=controller,policy=policy,status=status,committed=int(committed),valid=int(valid),invalid=int(committed and not valid),
                config=after['config'],context=after['context'],database=db.name,contract_empty=not rows,final_diagnostic_count=len(issues),formal_reference_equal=equal,
                source_feasible=original['source_feasible'],duplicate_new_effect=int(committed and result['duplicate_before']),
                matched_advisory_status=original['status'],matched_advisory_valid=original['valid'],matched_advisory_invalid=original['invalid'])
            r['destination']=outcome(r);r['deferred_valid_advisory_commit']=int(r['destination']=='deferred' and original['valid'])
            r['deferred_reference_equivalent']=int(r['destination']=='deferred' and equal)
            if r['destination']=='deferred':assert after==before and session.db.execute('SELECT count(*) FROM operations').fetchone()[0]==0
            session.close()
            # Read through a new connection, independently of the Session object.
            with sqlite3.connect('file:'+db.as_posix()+'?mode=ro',uri=True) as connection:
                saved=json.loads(connection.execute('SELECT state FROM project WHERE id=1').fetchone()[0]);effects=connection.execute("SELECT op_id,status,config FROM operations").fetchall()
            assert saved==after and sum(v[1]=='COMMITTED' for v in effects)==int(committed)
            if policy=='advisory':
                assert all(r[k]==original[k] for k in ['status','committed','valid','invalid','config','context','duplicate_new_effect'])
            if r['destination']=='deferred':assert not effects
            with log.open('a',encoding='utf-8') as f:f.write(json.dumps(r,ensure_ascii=False)+'\n')
            records.append(r);done[key]=r
        print('Replay',case['id'],model,method,'records',len(records),flush=True)
    assert len(records)==8640 and sum(r['duplicate_new_effect'] for r in records)==0
    dump('policy_replay/contract_diagnostics.json',contract_flags)
    summaries=[]
    for method,controller,policy in itertools.product(methods,controllers,policies):
        rs=[r for r in records if (r['method'],r['controller'],r['policy'])==(method,controller,policy)];assert len(rs)==96
        counts=Counter(r['destination'] for r in rs);nondup=[r for r in rs if r['race']!='duplicate'];assert len(nondup)==72
        summaries.append(dict(method=method,controller=controller,policy=policy,n=96,nonduplicate_n=72,**{k:counts[k] for k in ['valid_commit','invalid_commit','deferred','idempotent_return','version_dependency_rejection','acceptance_rejection','no_design','other_failure']},
            valid_advisory_commits_blocked=sum(r['deferred_valid_advisory_commit'] for r in rs),reference_equivalent_contracts_deferred=sum(r['deferred_reference_equivalent'] for r in rs),
            source_feasible_requests_deferred=sum(r['destination']=='deferred' and r['source_feasible'] for r in rs),new_effect_completion=sum(r['committed'] for r in nondup)/72,source_valid_new_effect_completion=sum(r['valid'] for r in nondup)/72,
            exact_status_counts=dict(Counter(r['status'] for r in rs))))
    dump('policy_replay/summary.json',summaries)
    with (dest/'summary.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(summaries[0]));w.writeheader();w.writerows({k:json.dumps(v) if isinstance(v,dict) else v for k,v in r.items()} for r in summaries)
    dump('policy_replay/verification.json',dict(actual_new_databases=8640,advisory_matches_original_scenarios=2880,all_states_independently_read_back=True,duplicate_new_effects=0,
        planning_observation_sha256=sha(SCI/'backend/simulation_table.json'),new_simulator_runs=0,new_model_calls=0,exploratory_reuse=True))
    print('Complete8640actualSQLite replays; original2880advisory outcomes reproduced.')

if __name__=='__main__':main()
