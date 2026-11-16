# 离线绑定的联系人导入

Python3.11与标准库SQLite，无外部依赖。在本目录执行：

- `python3 preview.py catalog.json request.json`：独立离线绑定客户端，不连接DB。
- `python3 probe.py`：真实SQLite正常导入及两条独立入口反例。

binding.bind生成可JSON传递的BoundPlan；execution.apply直接消费该计划，importer只负责两者组合。公开输入/类型/动作/失败语义见contract.md。database.py是现有真实约束设施，receipt.py复制动作时完整行，不是完整绑定或执行答案。
