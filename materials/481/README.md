# 本地批数据函数宿主

Python3.11；`python3 -m pip install -r requirements.txt`，在本目录执行
`python3 probe.py`。依赖 Wasmtime 只提供真实虚拟机、fuel 和隔离。

- abi.decode_image：批列内存镜像的独立译码入口；`python3 batch_client.py`
  运行原生客户端。`image` 的长度就是本次可访问内存范围，成功返回的行已复制，
  之后修改可变 `image` 不影响结果。
- product.Product：普通生产者和 WASM 宿主共用的分批工件接口；
  `python3 product_client.py` 演示完整发布。行与回执先在内存暂存，`commit`
  在整作业握手成立后以同目录临时文件 + `os.replace` 原子发布；失败只写
  `<name>.diag.json` 诊断，`inspect` 区分 unpublished / failed / complete。
- runtime.Runner：真实 guest 调用链，`probe.py` 还同时调用上述两个独立客户端。
  每个 job 使用独立 Store/Instance，fuel 在 job 开始时设一次（不按批加满），
  只读取 `process` 返回后的当前内存，记录每次 alloc/process/release/dealloc
  的实参、燃料与内存页变化。

完整字节 ABI、job 生命周期与可变范围见 semantics.md。WAT 是公开可执行材料，
不按文件名定义结果；identity 会在 process 中 grow，bad_descriptor 返回越界
正文，second_trap 在第二批 trap，loop 消耗真实 fuel。probe 将每次运行工件写
到临时目录，退出后清理。

## 自测

`python3 tests.py` 运行 32 项证据检查（越界/偏移倒退/NULL 非空 span/错批号
与 FINAL/flags 保留位/validity padding/重叠/UTF-8/溢出/可变镜像/输出别名/
release 与 dealloc trap/second_trap 前缀隔离/fuel 全局预算/grow 后读取/
下一 job 隔离/模块接口拒绝/已有 complete 不被覆盖等），并包含一个不依赖
`abi` 译码逻辑的独立有效位与切片 oracle，逐码点核对 identity 输出。
