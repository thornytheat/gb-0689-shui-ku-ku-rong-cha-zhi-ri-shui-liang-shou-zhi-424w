from .rules import validate_curve,normalize,select
from app.normalize import number
from app.merge import merge
from app.errors import DomainError
from app.reasons import rejected
def execute(ctx):
    curve=validate_curve(ctx['params']['curve']);gap=number(ctx['params']['max_gap_hours']);tol=number(ctx['params']['tolerance_m3'])
    if gap<=0 or gap>24 or tol<0:raise DomainError('RANGE','缺口或容差参数无效')
    spike=number(ctx['params'].get('spike_m','0.15'))
    if spike<=0:raise DomainError('RANGE','气泡阈值必须为正')
    reservoirs_with_backup={row['reservoir'] for row in ctx['records'] if row.get('role')=='backup'}
    # 只报水位、从不报流量的站线可能是未登记备站；只有存在另一条确实报
    # 流量的站线时才放宽，否则单站文件仍按原契约把缺水位/缺流量隔离。
    stations={row['station'] for row in ctx['records']}
    flowed=set()
    for row in ctx['records']:
        try:
            number(row.get('inflow'));number(row.get('outflow'))
        except DomainError:continue
        flowed.add(row['station'])
    may_have_backup=len(stations)>1 and bool(stations-flowed) and bool(flowed)
    good=[]
    for row in ctx['records']:
        role=row.get('role') or 'main'
        allow_level=role=='main' and (row['reservoir'] in reservoirs_with_backup or may_have_backup)
        allow_flow=role=='backup' or (may_have_backup and row['station'] not in flowed)
        try:good.append(normalize(row,curve,role=role,allow_missing_level=allow_level,allow_missing_flow=allow_flow))
        except DomainError as exc:ctx['quarantine'].append(rejected(row,exc.code,exc.message))
    watermarks={}
    for row in good:
        old=watermarks.get(row['station'])
        row['arrival']='迟到' if old is not None and row['time']<old else '顺序'
        watermarks[row['station']]=max(old,row['time']) if old else row['time']
    good,bad=select(good);ctx['quarantine'].extend(bad);ctx['records']=good;ctx['curve']=curve
    merged=merge(good,curve,gap,spike,ctx['params'].get('station_roles'))
    ctx['quarantine'].extend(merged['quarantine'])
    ctx['records']=merged['records'];ctx['levels']=merged['levels'];ctx['stations']=merged['stations']
    ctx['stages'].append({'title':'曲线校核','rows':[{'level':h,'volume':v} for h,v in curve],'metrics':{'曲线点':len(curve),'有效测次':len(good),'最大缺口 h':str(gap),'气泡阈值 m':str(spike),'合并库':len(ctx['stations'])}});return ctx
