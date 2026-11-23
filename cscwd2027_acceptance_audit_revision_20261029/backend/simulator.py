"""PCB-line adapter over unmodified FactorySimPy components, fixed upstream commit.

Topology and stochastic service families adapted from upstream examples/usecase_PCB.py.
All calibration and design choices are research scenarios, not measurements of a factory.
"""
from __future__ import annotations
import contextlib, hashlib, itertools, json, math, os, random, sys, time
from pathlib import Path
import numpy as np
import simpy
ROOT=Path(__file__).resolve().parent
VENDOR=ROOT/'vendor/FactorySimPy'
sys.path.insert(0,str(VENDOR/'src'))
from factorysimpy.nodes.machine import Machine
from factorysimpy.nodes.source import Source
from factorysimpy.nodes.sink import Sink
from factorysimpy.edges.buffer import Buffer

UPSTREAM='1cc6ad89e01a277662e68d16ca91d4cc5ef956c2'
DEFAULT={'placer_time':6.,'inspection_time':2.5,'buffer_capacity':3}
GRID={'placer_time':[4.,5.,6.,7.,8.], 'inspection_time':[1.5,2.5,3.5], 'buffer_capacity':[1,3,6]}
CONTEXTS={'nominal':{'arrival_mean':2.,'reflow_mean':2.,'packing_mean':4.},
          'packing_slow':{'arrival_mean':2.,'reflow_mean':2.,'packing_mean':6.},
          'supply_slow':{'arrival_mean':5.,'reflow_mean':2.,'packing_mean':4.}}
T_END=1800.;WARMUP=300.
SEEDS=[1103,2207,3301]
TEST_SEEDS=[4409,5501,6607]

def dump(name,value):
    path=ROOT/name;path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
def load(name):return json.loads((ROOT/name).read_text(encoding='utf-8'))
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def digest(obj):return hashlib.sha256(json.dumps(obj,ensure_ascii=False,sort_keys=True).encode()).hexdigest()

class CompletionStats(dict):
    def __init__(self,sink,original,events):super().__init__(original);self.sink=sink;self.events=events
    def __setitem__(self,key,value):
        if key=='num_item_received' and value>self.get(key,0):
            item=self.sink.item_in_process
            self.events.append({'time':float(self.sink.env.now),'cycle_time':float(self.sink.env.now-item.timestamp_creation),'item_id':str(item.id)})
        super().__setitem__(key,value)

def simulate(config,context='nominal',seed=1103,horizon=T_END,warmup=WARMUP,trace=False):
    assert 0<=warmup<horizon
    if isinstance(context,str):ctx=CONTEXTS[context]
    else:ctx=context
    assert set(config)==set(DEFAULT)
    assert config['placer_time']>0 and config['inspection_time']>0 and type(config['buffer_capacity']) is int and config['buffer_capacity']>0
    env=simpy.Environment();rngs=[np.random.default_rng(np.random.SeedSequence([seed,i])) for i in range(7)]
    service=[lambda:float(rngs[0].exponential(.5)),lambda:float(rngs[1].triangular(1.,2.,3.)),
             lambda:float(rngs[2].uniform(.5,1.5)*config['placer_time']),
             lambda:max(.01,float(rngs[3].normal(ctx['reflow_mean'],.25))),
             lambda:float(rngs[4].exponential(config['inspection_time'])),
             lambda:float(rngs[5].uniform(.5,1.5)*ctx['packing_mean'])]
    ids=['board_loader','solder_printer','component_placer','reflow_oven','inspection','packing']
    source=Source(env,'source',inter_arrival_time=lambda:float(rngs[6].uniform(.5,1.5)*ctx['arrival_mean']),blocking=True)
    machines=[Machine(env,name,work_capacity=1,blocking=True,processing_delay=delay) for name,delay in zip(ids,service)]
    sink=Sink(env,'sink');events=[];sink.stats=CompletionStats(sink,sink.stats,events)
    nodes=[source]+machines+[sink]
    buffers=[Buffer(env,f'buffer_{i}',capacity=config['buffer_capacity'] if 0<i<6 else 1) for i in range(7)]
    with open(os.devnull,'w',encoding='utf-8') as silence,contextlib.redirect_stdout(silence):
        for i,b in enumerate(buffers):b.connect(nodes[i],nodes[i+1])
    warm={};samples=[]
    def monitor():
        while True:
            if env.now>=warmup:
                inside=sum(len(b.inbuiltstore.items)+len(b.inbuiltstore.ready_items) for b in buffers)+sum(m.num_workers for m in machines)
                samples.append(float(inside))
            yield env.timeout(1.)
    def mark_warmup():
        yield env.timeout(warmup)
        warm['received']=sink.stats['num_item_received']
    env.process(monitor());env.process(mark_warmup())
    start=time.perf_counter()
    with open(os.devnull,'w',encoding='utf-8') as silence,contextlib.redirect_stdout(silence):env.run(until=horizon)
    completed=[e for e in events if e['time']>=warmup]
    assert len(completed)==sink.stats['num_item_received']-warm['received']
    metrics={'throughput':len(completed)*60/(horizon-warmup),
             'cycle_time':float(np.mean([e['cycle_time'] for e in completed])) if completed else math.inf,
             'wip':float(np.mean(samples)),**config}
    # Count conservation using actual component counters, with no analytic throughput proxy.
    generated=source.stats['num_item_generated'];received=sink.stats['num_item_received']
    assert received<=generated and all(m.stats['num_item_discarded']==0 for m in machines)
    return {'config':config,'context':ctx,'seed':seed,'metrics':metrics,'completions':len(completed),
            'generated':generated,'received_total':received,'wall_seconds':time.perf_counter()-start,
            'events':events if trace else None,'horizon':horizon,'warmup':warmup}

def configurations():
    return [dict(zip(GRID,values)) for values in itertools.product(*GRID.values())]

def build_table():
    path=ROOT/'simulation_runs.jsonl';old={}
    if path.exists():
        for l in path.read_text(encoding='utf-8').splitlines():
            r=json.loads(l);assert r['key'] not in old;old[r['key']]=r
    total=len(CONTEXTS)*len(configurations())*len(SEEDS);done=0
    for context in CONTEXTS:
        for config in configurations():
            for seed in SEEDS:
                key=digest([config,context,seed,T_END,WARMUP])
                if key not in old:
                    r={'key':key,'context_name':context,**simulate(config,context,seed)}
                    with path.open('a',encoding='utf-8') as f:f.write(json.dumps(r)+'\n')
                    old[key]=r
                done+=1
                if done%45==0:print(f'FactorySimPy runs {done}/{total}',flush=True)
    table=[]
    for context in CONTEXTS:
        for config in configurations():
            rs=[old[digest([config,context,seed,T_END,WARMUP])] for seed in SEEDS]
            table.append({'config':config,'context':context,'metrics':{k:float(np.mean([r['metrics'][k] for r in rs])) for k in rs[0]['metrics']},'seeds':SEEDS})
    dump('simulation_table.json',table)
    dump('simulation_manifest.json',{'upstream_commit':UPSTREAM,'adapter_sha256':sha(__file__),
          'upstream_example_sha256':sha(VENDOR/'examples/usecase_PCB.py'),'simpy_version':simpy.__version__,
          'grid':GRID,'contexts':CONTEXTS,'seeds':SEEDS,'horizon':T_END,'warmup':WARMUP,
          'runs':len(old),'table_rows':len(table),'simulation_runs_sha256':sha(path)})
    return table

if __name__=='__main__':
    import argparse
    ap=argparse.ArgumentParser();ap.add_argument('--table',action='store_true');args=ap.parse_args()
    if args.table:build_table()
    else:
        result=simulate(dict(DEFAULT),trace=True);dump('calibration_trace.json',result)
        print(json.dumps({k:v for k,v in result.items() if k!='events'},indent=2))
