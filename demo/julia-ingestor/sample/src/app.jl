module App

using Printf

# A second module that uses the Shapes model. Cross-module calls resolve name-based to the
# generic functions defined in `Shapes` (a live Julia session would `using ..Shapes`; the fast
# engine resolves by name across the whole project regardless of the import form).

function build_canvas()
    canvas = Canvas(Shape[], "white")
    return canvas
end

function report(canvas::Canvas)
    a = total_area(canvas)
    println(a)
    return a
end

function demo()
    c = Circle(2.0)
    r = Rectangle(3.0, 4.0)
    a1 = area(c)
    a2 = area(r)
    return a1 + a2
end

end
