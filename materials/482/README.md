# 离线绑定的联系人导入

Python3.11 与标准库 SQLite，无外部依赖。在本目录执行：

- `python3 preview.py catalog.json request.json`：独立离线绑定客户端，不连接 DB，打印可 JSON 传递的 BoundPlan。
- `python3 offline_bind.py catalog.json request.json`：同样纯离线，非法请求以非零退出码输出**路径诊断**。
- `python3 probe.py`：真实 SQLite 上的完整验证套件（53 项断言），覆盖正常两源行、缺谓词拒绝、JSON 往返 apply、非目标 unique 冲突回滚、inactive、NULL/空串、两 unique 撞不同行、nothing、三值 WHERE、同批连续命中、末行失败整批回滚、500 行、int64 溢出与计划防护。
- `python3 bind_matrix.py`：无 DB 的非法请求矩阵（23 例），全部在 bind 阶段带具体路径拒绝。
- `python3 process_bind.py && python3 process_apply.py`：两个独立进程，前者只产出 JSON 计划工件，后者只读 JSON 写真实库，证明计划不依赖 Python 对象身份或闭包。

工件（catalog、bound plan、源行、逐源回执、批末表）写入 `artifacts/`。

## 设计要点

**绑定（binding，离线无 DB）**

- `binding.bind(catalog, request)` 只吃 `describe` 产出的目录与 typed 请求。target 只接受两种规范形：`external_id` 单列，或 `(tenant, lower(email)) WHERE active=1` 的精确结构；缺 partial 谓词、错表达式、未知列、列序不同一律按路径拒绝，绝不猜成 active_email。
- 表达式/谓词做完整类型检查：old/excluded/literal、add/sub（仅 int64、NULL 传播）、coalesce（短路、同型）、比较与 and/or/not/isnull 的 SQL 三值逻辑、AST 深度 ≤8、literal NULL 的上下文类型推断、bool 不等于 int。
- BoundPlan 为纯 JSON（`format=1`、`catalog_sha256`、`target_index`、规范化 target/action，节点带解析后类型），可跨进程往返，不含连接、cursor、闭包或任意 SQL 文本。

**执行（execution，受控写真实 SQLite）**

- `execution.apply(db, plan, rows)` 先核 format、结构、节点类型与**真实目录身份**（canonical JSON sha256），未绑定 request（无 format）直接拒绝。
- 冲突仲裁**只查指定目标**。目标未命中走普通参数化 INSERT，SQLite 对所有 unique 约束负责；目标命中后，若输入还在另一个 unique 上碰撞（同一目标行自身或另一行），改走普通 INSERT 让真实约束判非法——因此一个输入同时撞两个不同行时不会挑一条更新。这与原生 `ON CONFLICT` 的语义一致（原生 UPSERT 同样会把非目标冲突升级为约束错误），但显式查找额外保证“双不同行”不被选行。
- partial 索引资格按 `active=1 且 email 非 NULL` 判定；ASCII email 的 NULL 不碰撞、空串碰撞。
- nothing 只略过目标冲突；update 的可选 WHERE 仅在 TRUE 时动作，FALSE/NULL 为 skipped。赋值表达式在 Python 侧基于该源行**动作前** old 与 excluded 同时求值（SQLite 算术溢出会静默转 REAL，故 int64 溢出在求值层显式判错），再以参数化 SQL 写回。
- 按 source_no 源序处理，后行看前行结果；每源行回执保存动作结束时完整行（不可被后续更新改写），提交后另按 id 排序产出涉及 id 的 final_rows。任意约束、类型、溢出错误整批回滚，失败无有效成功回执。所有 SQL 参数绑定，不执行输入提供的 SQL，不重写索引。
