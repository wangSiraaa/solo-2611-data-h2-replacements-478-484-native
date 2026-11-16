# 离线绑定与受控UPSERT合同

固定真实SQLite contacts schema由database.create创建，保留其约束。数据行只能含tenant/email/active/external_id/n；tenant及external_id为非空ASCII文本，email为ASCII文本或NULL，active严格是整数0/1（bool不算int），n是int64。id数据库生成且输入禁止。email空串参与唯一性，NULL不参与；lower只处理ASCII。最多500源行，单进程，不做schema迁移、网络并发或掉电恢复。

## binding

binding.describe(db)生成可存为JSON的目录，binding.bind(catalog,request)只接目录与typed请求，离线不连接数据库、不改表；preview.py就是独立消费者。目录table/columns/indexes描述固定schema，catalog_sha256按binding.identity的canonical JSON定义。绑定后执行计划可以JSON往返；不把数据库连接、cursor或Python闭包放进工件。

请求target只有两种合法规范形：
1. {"columns":["external_id"]}
2. {"columns":["tenant",{"expr":"lower","column":"email"}],"where":{"column":"active","op":"eq","value":1}}
要求恰好匹配目录中的现有唯一索引；大小写、列顺序及表达式不做代数猜测，缺partial谓词、未知列或不等价表达式拒绝。外部身份目标规范名external_id；表达式目标规范名active_email。

request.action为{"kind":"nothing"}或{"kind":"update","set":{column:expr,...},"where":predicate可选}，set非空，只能写email/active/external_id/n。expr为{"old":column}、{"excluded":column}、{"literal":value}，或{"op":"add|sub|coalesce","args":[expr,expr]}；add/sub只接受int64且NULL传播，结果超int64整批失败；coalesce短路并要求非NULL分支同型。old可引用id和其他列，excluded不能引用数据库生成id。where比较形如{"op":"eq|ne|lt|le|gt|ge","args":[expr,expr]}，IS NULL为{"op":"isnull","arg":expr}，and/or为{"op":"and|or","args":[predicate,...]}（列表非空），not为{"op":"not","arg":predicate}；比较同型并遵守SQL三值逻辑。AST深度至8。字段不存在、类型不符、非法目标和不支持的表达式在bind时给具体路径错误。literal NULL的上下文类型由被比较、赋值或coalesce另一端确定，不能跨型隐式转换；无法从上下文确定NULL类型的表达式在bind拒绝。

BoundPlan含format=1、catalog_sha256、target_index和规范化action，保持可JSON序列化；可增加typed节点元数据。plan是受控结构，不包含可执行任意SQL文本。非法请求不得返回计划。既有预览消费者会在无DB的进程调用bind，不能只在importer补校验。

## execution

execution.apply(db,plan,rows)消费合法BoundPlan和源行，不接受未绑定request代替。先核plan结构和真实db目录身份，拒绝未知format、目录不符或损坏计划，再做源行写入。计划允许由绑定客户端传递，执行器不应依赖某个刚创建计划的Python对象身份；目录摘要不是抵御恶意篡改的签名，不要求加密签名或权限系统。

只用指定目标仲裁冲突：未命中该目标则INSERT；命中才评估action.where，非TRUE为skipped；nothing只略过目标冲突，其他唯一约束继续真实生效。update用该源行动作前old及excluded同时求新值；UPDATE引发任何唯一冲突或溢出整批回滚。一个输入同时撞到两个不同unique对应的两行不能任选一行成功。可用原生ON CONFLICT和受控表达式编译，也可显式查目标再受控写，SQL均参数绑定；不重写SQLite唯一索引。

按源位置source_no顺序处理，后行看前行变化；同一目标可多次更新。每源行给insert/update/skipped、目标id；有动作的value保存动作结束时完整行，skipped的value保存当时未改变行。返回receipts及本批涉及id按id排序的final_rows，区分动作时值和批末值。失败无有效成功回执，业务表无本批部分修改。inspect可直接SELECT读取完整当前表。

public preview和importer使用同一BoundPlan协议；两个公共入口分别可独立调用，内部算法和SQL组织不固定。示例目录和请求见catalog.json、request.json。
