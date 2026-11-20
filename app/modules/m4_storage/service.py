from .rules import storage,interpolate
def execute(ctx):
    rows=storage(ctx['levels'],ctx['curve']);ctx['storage']=rows
    ctx['stages'].append({'title':'库容换算','rows':rows,'metrics':{'换算测次':len(rows),'合并库':len(ctx['stations']),'最小库容 m³':str(min((x['volume'] for x in rows),default=0)),'最大库容 m³':str(max((x['volume'] for x in rows),default=0))}});return ctx

def level_to_volume(level,curve):return interpolate(level,curve)
