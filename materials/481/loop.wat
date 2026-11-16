(module
 (memory (export "memory") 1 4)
 (func (export "alloc") (param i32) (result i32) i32.const 4096)
 (func (export "dealloc") (param i32 i32))
 (func (export "release") (param i32))
 (func (export "process") (param i32 i32 i32) (result i32)
  (loop $forever br $forever) i32.const 0))
