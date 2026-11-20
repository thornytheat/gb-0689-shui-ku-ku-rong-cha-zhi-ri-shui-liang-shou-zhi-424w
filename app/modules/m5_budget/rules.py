from datetime import datetime,timedelta,timezone
from decimal import Decimal as D
from collections import defaultdict
# Public pure interpolation is deliberately passed in by the service.
def day_budget(flows,curve,interpolate,levels=None,stations=None):
    """按水库汇总一条日账，并保留每段水位/流量的采用依据。

    flows 只来自主站（备站无流量）。日界处先线性插出流量与水位，再分别
    积分和按曲线换算库容，因此跨日库容变化可加、不会在日界跳变。
    每个日账附带 level_sources / flow_sources / segments，交班时可回查
    哪一段用了主站、哪一段由备站接续、偏差与锚点是什么。
    """
    del levels  # 端点水位已带在 part['left']/part['right'] 上，保持守恒。
    zone=timezone(timedelta(hours=8))
    buckets=defaultdict(lambda:{'in_m3':D(0),'out_m3':D(0),'storage_change':D(0),'coverage_hours':D(0),'segments':[]})
    for part in flows:
        left,right=part['left'],part['right'];start,end=part['start'],part['end'];duration=D(str((end-start).total_seconds()));cursor=start
        reservoir=part.get('reservoir',part.get('station'))
        def at(t,key):return left[key]+(right[key]-left[key])*D(str((t-start).total_seconds()))/duration
        while cursor<end:
            local=cursor.astimezone(zone);midnight=datetime.combine(local.date()+timedelta(days=1),datetime.min.time(),zone);stop=min(end,midnight);seconds=D(str((stop-cursor).total_seconds()))
            b=buckets[(reservoir,local.date().isoformat())]
            in_cut=(at(cursor,'inflow')+at(stop,'inflow'))/2*seconds
            out_cut=(at(cursor,'outflow')+at(stop,'outflow'))/2*seconds
            change=interpolate(at(stop,'level'),curve)-interpolate(at(cursor,'level'),curve)
            b['in_m3']+=in_cut;b['out_m3']+=out_cut;b['storage_change']+=change;b['coverage_hours']+=seconds/3600
            b['segments'].append(_segment(part,reservoir,cursor,stop,seconds,in_cut,out_cut,change))
            cursor=stop
    result=[]
    for (reservoir,day),values in sorted(buckets.items()):
        segments=values.pop('segments')
        level_sources={}
        flow_sources={}
        for seg in segments:
            level_sources[seg['level_source']]=level_sources.get(seg['level_source'],D(0))+seg['hours']
            flow_sources[seg['flow_source']]=flow_sources.get(seg['flow_source'],D(0))+seg['hours']
        dayinfo=(stations or {}).get(reservoir,{})
        result.append({'reservoir':reservoir,'station':reservoir,'date':day,**values,
                       'residual':values['storage_change']-values['in_m3']+values['out_m3'],
                       'state':'完整' if values['coverage_hours']==24 else '缺测',
                       'level_sources':level_sources,'flow_sources':flow_sources,
                       'wind_bias_m':dayinfo.get('wind_bias_m'),
                       'segments':segments})
    return result

def _segment(part,reservoir,cursor,stop,seconds,in_cut,out_cut,change):
    left,right=part['left'],part['right']
    # 段内换源是可能的：接续点两侧来源不同，按左右端点列出采用依据。
    uses_backup=any(row.get('level_source')=='备站' for row in (left,right))
    basis=[]
    for row,label in ((left,'start'),(right,'end')):
        info=row.get('level_basis') or {}
        basis.append({'end':label,'level_source':row.get('level_source'),
                      'level_method':info.get('method'),'offset_m':info.get('offset_m'),
                      'wind_bias_m':info.get('wind_bias_m'),
                      'anchor_start_line':info.get('anchor_start_line'),
                      'anchor_end_line':info.get('anchor_end_line'),
                      'flow_source':row.get('flow_source')})
    return {'reservoir':reservoir,'start':cursor,'end':stop,'hours':seconds/3600,
            'station':part.get('station'),'level_source':'备站' if uses_backup else '主站',
            'flow_source':'主站','in_m3':in_cut,'out_m3':out_cut,'storage_change':change,
            'basis':basis,'detail':_detail(basis)}

def _detail(basis):
    """把每段两端的水位/流量采用依据压成一行可回查文本。"""
    parts=[]
    for end in basis:
        if end['level_source']=='备站' and end['level_method'] not in ('shadowed','not_used'):
            parts.append('%s水位取备站(%s,偏差%s m,锚点行%s→%s)'%(end['end'],end['level_method'],end['offset_m'],end['anchor_start_line'],end['anchor_end_line']))
        else:
            parts.append('%s水位主站(%s)'%(end['end'],end['level_method'] or end['level_source']))
        parts.append('%s流量%s'%(end['end'],end['flow_source'] or '缺测'))
    return ' · '.join(parts)
