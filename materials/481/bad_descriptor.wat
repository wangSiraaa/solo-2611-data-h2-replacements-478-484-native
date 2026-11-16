(module
 (memory (export "memory") 1 4)
 (func (export "alloc") (param i32) (result i32) i32.const 4096)
 (func (export "dealloc") (param i32 i32))
 (func (export "release") (param i32))
 (func (export "process") (param $p i32) (param i32) (param i32) (result i32)
  local.get $p i32.const 20 i32.add i32.const 65536 i32.store
  local.get $p i32.const 24 i32.add i32.const 100 i32.store
  local.get $p))
