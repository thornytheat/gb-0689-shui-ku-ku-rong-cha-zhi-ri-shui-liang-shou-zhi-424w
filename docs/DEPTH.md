# 深度登记 水库库容插值与日水量收支

- 规模：中型
- 核心规则：单调水位库容曲线分段线性插值；流量梯形积分；缺口超限不插补，日边界拆分且收支误差显式归因
- 使用者与工作流：水库调度数据员；采信水位流量日志，复核日库容变化与入出库偏差

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 测站采信 | 采信水位流量与来源 | `/api/m1` | `app/modules/m1_receive/rules.py` `app/modules/m1_receive/service.py` | `tests/test_m1_receive.py` |
| M2 曲线校核 | 校验曲线、测次冲突并合并同库主备站 | `/api/m2` | `app/modules/m2_curve/rules.py` `app/modules/m2_curve/service.py` `app/merge.py` | `tests/test_m2_curve.py` `tests/test_merge.py` |
| M3 流量积分 | 梯形积分并定位缺口 | `/api/m3` | `app/modules/m3_integrate/rules.py` `app/modules/m3_integrate/service.py` | `tests/test_m3_integrate.py` |
| M4 库容换算 | 按曲线分段换算库容 | `/api/m4` | `app/modules/m4_storage/rules.py` `app/modules/m4_storage/service.py` | `tests/test_m4_storage.py` |
| M5 日收支 | 每库一条跨午夜日账并标注接续依据 | `/api/m5` | `app/modules/m5_budget/rules.py` `app/modules/m5_budget/service.py` | `tests/test_m5_budget.py` `tests/test_merge.py` |
| M6 异常复核 | 按缺测优先裁决超差 | `/api/m6` | `app/modules/m6_review/rules.py` `app/modules/m6_review/service.py` | `tests/test_m6_review.py` |
| M7 重算留痕 | 核对总积分与重算水位 | `/api/m7` | `app/modules/m7_trace/rules.py` `app/modules/m7_trace/service.py` | `tests/test_m7_trace.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | GB18030测站 | 静默填补或算错 | 明确隔离和守恒 | `app/modules/m1_receive/rules.py` | `tests/test_m1_receive.py::test_legacy_encoding` |
| TRAP-06 | 全角流量 | 静默填补或算错 | 明确隔离和守恒 | `app/modules/m2_curve/rules.py` | `tests/test_m2_curve.py::test_fullwidth` |
| TRAP-07 | 未知水位 | 静默填补或算错 | 明确隔离和守恒 | `app/modules/m2_curve/rules.py` | `tests/test_m2_curve.py::test_sentinel` |
| TRAP-10 | 小数插值 | 静默填补或算错 | 明确隔离和守恒 | `app/modules/m4_storage/rules.py` | `tests/test_m4_storage.py::test_decimal_precision` |
| TRAP-13 | 缺口和乱序 | 静默填补或算错 | 明确隔离和守恒 | `app/modules/m3_integrate/rules.py` | `tests/test_m3_integrate.py::test_real_late_point_retained` |
| TRAP-14 | 同刻水文矛盾 | 静默填补或算错 | 明确隔离和守恒 | `app/modules/m2_curve/rules.py` | `tests/test_m2_curve.py::test_real_conflict` |
| TRAP-15 | 升每秒换算 | 静默填补或算错 | 明确隔离和守恒 | `app/modules/m2_curve/rules.py` | `tests/test_m2_curve.py::test_unit_conversion` |
| TRAP-23 | 午夜切分 | 静默填补或算错 | 明确隔离和守恒 | `app/modules/m5_budget/rules.py` | `tests/test_m5_budget.py::test_midnight_split` |
| TRAP-31 | 主备站各报各的 | 同库算成两套日账 | 按库合并成一条日账 | `app/merge.py` | `tests/test_merge.py::test_two_station_sample_forms_one_reservoir_day` |
| TRAP-32 | 风壅几厘米偏差 | 直接拼接会被当成进出水 | 中位偏差+锚点渐变接续 | `app/merge.py` | `tests/test_merge.py::test_wind_bias_recovered_not_booked_as_flow` |
| TRAP-33 | 备站气泡跳变 | 跳变被当作库容突变 | 内插残差识别并隔离 | `app/merge.py` | `tests/test_merge.py::test_bubbles_isolated` |
| TRAP-34 | 主站短时缺测 | 只认主站就断账 | 备站水位接续、流量沿用主站 | `app/merge.py` | `tests/test_merge.py::test_main_outage_spliced_with_backup` |
| TRAP-35 | 重跑同批 | 重复新增日账或覆盖旧版 | 幂等ID复用、失败保留旧版 | `app/pipeline.py` `app/store.py` | `tests/test_rerun.py` |

## 对账规则

输入逻辑行数 = 采信行数 + 隔离行数；空行单列。每条重复、冲突、超限、缺失均保留原始行号与原因码。计算阶段的输出数量可聚合，原始采信records始终保留，业务平衡另按模块给出的重量、时长、电量或座席数量核对。
相同输入摘要、规则版本、参数产生相同批次ID，重跑同批复用已有日账不新增行。运行原子插入SQLite；计算失败在保存前抛错，不留半条记录也不覆盖旧版；复核使用整数版本乐观锁。

## 已知不覆盖

库容曲线采用线性分段，不作范围外推；流量与水位相邻点间线性变化。同库合并以主站水位为锚、备站只补缺测，不做多站数据同化；风壅偏差用中位数估计，不区分风向与时段。未建蒸发降雨及地下渗漏模型，收支差保留待人工归因。
