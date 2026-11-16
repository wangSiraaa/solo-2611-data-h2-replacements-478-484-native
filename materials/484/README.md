运行 `python3 probe.py`。device 使用真实文件顺序 append 和整 zone reset，index 是可独立调用的版本/位置目录，reclamation 执行搬迁并调用目录发布位置，store 提供常规 key 读写。

正常的 put/get 和无读者清理已经可跑。probe 另展示直接迟到 relocate 覆盖新版本、尚有 pin 的旧位置被 reset，以及真实线程中复制后新 publish 被迟到清理覆盖。探针会释放自身事件并 join 线程。所有写入均在 TemporaryDirectory 内。

公共索引接口：publish(key,location) 返回新逻辑版本；get(key) 返回当前 (version,location)；pin(key) 返回含 key/version/location 的 ReadHandle；release(handle)；relocate(key,expected_version,expected_location,new_location) 返回是否接受；references(zone)、entries()、audit()。实现可调整内部结构及增加参数，但这些接口也会直接由读取方使用。

Cleaner.clean(zone,after_copy=None,before_reset=None) 提供清理入口和两个可选受控观察点，Store 可委托它。回调只用于确定性交错，不构成实际业务锁。源设备无掉电/重启协议，任务无需补建 WAL。
