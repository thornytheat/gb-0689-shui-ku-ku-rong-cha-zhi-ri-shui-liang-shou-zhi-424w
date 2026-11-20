from app.normalize import number,instant
from app.errors import DomainError
from decimal import Decimal as D
from collections import defaultdict
from app.reasons import rejected
def validate_curve(points):
    if not isinstance(points,list) or len(points)<2:raise DomainError('CURVE','库容曲线至少两个点')
    try:curve=[(number(p[0]),number(p[1])) for p in points]
    except (TypeError,IndexError):raise DomainError('CURVE','曲线格式无效')
    if any(v<0 for _,v in curve) or any(b[0]<=a[0] or b[1]<=a[1] for a,b in zip(curve,curve[1:])):raise DomainError('CURVE','曲线必须严格递增')
    return curve
def normalize(row,curve,role='main',allow_missing_level=False,allow_missing_flow=False):
    """校核并归一单条测次。

    主站默认水位与流量均必填。当同库存在备站时，主站允许短时缺水位
    （由备站接续，流量仍保留），备站只有水位，流量可空。空值保留为
    None，绝不当作零。
    """
    out=dict(row);out['time']=instant(row['time'])
    backup=role=='backup'
    out['level']=number(row['level'],nullable=allow_missing_level)
    out['inflow']=number(row['inflow'],nullable=allow_missing_flow or backup)
    out['outflow']=number(row['outflow'],nullable=allow_missing_flow or backup)
    if row['flow_unit']=='L/s':
        if out['inflow'] is not None:out['inflow']/=1000
        if out['outflow'] is not None:out['outflow']/=1000
    elif row['flow_unit']!='m3/s':raise DomainError('UNIT','流量单位无效')
    if (out['inflow'] is not None and out['inflow']<0) or (out['outflow'] is not None and out['outflow']<0):raise DomainError('RANGE','流量或水位越界')
    if out['level'] is not None and not curve[0][0]<=out['level']<=curve[-1][0]:raise DomainError('RANGE','流量或水位越界')
    v=number(row['version'])
    if v<1 or v!=v.to_integral():raise DomainError('RANGE','测次版本无效')
    out['version']=int(v);out['flow_unit']='m3/s';out['role']=role;return out
def select(rows):
    groups=defaultdict(list)
    for row in rows:groups[(row['station'],row['time'])].append(row)
    good=[];bad=[]
    for key,group in sorted(groups.items()):
        latest=max(x['version'] for x in group);top=[x for x in group if x['version']==latest]
        sig=lambda x:(x['level'],x['inflow'],x['outflow'])
        if len({sig(x) for x in top})>1:bad.extend(rejected(x,'CONFLICT','同刻水文值矛盾') for x in group);continue
        winner=min(top,key=lambda x:x['_line']);good.append(winner);bad.extend(rejected(x,'DUPLICATE','重复或旧版测次') for x in group if x is not winner)
    return good,bad
