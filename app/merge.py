"""同库多站合并：主站水位流量 + 备站水位接续。

青岚库主站与库中备站观测同一座水库。备站只有水位，且受风壅影响与主站
存在几厘米的系统偏差；备站偶发气泡跳变。若把两站各自当成一座库，日账
会把同一天的库容变化算成两套；若把两站水位直接拼接，风壅偏差又会被算
成进出水。本模块把两站合成一条连续水位序列：

* 气泡跳变按相邻点线性内插残差识别并隔离，不参与接续；
* 主站与备站同库同时刻之差取中位数，作为风壅偏差证据；
* 主站有水位时只用主站水位，备站水位仅在主站缺测或断档时补点；
* 补点水位加“锚点偏差”：前后两个主站水位锚点处偏差等于主站减备站，
  两点之间线性渐变，因此接续点与主站水位重合，库容序列连续；
* 主站有流量但缺水位时，水位由备站接续、流量仍沿用主站，日账不断。

每个保留测次都带 level_raw、level_source 和 level_basis，日账逐段可回查
采用了哪一站、哪两个锚点以及偏差是多少。
"""
import re
from bisect import bisect_left
from collections import defaultdict
from decimal import Decimal as D
from app.reasons import rejected

ROLE_RE = re.compile(
    r"^(?P<prefix>.*?)[\s\-_（(]*?"
    r"(?P<role>库中备站|备用站|备站|副站|主站|备用|backup|standby|aux|main|备|主)"
    r"[\s\-_）)]*$",
    re.IGNORECASE,
)
ROLE_WORDS = {
    "库中备站": "backup", "备用站": "backup", "备站": "backup", "副站": "backup",
    "备用": "backup", "backup": "backup", "standby": "backup", "aux": "backup",
    "备": "backup",
    "主站": "main", "main": "main", "主": "main",
}
ROLE_ALIASES = {
    "backup": "backup", "standby": "backup", "second": "backup", "aux": "backup",
    "备": "backup", "备站": "backup", "库中备站": "backup", "副站": "backup",
    "main": "main", "primary": "main", "主": "main", "主站": "main",
}


def classify_station(name, overrides=None):
    """返回 (库名, 站别)；站别为 main 或 backup。

    站别名优先。否则识别常见站别后缀/括号标记：主站、备站、副站、
    库中备站、备用站、-备、_main、backup 等。没有站别标记的测站按主站
    处理，库名为空（调用方回退为站名）。

    overrides 支持三种写法，便于把无站别后缀的站名挂到同一座库：
    ``{"站名": "备站"}`` 只改站别；``{"站名": "青岚库/备站"}`` 或
    ``{"站名": {"reservoir": "青岚库", "role": "backup"}}`` 同时指定库名。
    """
    name = str(name).strip()
    match = ROLE_RE.match(name)
    reservoir = None
    role = None
    if match:
        candidate = match.group("prefix").strip().rstrip(" -_（(【[")
        token = match.group("role").lower()
        role = ROLE_WORDS.get(token) or ROLE_WORDS.get(match.group("role"), "main")
        if candidate:
            reservoir = candidate
    if isinstance(overrides, dict) and name in overrides:
        override = overrides[name]
        if isinstance(override, dict):
            if override.get("reservoir"):
                reservoir = str(override["reservoir"]).strip()
            if override.get("role"):
                role = ROLE_ALIASES.get(str(override["role"]).strip().lower(), role)
        else:
            text = str(override).strip()
            if "/" in text:
                left, right = text.split("/", 1)
                reservoir = left.strip() or reservoir
                text = right.strip()
            if text.lower() in ROLE_ALIASES:
                role = ROLE_ALIASES[text.lower()]
    return reservoir, role or "main"


def filter_spikes(rows, spike_m):
    """隔离备站气泡跳变。

    对每个内部点，用相邻保留点线性内插出期望水位；残差超过 spike_m，
    且高于两侧或低于两侧（乘积为正），并且与两侧的跳幅本身都超过阈值，
    才判为气泡跳变。这样常规涨落形成的真实峰谷不会被误删，而单点鼓包
    或凹坑能稳定识别。每轮只剔除残差最大的一个点，避免一个跳变把相邻
    正常点也带偏，直到稳定。
    """
    alive = sorted(rows, key=lambda r: r["time"])
    removed = []
    while len(alive) >= 3:
        worst = None
        for index in range(1, len(alive) - 1):
            row = alive[index]
            left, right = alive[index - 1], alive[index + 1]
            span = (right["time"] - left["time"]).total_seconds()
            if span <= 0:
                continue
            frac = D(str((row["time"] - left["time"]).total_seconds())) / D(str(span))
            expected = left["level"] + (right["level"] - left["level"]) * frac
            residual = row["level"] - expected
            left_jump = row["level"] - left["level"]
            right_jump = row["level"] - right["level"]
            if (abs(residual) > spike_m and left_jump * right_jump > 0
                    and abs(left_jump) > spike_m and abs(right_jump) > spike_m):
                if worst is None or abs(residual) > abs(worst[1]):
                    worst = (row, residual)
        if worst is None:
            break
        removed.append(worst)
        alive = [row for row in alive if row is not worst[0]]
    return alive, removed


def _median(values):
    values = sorted(values)
    if not values:
        return None
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2


def _interpolator(points, max_gap_hours):
    """按备站相邻观测线性内插水位；缺口超过 max_gap_hours 不插补。"""
    ordered = sorted(points, key=lambda r: r["time"])
    times = [row["time"] for row in ordered]
    limit = max_gap_hours * 3600

    def at(instant):
        index = bisect_left(times, instant)
        if index < len(times) and times[index] == instant:
            return ordered[index]["level"]
        if index == 0 or index >= len(times):
            return None
        left, right = ordered[index - 1], ordered[index]
        span = (right["time"] - left["time"]).total_seconds()
        if span > limit:
            return None
        frac = D(str((instant - left["time"]).total_seconds())) / D(str(span))
        return left["level"] + (right["level"] - left["level"]) * frac

    return at


def estimate_bias(main_rows, backup_rows, max_gap_hours):
    """估计主站减备站的风壅偏差（中位数）。返回 (偏差, 样本数, 方法)。"""
    main_levels = [row for row in main_rows if row.get("level") is not None]
    if not main_levels or not backup_rows:
        return None, 0, "none"
    at = _interpolator(backup_rows, max_gap_hours)
    diffs = []
    for row in main_levels:
        value = at(row["time"])
        if value is not None:
            diffs.append(row["level"] - value)
    if not diffs:
        return None, 0, "none"
    return _median(diffs), len(diffs), "median"


def _basis(method, offset, anchor_start=None, anchor_end=None, bias=None):
    return {
        "source": "备站",
        "method": method,
        "offset_m": offset,
        "anchor_start": anchor_start["time"] if anchor_start else None,
        "anchor_start_line": anchor_start.get("_line") if anchor_start else None,
        "anchor_end": anchor_end["time"] if anchor_end else None,
        "anchor_end_line": anchor_end.get("_line") if anchor_end else None,
        "wind_bias_m": bias,
    }


def _offset_at(instant, anchors, anchor_times, at, bias):
    """在前后主站水位锚点之间线性渐变接续偏差。

    锚点时刻的偏差等于主站水位减备站水位，因此接续水位与主站水位重合；
    两锚点之间偏差线性变化，库容不会在换源处跳变。返回
    (偏差, 方法, 前锚点, 后锚点)。
    """
    index = bisect_left(anchor_times, instant)
    prev = anchors[index - 1] if index > 0 else None
    nxt = anchors[index] if index < len(anchors) else None
    if nxt is not None and nxt["time"] == instant:
        prev = nxt
    off_prev = None
    off_next = None
    if prev is not None:
        value = at(prev["time"])
        if value is not None:
            off_prev = prev["level"] - value
    if nxt is not None:
        value = at(nxt["time"])
        if value is not None:
            off_next = nxt["level"] - value
    if off_prev is not None and off_next is not None:
        if nxt is prev:
            return off_prev, "anchor_exact", prev, nxt
        span = D(str((nxt["time"] - prev["time"]).total_seconds()))
        frac = D(str((instant - prev["time"]).total_seconds())) / span
        return off_prev + (off_next - off_prev) * frac, "anchor_ramp", prev, nxt
    if off_prev is not None:
        return off_prev, "anchor_prev", prev, None
    if off_next is not None:
        return off_next, "anchor_next", None, nxt
    if bias is not None:
        return bias, "median_bias", None, None
    return D(0), "no_anchor", None, None


def _main_basis(row, bias):
    return {
        "source": "主站",
        "method": "direct",
        "offset_m": D(0),
        "anchor_start": row["time"],
        "anchor_start_line": row.get("_line"),
        "anchor_end": row["time"],
        "anchor_end_line": row.get("_line"),
        "wind_bias_m": bias,
    }


def _cadence(points):
    """主站水位点的典型间隔（秒）；不足两点返回 None。"""
    times = sorted(row["time"] for row in points)
    deltas = [(b - a).total_seconds() for a, b in zip(times, times[1:]) if b > a]
    if not deltas:
        return None
    return _median([D(str(value)) for value in deltas])


def _needs_backup(instant, anchors, anchor_times, cadence):
    """主站水位在该时刻是否属于断档，需要备站补点。

    只在主站首末锚点覆盖的时段内补点；常规覆盖（主站前后两点间隔不超过
    典型间隔的 1.5 倍）不补点，避免把备站噪声当成水量；断档时才用备站
    补点，维持库容连续。主站完全没有水位锚点时一律不补，交由隔离处理。
    """
    if not anchors:
        return False
    if instant < anchors[0]["time"] or instant > anchors[-1]["time"]:
        return False
    if cadence is None:
        return True
    index = bisect_left(anchor_times, instant)
    prev = anchors[index - 1] if index > 0 else None
    nxt = anchors[index] if index < len(anchors) else None
    if nxt is not None and nxt["time"] == instant:
        return False
    if prev is None or nxt is None:
        return True
    gap = (nxt["time"] - prev["time"]).total_seconds()
    return D(str(gap)) > cadence * D("1.5")


def _reservoir_key(name):
    """库名归一：去掉“水库/库”等通用后缀后比较。"""
    text = str(name).strip()
    for suffix in ("水库", "库"):
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)]
            break
    return text


def _canonical_reservoirs(records):
    """把“青岚”“青岚库”“青岚水库”这类同库异写归到主站库名。

    只把库名归一到某个唯一的主站库；没有唯一对应时保持原样，宁可分成
    两条也不擅自合并。
    """
    main_names = sorted({row["reservoir"] for row in records if row["role"] == "main"})
    keyed = defaultdict(list)
    for name in main_names:
        keyed[_reservoir_key(name)].append(name)
    alias = {}
    for name in {row["reservoir"] for row in records}:
        if name in main_names:
            continue
        candidates = keyed.get(_reservoir_key(name), [])
        if len(candidates) == 1:
            alias[name] = candidates[0]
    for row in records:
        if row["reservoir"] in alias:
            row["reservoir"] = alias[row["reservoir"]]
    return records


def _infer_roles(records, station_roles):
    """为未登记站别的测站推断主备关系。

    站名带主站/备站标记的直接采用；否则仅当整个文件只有一座报流量的
    主站库，且另有一条无标记、只报水位、从不报流量的站线与其时段重叠
    时，才把该站线按备站挂到这座库。多库文件不做猜测，避免把无关水库
    并成一座；需要时用显式 station_roles。显式登记与站名标记优先。
    """
    profile = defaultdict(lambda: {"flow": 0, "level": 0, "span": [None, None]})
    for row in records:
        info = profile[row["station"]]
        info["level"] += 1 if row.get("level") is not None else 0
        info["flow"] += 1 if (row.get("inflow") is not None and row.get("outflow") is not None) else 0
        low, high = info["span"]
        info["span"] = [row["time"] if low is None else min(low, row["time"]),
                        row["time"] if high is None else max(high, row["time"])]
    marked = set()
    if isinstance(station_roles, dict):
        marked.update(station_roles)
    for row in records:
        if ROLE_RE.match(str(row["station"]).strip()):
            marked.add(row["station"])

    unmarked_main = [row for row in records
                     if row["role"] == "main" and row["station"] not in marked]
    candidates = sorted({row["reservoir"] for row in unmarked_main
                         if profile[row["station"]]["flow"] > 0})
    if len(candidates) != 1:
        return records
    target = candidates[0]
    main_stations = {row["station"] for row in unmarked_main
                     if row["reservoir"] == target and profile[row["station"]]["flow"] > 0}
    spans = [profile[station]["span"] for station in main_stations]
    for row in records:
        if row["station"] in marked or row["role"] != "main":
            continue
        info = profile[row["station"]]
        if info["flow"] != 0 or info["level"] == 0:
            continue
        low, high = info["span"]
        if any(other[0] <= high and low <= other[1] for other in spans):
            row["reservoir"] = target
            row["role"] = "backup"
    return records


def merge(records, curve, max_gap_hours, spike_m, station_roles=None):
    """把测次按水库合并为一条有来源证据的水位序列。

    返回 records（保留测次）、quarantine（气泡隔离）、levels（统一水位点）、
    stations（各库站别与风壅偏差）。保留测次的 level 为接续后水位，
    level_raw 保留原始观测，level_source / level_basis 记录依据。
    """
    low, high = curve[0][0], curve[-1][0]
    for row in records:
        if not row.get("reservoir") or not row.get("role"):
            reservoir, role = classify_station(row["station"], station_roles)
            row["reservoir"] = row.get("reservoir") or reservoir or row["station"]
            row["role"] = row.get("role") or role
    records = _infer_roles(records, station_roles)
    records = _canonical_reservoirs(records)

    grouped = defaultdict(lambda: {"main": [], "backup": []})
    for row in records:
        grouped[row["reservoir"]]["main" if row["role"] == "main" else "backup"].append(row)

    kept = []
    quarantine = []
    levels = []
    station_info = {}

    for reservoir in sorted(grouped):
        main_rows = sorted(grouped[reservoir]["main"], key=lambda r: r["time"])
        backup_rows = sorted(grouped[reservoir]["backup"], key=lambda r: r["time"])

        # 同库多主站同刻水位矛盾：无法判定谁对，全部隔离，不进入接续。
        by_time = defaultdict(list)
        for row in main_rows:
            if row.get("level") is not None:
                by_time[row["time"]].append(row)
        conflicted = set()
        for instant, group in by_time.items():
            stations = {row["station"] for row in group}
            distinct = {row["level"] for row in group}
            if len(stations) > 1 and len(distinct) > 1:
                for row in group:
                    quarantine.append(rejected(
                        row, "CONFLICT",
                        "同库不同主站同刻水位矛盾：%s" % "、".join(
                            "%s %s m" % (r["station"], r["level"]) for r in sorted(group, key=lambda r: r["station"]))),
                    )
                    conflicted.add(id(row))
        if conflicted:
            main_rows = [row for row in main_rows if id(row) not in conflicted]

        valid_backup = [row for row in backup_rows if row.get("level") is not None]
        valid_backup, spikes = filter_spikes(valid_backup, spike_m)
        spike_ids = {id(row) for row, _ in spikes}
        for row, residual in spikes:
            quarantine.append(rejected(
                row, "SPIKE",
                "备站气泡跳变：水位 %s m，偏离相邻内插 %s m" % (row["level"], residual),
            ))
            row["level_source"] = "隔离"
            row["level_basis"] = {"source": "隔离", "method": "spike", "offset_m": None,
                                  "residual_m": residual, "wind_bias_m": None}

        bias, samples, bias_method = estimate_bias(main_rows, valid_backup, max_gap_hours)
        at = _interpolator(valid_backup, max_gap_hours)
        anchors = [row for row in main_rows if row.get("level") is not None]
        anchor_times = [row["time"] for row in anchors]
        cadence = _cadence(anchors)

        points = {}
        spliced = 0
        outage = 0
        incomplete = []

        # 主站：有水位直接采信，缺水位时用备站接续；流量始终沿用主站。
        for row in main_rows:
            row["level_raw"] = row.get("level")
            row["flow_source"] = "主站" if row.get("inflow") is not None and row.get("outflow") is not None else "缺测"
            row["flow_basis"] = {
                "source": row["flow_source"],
                "method": "trapezoid" if row["flow_source"] == "主站" else "none",
                "in_line": row.get("_line"),
            }
            if row.get("level") is not None:
                row["level_source"] = "主站"
                row["level_basis"] = _main_basis(row, bias)
                points[row["time"]] = {
                    "reservoir": reservoir, "time": row["time"], "level": row["level"],
                    "level_source": "主站", "level_basis": row["level_basis"],
                    "level_raw": row.get("level_raw"), "station": row["station"],
                    "role": "main", "line": row.get("_line"),
                    "id": row.get("id"), "arrival": row.get("arrival"),
                }
                continue
            value = at(row["time"])
            if value is None:
                # 主站与备站同时缺水位：不发明水位，整条隔离并留下缺口证据。
                row["level_source"] = "缺测"
                row["level_basis"] = {"source": "缺测", "method": "none", "offset_m": None,
                                      "wind_bias_m": bias}
                row["flow_source"] = "缺测"
                row["flow_basis"] = {"source": "缺测", "method": "none", "in_line": row.get("_line")}
                incomplete.append(row)
                continue
            offset, method, prev, nxt = _offset_at(row["time"], anchors, anchor_times, at, bias)
            level = value + offset
            if not low <= level <= high:
                row["level_source"] = "缺测"
                row["level_basis"] = {"source": "缺测", "method": "out_of_curve",
                                      "offset_m": offset, "wind_bias_m": bias}
                row["flow_source"] = "缺测"
                row["flow_basis"] = {"source": "缺测", "method": "none", "in_line": row.get("_line")}
                quarantine.append(rejected(
                    row, "RANGE",
                    "备站接续水位 %s m 超出库容曲线范围，未入账；前后按缺口处理" % level))
                continue
            row["level"] = level
            row["level_source"] = "备站"
            row["level_basis"] = _basis(method, offset, prev, nxt, bias)
            spliced += 1
            points[row["time"]] = {
                "reservoir": reservoir, "time": row["time"], "level": row["level"],
                "level_source": "备站", "level_basis": row["level_basis"],
                "level_raw": row.get("level_raw"), "station": row["station"],
                "role": "main", "line": row.get("_line"),
                "id": row.get("id"), "arrival": row.get("arrival"),
            }

        excluded = {id(row) for row in incomplete}
        for row in incomplete:
            quarantine.append(rejected(
                row, "INCOMPLETE",
                "主站与备站同时缺水位，无法接续；流量随之断账，前后按缺口处理",
            ))
        kept.extend(row for row in main_rows if id(row) not in excluded)

        # 备站：主站同刻已有水位时不重复采信；主站断档时补点。
        for row in backup_rows:
            row["level_raw"] = row.get("level")
            if id(row) in spike_ids:
                continue
            if row.get("level") is None:
                row["level_source"] = "缺测"
                row["level_basis"] = {"source": "缺测", "method": "none", "offset_m": None,
                                      "wind_bias_m": bias}
                kept.append(row)
                continue
            if row["time"] in points:
                info = points[row["time"]]
                # 同刻已有采信水位：备站观测只作交叉校核，记录采用水位以入账
                # 值为准，level_raw 保留备站原始读数，便于回查两站差值。
                row["level"] = info.get("level")
                row["level_source"] = info.get("level_source")
                row["level_basis"] = {"source": info.get("level_source"),
                                      "method": "shadowed",
                                      "offset_m": None, "used_line": info.get("line"),
                                      "used_source": info.get("level_source"),
                                      "wind_bias_m": bias,
                                      "reason": "同刻已有采信水位，备站只作交叉校核未入账"}
                kept.append(row)
                continue
            if not _needs_backup(row["time"], anchors, anchor_times, cadence):
                row["level_source"] = "备站"
                row["level_basis"] = {"source": "备站", "method": "not_used", "offset_m": None,
                                      "reason": "主站常规覆盖，备站只作交叉校核未入账", "wind_bias_m": bias}
                kept.append(row)
                continue
            offset, method, prev, nxt = _offset_at(row["time"], anchors, anchor_times, at, bias)
            level = row["level"] + offset
            if not low <= level <= high:
                row["level_source"] = "缺测"
                row["level_basis"] = {"source": "缺测", "method": "out_of_curve",
                                      "offset_m": offset, "wind_bias_m": bias}
                quarantine.append(rejected(
                    row, "RANGE",
                    "备站补点水位 %s m 超出库容曲线范围，未入账；前后按缺口处理" % level))
                continue
            row["level"] = level
            row["level_source"] = "备站"
            row["level_basis"] = _basis(method, offset, prev, nxt, bias)
            row["flow_source"] = "缺测"
            row["flow_basis"] = {"source": "缺测", "method": "none", "in_line": row.get("_line")}
            outage += 1
            points[row["time"]] = {
                "reservoir": reservoir, "time": row["time"], "level": row["level"],
                "level_source": "备站", "level_basis": row["level_basis"],
                "level_raw": row.get("level_raw"), "station": row["station"],
                "role": "backup", "line": row.get("_line"),
                "id": row.get("id"), "arrival": row.get("arrival"),
            }
            kept.append(row)

        levels.extend(points[key] for key in sorted(points))
        station_info[reservoir] = {
            "reservoir": reservoir,
            "main_stations": sorted({row["station"] for row in main_rows}),
            "backup_stations": sorted({row["station"] for row in backup_rows}),
            "wind_bias_m": bias,
            "bias_samples": samples,
            "bias_method": bias_method,
            "main_cadence_s": cadence,
            "spikes": len(spikes),
            "spliced": spliced,
            "outage_points": outage,
        }

    kept.sort(key=lambda r: (r["reservoir"], r["time"], r["role"], r.get("_line") or 0))
    levels.sort(key=lambda r: (r["reservoir"], r["time"]))
    return {"records": kept, "quarantine": quarantine, "levels": levels, "stations": station_info}
