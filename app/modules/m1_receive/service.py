from .rules import receive
from app.merge import classify_station
def execute(ctx):
    ctx.update(receive(ctx['source']))
    overrides=(ctx.get('params') or {}).get('station_roles') or {}
    if not isinstance(overrides,dict):overrides={}
    for row in ctx['records']:
        reservoir,role=classify_station(row['station'],overrides)
        row['reservoir']=reservoir or row['station']
        row['role']=role
    ctx['stages'].append({'title':'测站采信','rows':ctx['records'],'metrics':{'输入':ctx['input_count'],'采信':len(ctx['records']),'隔离':len(ctx['quarantine'])}});return ctx
