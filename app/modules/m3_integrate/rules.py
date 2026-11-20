from collections import defaultdict
from decimal import Decimal as D
def integrate(rows,max_gap):
    """按水库对主站流量做梯形积分。

    备站没有流量，不参与积分；主站缺流量或水位接续点没有流量时，该点
    不推进积分，前后间隔由 max_gap 判缺口，绝不按零补。按时间排序，
    迟到测次保留在事件时间上。
    """
    reservoirs=defaultdict(list)
    for row in rows:
        if row.get('role','main')!='main':continue
        if row.get('inflow') is None or row.get('outflow') is None:continue
        reservoirs[row.get('reservoir',row['station'])].append(row)
    parts=[];gaps=[]
    for reservoir,items in sorted(reservoirs.items()):
        items=sorted(items,key=lambda r:r['time'])
        for a,b in zip(items,items[1:]):
            seconds=D(str((b['time']-a['time']).total_seconds()))
            if seconds>max_gap*3600:
                gaps.append({'reservoir':reservoir,'station':a['station'],'start':a['time'],'end':b['time'],'hours':seconds/3600,'code':'GAP','state':'缺测'});continue
            parts.append({'reservoir':reservoir,'station':a['station'],'start':a['time'],'end':b['time'],
                          'in_m3':(a['inflow']+b['inflow'])/2*seconds,'out_m3':(a['outflow']+b['outflow'])/2*seconds,
                          'left':a,'right':b})
    return parts,gaps
