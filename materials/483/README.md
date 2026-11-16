运行 `python3 probe.py`。schema 包供导入程序独立预检列定义与行；storage 包接收编译计划，将写入结果和二级索引保存到本地文件。无外部依赖，Python 3.11。

Compiler.compile(columns) 返回 CompiledSchema，preview(base) 返回完整逻辑行，dump() 返回可 JSON 序列化计划；Compiler.load(payload) 重建计划。RowStore.create(path,plan,indexes)、open(path)、apply(operations)、query(index_name,operation,key,projection=None)、scan()、inspect()、close() 构成落库接口。

操作格式为 {"op":"insert"或"patch","id":"文本身份","base":{...}}。索引定义形如 {"name":"by_y","columns":["y"],"unique":false}。query 的 eq key 为字段值数组；range key 为 [lower,upper]，使用单列半开范围。

当前按声明顺序求值，正常已排序定义能运行；前向依赖、无效列引用、patch旧索引以及虚拟列落盘问题由 probe 直接展示。probe 不修改材料目录中的数据，所有存储写入均在临时目录。
