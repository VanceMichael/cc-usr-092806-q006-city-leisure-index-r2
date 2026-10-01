# 协同事务平台

协同事务平台为需要多机构参与的业务提供统一的机构、授权、案件、证据、审批、资源预约、资金分录、消息归并、通知、定时任务和历史回放能力。项目只使用 Python 标准库与 SQLite，适合在单个 Linux 应用容器内运行。

## 目录

- `src/civicflow/`：领域服务、SQLite 持久化、权限和命令行入口。
- `tests/`：核心流程、边界条件和异常路径测试。
- `examples/`：本地演示输入。

## 配置

通过 `CIVICFLOW_DB` 指定 SQLite 文件路径；不设置时命令行使用当前目录下的 `civicflow.sqlite3`。所有时间使用带时区的 ISO 8601 字符串。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 编译或构建

```bash
PYTHONPATH=src python3 -m compileall -q src
```

## 使用

初始化数据库并运行离线演示：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 demo
```

查看当前案件：

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/civicflow-demo.sqlite3 list-cases
```

## 城市休闲化指数复核

`leisure.py` 在同一平台内承载指数复核：

- 按城市与统计期保存人口基准（市域/城区/常住三种口径，版本化分母）、休闲资源、服务覆盖、空间密度观测、样本出处与覆盖期、指标权重；每次计算在结果谱系中固定单位、覆盖期和分母版本。
- 报送按批次幂等：同批次重复提交返回既有处理结果；批次编号相同但内容不一致时写入冲突留痕且暂不入库、不计算。
- 单位换算与权重调整必须说明原因，由另一名成员确认后才影响待发布结果；关账前未完成确认不能关账。
- 统计期经历催收 → 复核 → 关账，催收提醒与重算任务均落入可恢复队列，进程重启后继续。
- 迟到修订只触发相关城市、相关维度的重算；正式年度榜单原貌冻结，变化以带关联关系的勘误版本说明旧/新名次与分值。
- 合作机构只能查阅获授权城市；任一排名都可经 `leisure-trace` 追溯指标值、人口版本、来源材料、调整理由与责任人。

```bash
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/leisure-demo.sqlite3 leisure-demo
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/leisure-demo.sqlite3 leisure-rankings --period 2025
PYTHONPATH=src python3 -m civicflow.cli --db /tmp/leisure-demo.sqlite3 leisure-trace --period 2025 --city sh
```
