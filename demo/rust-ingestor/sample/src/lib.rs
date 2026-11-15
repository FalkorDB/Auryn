//! Demo crate root for the Rust auryn.
//!
//! Exercises every node/edge kind the fast tree-sitter engine emits: an inline module tier
//! (`geometry`, with a nested `mod`), an external-file module declaration (`mod app;` -> src/app.rs),
//! a struct with an inherent impl and two trait impls, a trait with a supertrait bound and a
//! default method, an enum with unit/tuple/struct variants, crate-level consts and free functions,
//! and calls of every shape (free `foo()`, `self.m()`, receiver-typed `x.m()`, and `Type::new()`).

use crate::geometry::{Circle, Shape};

/// A crate-level constant (Const node, HAS_CONST off the Project).
pub const PI: f64 = 3.141592653589793;

static UNIT_LABEL: &str = "px";

pub mod geometry {
    /// Anything with a human-readable name.
    pub trait Named {
        fn name(&self) -> String;
    }

    /// A geometric shape. `Shape: Named` is a supertrait bound -> Shape EXTENDS Named.
    pub trait Shape: Named {
        /// Required method (no body).
        fn area(&self) -> f64;

        /// Default method (has a body) -- its call to `self.name()` resolves within the trait.
        fn describe(&self) -> String {
            self.name()
        }
    }

    pub struct Circle {
        pub radius: f64,
        label: String,
    }

    impl Circle {
        /// Associated function (no `self`) -> a Method with is_associated = true.
        pub fn new(radius: f64) -> Circle {
            Circle { radius, label: String::from("circle") }
        }

        fn scaled_radius(&self, factor: f64) -> f64 {
            self.radius * factor
        }
    }

    impl Named for Circle {
        fn name(&self) -> String {
            self.label.clone()
        }
    }

    impl Shape for Circle {
        fn area(&self) -> f64 {
            let r = self.scaled_radius(1.0); // self.m() -> Circle::scaled_radius
            PI_LOCAL * r * r
        }
    }

    const PI_LOCAL: f64 = 3.14;

    /// Enum with a unit, a tuple, and a struct-like variant.
    pub enum Kind {
        Round,
        Polygon(u32),
        Custom { sides: u32 },
    }

    /// A nested inline module -> HAS_SUBMODULE geometry -> geometry::conversions.
    pub mod conversions {
        pub fn to_diameter(radius: f64) -> f64 {
            radius * 2.0
        }
    }
}

/// External-file module declaration; its body lives in src/app.rs.
pub mod app;

pub fn run() -> f64 {
    let c = geometry::Circle::new(2.0); // Type::new() -> Circle::new (associated fn)
    area_of(&c) // free call -> area_of
}

fn area_of(shape: &Circle) -> f64 {
    shape.area() // receiver-typed: `shape` is a &Circle param -> Circle::area
}
