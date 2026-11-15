//! Body of `mod app;` (declared in lib.rs). In the fast pass this file is parsed as its own
//! crate-root scope, so its items hang off the Project -- the external-file `mod app;` becomes a
//! (bodyless) Module node, and stitching this file into it is deferred to the deep pass. Calls to
//! `geometry`'s types still resolve, because resolution is name-based across the whole crate.

use crate::geometry::{Circle, Shape};

pub struct App {
    shapes: u32,
}

impl App {
    /// Associated function (no `self`) -- a constructor.
    pub fn new() -> App {
        App { shapes: 0 }
    }

    /// Drives the app; `self.add_circle(..)` resolves to this type's own method.
    pub fn run(&mut self) -> f64 {
        self.add_circle(2.0)
    }

    /// Constructs a Circle (cross-file `Type::new()`) and calls a method on it.
    pub fn add_circle(&mut self, radius: f64) -> f64 {
        let circle = Circle::new(radius); // Circle::new -> geometry::Circle::new
        self.shapes += 1;
        circle.area() // receiver is a local -> name-based fallback to Circle::area
    }

    pub fn count(&self) -> u32 {
        self.shapes
    }
}

pub fn total_area(app: &mut App, radii: &[f64]) -> f64 {
    let mut sum = 0.0;
    for r in radii {
        sum += app.add_circle(*r); // receiver-typed: `app` is &mut App -> App::add_circle
    }
    sum
}
