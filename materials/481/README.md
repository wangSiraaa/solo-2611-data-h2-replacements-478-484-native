# 本地批数据函数宿主

Python3.11；`python3 -m pip install -r requirements.txt`，在本目录执行`python3 probe.py`。依赖Wasmtime只提供真实虚拟机、fuel和隔离。

- abi.decode_image：批列内存镜像的独立译码入口；`python3 batch_client.py`运行原生客户端。
- product.Product：普通生产者和WASM宿主共用的分批工件接口；`python3 product_client.py`演示完整发布。
- runtime.Runner：真实guest调用链，`probe.py`还同时调用上述两个独立客户端。

完整字节ABI、job生命周期与可变范围见semantics.md。WAT是公开可执行材料，不按文件名定义结果；identity会在process中grow，bad_descriptor返回越界正文，second_trap在第二批trap，loop消耗真实fuel。probe将每次运行工件写到临时目录，退出后清理。
