module Shapes

export Shape, Circle, Rectangle, Canvas, area, total_area

"""An abstract base type -- concrete shapes are its subtypes (`<:`)."""
abstract type Shape end

struct Circle <: Shape
    radius::Float64
end

struct Rectangle <: Shape
    width::Float64
    height::Float64
end

# A mutable struct with fields (one referencing the abstract type via a container type).
mutable struct Canvas
    shapes::Vector{Shape}
    background::String
end

# One generic function `area` with two methods, dispatching on the concrete Shape subtypes.
area(c::Circle) = 3.141592653589793 * c.radius ^ 2

function area(r::Rectangle)::Float64
    return r.width * r.height
end

# Cross-function call: total_area calls the generic `area` (per element).
function total_area(canvas::Canvas)
    total = 0.0
    for shape in canvas.shapes
        total = total + area(shape)
    end
    return total
end

# A nested module (HAS_SUBMODULE) with its own function and a primitive type.
module Geometry
    primitive type Radian 64 end

    scale(factor::Float64) = factor * 2.0
end

end
