"""Offline region-inclusion checks, independent of language feedback."""
import math
from benchmark import solve

def implied(source,row,integrality=None):
    if row['cmp']=='eq':
        return implied(source,dict(row,cmp='le'),integrality) and implied(source,dict(row,cmp='ge'),integrality)
    coeff=list(row['a']) if row['cmp']=='le' else [-v for v in row['a']]
    rhs=row['b'] if row['cmp']=='le' else -row['b']
    objective=dict(coefficients=coeff,sense='maximize')
    if integrality:objective['integrality']=integrality
    out=solve(source,objective)
    if out['status']==2:return True
    if out['status']!=0:return False
    return out['value']<=rhs+1e-5*max(1,abs(rhs),abs(out['value']))

def check(candidate,reference,integrality=None):
    feasibility=dict(coefficients=[0.,0.],sense='minimize')
    if integrality:feasibility['integrality']=integrality
    c=solve(candidate,feasibility);g=solve(reference,feasibility)
    if c['status']!=0 or g['status']!=0:
        return dict(region_equal=False,missing_reference_rows=None,spurious_candidate_rows=None,candidate_status=c['status'])
    missing=sum(not implied(candidate,r,integrality) for r in reference)
    spurious=sum(not implied(reference,r,integrality) for r in candidate)
    return dict(region_equal=not missing and not spurious,missing_reference_rows=missing,spurious_candidate_rows=spurious,candidate_status=c['status'])
