pub mod other;

pub trait Run { fn run(&self) -> u32; }

pub struct Fast;
impl Run for Fast { fn run(&self) -> u32 { 1 } }

pub fn helper() -> u32 { 42 }

// dynamic dispatch: the fast name pass can't pin this to Fast::run
pub fn via_dyn(x: &dyn Run) -> u32 { x.run() }

// generic dispatch
pub fn via_generic<T: Run>(x: T) -> u32 { x.run() }

pub fn caller() -> u32 {
    let f = Fast;
    let a = f.run();          // resolvable method call
    let b = helper();         // free call
    let c = other::deep();    // cross-module path call
    a + b + c
}

// a fn-pointer parameter call: even deep analysis can't name the runtime target
pub fn calls_param(f: fn() -> u32) -> u32 { f() }
