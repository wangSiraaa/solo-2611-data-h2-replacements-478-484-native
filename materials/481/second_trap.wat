(module
 (memory (export "memory") 1 4)
 (global $next (mut i32) (i32.const 4096))
 (func (export "alloc") (param $n i32) (result i32)
  (local $p i32) global.get $next local.set $p global.get $next local.get $n i32.add global.set $next local.get $p)
 (func (export "dealloc") (param i32 i32))
 (func (export "release") (param i32))
 (func (export "process") (param $p i32) (param $batch i32) (param i32) (result i32)
  local.get $batch i32.const 1 i32.eq if unreachable end local.get $p))
