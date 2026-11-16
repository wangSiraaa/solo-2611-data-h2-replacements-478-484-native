运行 `python3 probe.py` 核对正常预检路径、重开路径与三处已知缺陷；运行 `python3 verify.py` 执行覆盖全部题面约束的端到端验证。schema 包供导入程序独立预检列定义与行；storage 包接收同一份编译计划，把写入结果和二级索引保存到本地 JSON 文件。无外部依赖，Python 3.11。`records/` 保存一次实际运行输出。

## 计划格式

`Compiler.compile(columns)` 校验后返回 `CompiledSchema`，不访问数据库。列定义为
`{"name","type","kind","nullable"}`，生成列另带 `"expr"`，base 列可选 `"default"` 字面量：

```json
{"name":"z","type":"int64","kind":"stored","nullable":true,
 "expr":{"op":"+","args":[{"col":"y"},1]}}
```

- 至多 16 列、8 个生成列；`type ∈ {int64,text}`，`kind ∈ {base,stored,virtual}`。
- 表达式：int/text/NULL 常量、`{"col":name}`、`{"op":"+|-","args":[a,b]}`、
  `{"op":"concat","args":[a,b]}`、`{"op":"coalesce","args":[a,b]}`、
  `{"op":"case","when":cond,"then":e,"else":e}`。
- 条件：`{"op":"eq|lt","args":[a,b]}`、`{"op":"isnull","args":[a]}`。
- 编译期检查所有分支的类型、完整依赖集合与未知引用；前向引用合法，环拒绝。
  生成列与 base 列可任意互相依赖，只要整图无环。

`dump()` 返回 `{"columns":[...],"fingerprint":sha256(canonical columns)}`，可 JSON 序列化。
`Compiler.load(payload)` 重新编译并核对 fingerprint，得到语义与内容身份都相同的计划。
`preview(base)` 返回按声明顺序排列的完整逻辑行；只接受 base 字段。

求值语义（预检与落库共用 `schema.compiler.evaluate`）：

- base 缺省用声明 default，无 default 取 NULL；显式 `None` 不套默认。
- `+ - concat` 两个操作数都求值，任一为 NULL 结果为 NULL；整数溢出拒绝该行；
  concat 结果超过 128 码点拒绝。
- `coalesce` 只求左边，左边非 NULL 时不求右边。
- `case` 只执行所选分支。
- `eq/lt` 还原操作数必须同型（编译期检查），任一为 NULL 条件不成立。

## 落库接口

`RowStore.create(path,plan,indexes)`、`open(path)`、`apply(operations)`、
`query(index_name,operation,key,projection=None)`、`scan()`、`inspect()`、`close()`。

- 操作：`{"op":"insert"|"patch","id":"...","base":{...}}`。insert 不得与已有 id 重复；
  patch 省略字段保留旧 base，显式 `None` 替换。base 出现生成列或未知字段即拒绝。
- 一个 `apply` 至多 100 个操作，按序生效，后操作可见本批前操作；整批原子提交。
  失败批不改变内存行、索引目录或磁盘文件。
- 磁盘只保存 base 与 stored 列；virtual 在 projection、scan 与索引求键时计算。
- 保存 schema 计划、布局（物理列/类型）、索引定义目录与索引内容指纹；
  `open` 对 schema/布局/目录/存储身份/行布局/类型/stored 复算任一不匹配即拒绝。

索引定义 `{"name","columns":[...],"unique":bool}`（1 或 2 列，可含 virtual）。

- `eq`：key 为按索引列顺序的值数组；NULL 是独立可查询键值。
- `range`：单列索引，key 为 `[lower,upper]`，同型非 NULL，`lower <= v < upper`，
  排除 NULL。
- 结果按 id 排序，返回当前完整行或所选 projection。
- unique 索引中任一分量为 NULL 的键互不冲突；非 NULL 重复键拒绝。
- patch 在同一批内撤旧键、加新键，冲突则连同业务行一起回滚，不留孤儿键。

## 实际记录

- `probe.py`：前向 `z→y` 得 4，未知列被拒绝，重开后索引查询命中，patch 后旧键消失，
  物理列仅 `["x","z"]`。
- `verify.py`：16 个场景逐条打印 JSON；含独立 AST 解释器对 preview/reopen 后
  query/scan 的逐行比对、2000 行×6 索引容量、单命中业务行读取 1 行、失败批文件
  字节不变、8 类损坏拒开。
