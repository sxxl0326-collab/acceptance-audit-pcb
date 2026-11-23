"""Scoring-only MILP status audit. Does not change frozen inference or point scores."""
import numpy as np
from scipy.optimize import milp,LinearConstraint,Bounds
from benchmark import solve as original_solve,digest
import equivalence as core

events=[]
def solve(rows,objective):
    result=original_solve(rows,objective)
    if result['status']==4 and objective.get('integrality'):
        n=len(objective['coefficients']);matrix=[];lo=[];hi=[]
        for r in rows:
            matrix.append(r['a']);lo.append(r['b'] if r['cmp'] in ['ge','eq'] else -np.inf);hi.append(r['b'] if r['cmp'] in ['le','eq'] else np.inf)
        constraints=[] if not rows else [LinearConstraint(np.array(matrix,float),np.array(lo),np.array(hi))]
        c=np.array(objective['coefficients'],float)*(-1 if objective['sense']=='maximize' else 1)
        fallback=milp(c,integrality=np.array(objective['integrality']),bounds=Bounds(np.zeros(n),np.full(n,np.inf)),constraints=constraints,options={'presolve':False,'time_limit':10})
        result=dict(status=int(fallback.status),x=None if fallback.x is None else fallback.x.tolist(),value=None if fallback.x is None else float(np.dot(fallback.x,objective['coefficients'])))
        events.append(dict(program_hash=digest([rows,objective]),original_status=4,fallback_status=result['status'],fallback='same MILP, presolve disabled',message=str(fallback.message)))
    if result['status'] not in [0,2,3]:raise RuntimeError('region_solver_status_'+str(result['status']))
    return result

core.solve=solve
check=core.check
implied=core.implied
