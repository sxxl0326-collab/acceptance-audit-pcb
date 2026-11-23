"""Transactional design execution using SQLite and actual FactorySimPy result cache.

Expensive simulations happen before a transaction. At effect time the authoritative
config/context and acceptance registry are read atomically, and the matching cached
result is validated. This is a single-process server integration, not consensus.
"""
from __future__ import annotations
from copy import deepcopy
import json,sqlite3,time
from simulator import DEFAULT,load,dump,digest,sha,ROOT
from contracts import choose,holds,boundary

CONTROLLERS=['unsafe','global_cas','design_readset','complete_readset','semantic_guard','compiled_contract','central_fresh']
RACES=['clean','metadata','packing_slow','unwritten_buffer','overlap_revision','boundary_revision','boundary_relax','duplicate']

class SimulationCache:
    def __init__(self):
        self.table=load('simulation_table.json');self.manifest=load('simulation_manifest.json')
        assert self.manifest['adapter_sha256']==sha(ROOT/'simulator.py')
        self.identity=digest({k:self.manifest[k] for k in ['upstream_commit','adapter_sha256','seeds','horizon','warmup','grid','contexts']})
        self.rows={(r['context'],digest(r['config'])):r for r in self.table}
    def lookup(self,config,context,identity):
        if identity!=self.identity:raise ValueError('simulation certificate/model mismatch')
        return self.rows[(context,digest(config))]

class Session:
    def __init__(self,cache,contracts,review,db=':memory:'):
        self.cache=cache;self.review=deepcopy(review)
        self.db=sqlite3.connect(str(db),isolation_level=None,timeout=10.,check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS project (id INTEGER PRIMARY KEY,state TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS operations (attempt INTEGER PRIMARY KEY AUTOINCREMENT,op_id TEXT,status TEXT,config TEXT)')
        initial=dict(config=deepcopy(DEFAULT),context='nominal',accepted=deepcopy(contracts),
            version=0,versions={k:0 for k in ['placer_time','inspection_time','buffer_capacity','context','accepted','metadata']},metadata='')
        self.db.execute('INSERT OR IGNORE INTO project VALUES(1,?)',(json.dumps(initial),))
    def close(self):self.db.close()
    def state(self):return json.loads(self.db.execute('SELECT state FROM project WHERE id=1').fetchone()[0])
    def _store(self,state):self.db.execute('UPDATE project SET state=? WHERE id=1',(json.dumps(state),))
    def metrics(self,state):return self.cache.lookup(state['config'],state['context'],self.cache.identity)['metrics']
    def prepare(self,op_id):
        state=self.state();latest=self.metrics(state)
        row=choose(self.cache.table,state['accepted'],state['context'],self.review,latest)
        if row is None:return None
        return dict(op_id=op_id,base_version=state['version'],read_versions=deepcopy(state['versions']),
            patch={k:v for k,v in row['config'].items() if v!=state['config'][k]},
            original_config=deepcopy(row['config']),base_config=state['config'],certificate_identity=self.cache.identity)
    def perturb(self,kind,op_id):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            s=self.state();changed=[]
            if kind=='clean':pass
            elif kind=='metadata':s['metadata']='equipment engineer added a note';changed=['metadata']
            elif kind=='packing_slow':s['context']='packing_slow';changed=['context']
            elif kind=='unwritten_buffer':s['config']['buffer_capacity']=6;changed=['buffer_capacity']
            elif kind=='overlap_revision':
                s['config']['placer_time']=8.;changed=['placer_time','accepted']
                s['accepted'].append(dict(metric='placer_time',cmp='ge',amount=7.,calc='absolute',ref='absolute',unit='seconds'))
            elif kind=='boundary_revision':
                s['accepted'].append(dict(metric='inspection_time',cmp='ge',amount=3.5,calc='absolute',ref='absolute',unit='seconds'));changed=['accepted']
            elif kind=='boundary_relax':
                latest=self.metrics(s)
                s['accepted']=[dict(metric=c['metric'],cmp=c['cmp'],calc='absolute',ref='absolute',unit='native',
                    amount=boundary(c,self.review,latest)*(0.9 if c['cmp']=='ge' else 1.1)) for c in s['accepted']]
                changed=['accepted']
            elif kind=='duplicate':self.db.execute('INSERT INTO operations(op_id,status,config) VALUES(?,?,?)',(op_id,'PRIOR_EFFECT',json.dumps(s['config'])))
            else:raise ValueError(kind)
            if changed:
                s['version']+=1
                for key in changed:s['versions'][key]+=1
            self._store(s);self.db.execute('COMMIT')
            return deepcopy(s)
        except BaseException:self.db.execute('ROLLBACK');raise
    def commit(self,p,controller):
        start=time.perf_counter();self.db.execute('BEGIN IMMEDIATE')
        try:
            s=self.state();prior=self.db.execute('SELECT 1 FROM operations WHERE op_id=?',(p['op_id'],)).fetchone() is not None
            before=deepcopy(s);reason=None;candidate=None
            if controller!='unsafe' and prior:reason='ALREADY_APPLIED'
            if reason is None and controller=='global_cas' and s['version']!=p['base_version']:reason='VERSION_CONFLICT'
            keys=['placer_time','inspection_time','buffer_capacity','context']
            if controller=='complete_readset':keys+=['accepted']
            if reason is None and controller in ('design_readset','complete_readset'):
                if any(s['versions'][k]!=p['read_versions'][k] for k in keys):reason='DEPENDENCY_CONFLICT'
            latest=self.metrics(s)
            if reason is None:
                candidate={**s['config'],**p['patch']}
                if controller=='central_fresh':
                    row=choose(self.cache.table,s['accepted'],s['context'],self.review,latest)
                    candidate=None if row is None else deepcopy(row['config'])
                    if candidate is None:reason='NO_FEASIBLE_GRID_DESIGN'
                if candidate is not None:
                    certificate=self.cache.lookup(candidate,s['context'],p['certificate_identity'])
                    current_ok=holds(s['accepted'],certificate['metrics'],self.review,latest)
                    if controller in ('semantic_guard','compiled_contract','central_fresh') and not current_ok:reason='ACCEPTANCE_REVALIDATION_FAILED'
            if reason is None:
                assert candidate is not None
                old=deepcopy(s['config']);s['config']=candidate;s['version']+=1
                for k in old:
                    if old[k]!=candidate[k]:s['versions'][k]+=1
                self._store(s)
                self.db.execute('INSERT INTO operations(op_id,status,config) VALUES(?,?,?)',(p['op_id'],'COMMITTED',json.dumps(candidate)))
                reason='COMMITTED'
            after=deepcopy(s);self.db.execute('COMMIT')
            return dict(status=reason,before=before,after=after,duplicate_before=prior,
                elapsed_ms=(time.perf_counter()-start)*1000,candidate=candidate)
        except BaseException:self.db.execute('ROLLBACK');raise

def independent_eval(cache,contracts,review,before,after):
    """Scoring uses the actual applied config and accepted registry, not a verbal explanation."""
    latest=cache.lookup(before['config'],before['context'],cache.identity)['metrics']
    result=cache.lookup(after['config'],after['context'],cache.identity)['metrics']
    failures=[]
    for c in contracts:
        a=float(c['amount']);ref=review if c['ref']=='review' else latest
        scale=60. if c['unit']=='minutes' else .001 if c['unit']=='milliseconds' else 1/60 if c['unit']=='boards_per_hour' else 1.
        operation=c['calc']
        if operation=='absolute':b=a*scale
        elif operation=='pct_of':b=ref[c['metric']]*a/100.
        elif operation=='increase_pct':b=ref[c['metric']]*(100.+a)/100.
        elif operation=='decrease_pct':b=ref[c['metric']]*(100.-a)/100.
        elif operation=='add':b=ref[c['metric']]+a*scale
        else:b=ref[c['metric']]-a*scale
        value=result[c['metric']]
        ok=value>=b-1e-7 if c['cmp']=='ge' else value<=b+1e-7
        if not ok:failures.append(dict(metric=c['metric'],cmp=c['cmp'],bound=b,actual=value))
    return failures

def oracle_version_matrix():
    cache=SimulationCache();cases=[c for c in load('acceptance_cases.json') if c['split']=='holdout']
    review=next(r['metrics'] for r in cache.table if r['config']==DEFAULT and r['context']=='nominal')
    rows=[]
    for c in cases:
      for race in RACES:
       for controller in CONTROLLERS:
        session=Session(cache,c['gold'],review);p=session.prepare(c['id'])
        assert p is not None
        session.perturb(race,c['id']);authoritative=session.state();latest=session.metrics(authoritative)
        merged={**authoritative['config'],**p['patch']}
        frozen_valid=holds(authoritative['accepted'],cache.lookup(merged,authoritative['context'],cache.identity)['metrics'],review,latest)
        fresh=choose(cache.table,authoritative['accepted'],authoritative['context'],review,latest)
        outcome=session.commit(p,controller);committed=outcome['status']=='COMMITTED'
        failures=independent_eval(cache,authoritative['accepted'],review,outcome['before'],outcome['after']) if committed else []
        invalid=committed and (bool(failures) or outcome['duplicate_before'])
        rows.append(dict(id=c['id']+'/'+race,case=c['id'],race=race,controller=controller,status=outcome['status'],
          committed=int(committed),valid=int(committed and not invalid),invalid=int(invalid),
          duplicate=int(committed and outcome['duplicate_before']),frozen_candidate_valid=int(frozen_valid and race!='duplicate'),
          fresh_feasible=int(fresh is not None),benign_block=int(not committed and frozen_valid and race!='duplicate'),
          actual_config=outcome['after']['config'],context=authoritative['context'],
          accepted_after_background=authoritative['accepted'],failures=failures,elapsed_ms=outcome['elapsed_ms']))
        session.close()
    dump('version_results.json',rows)
    return rows

def summary(rows):
    result=[]
    for group in ['all']+RACES:
      for controller in CONTROLLERS:
        rs=[r for r in rows if r['controller']==controller and (group=='all' or r['race']==group)]
        result.append(dict(group=group,controller=controller,n=len(rs),
            **{k:sum(r[k] for r in rs) for k in ['committed','valid','invalid','duplicate','frozen_candidate_valid','fresh_feasible','benign_block']}))
    dump('version_summary.json',result)
    print(json.dumps([r for r in result if r['group']=='all'],indent=2))

if __name__=='__main__':summary(oracle_version_matrix())
