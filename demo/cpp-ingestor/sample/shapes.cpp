#include "geometry.hpp"

namespace geo {

// Out-of-line member definitions: each `Owner::method` is written apart from its class, yet must
// resolve back to that class's Method (merging with the in-class declaration -- one node).

int Point::manhattan() const {
    return x + y;
}

Shape::Shape(Point origin, ShapeKind kind) : origin_(origin), kind_(kind) {}

Shape::~Shape() {}

double Shape::area() const {
    return 0.0;
}

ShapeKind Shape::kind() const {
    return kind_;
}

Point Shape::origin() const {
    return origin_;
}

Circle::Circle(Point center, double radius)
    : Shape(center, ShapeKind::CIRCLE), radius_(radius) {}

double Circle::area() const {
    return 3.14159 * radius_ * radius_;
}

double Circle::radius() const {
    return radius_;
}

Scene::Scene(Circle shape) : shape_(shape) {}

double Scene::totalArea(Circle* other) {
    double a = shape_.area();        // obj.method()  -- shape_ is a field of type Circle
    double b = other->area();        // ptr->method() -- other is a param of type Circle*
    int cap = Scene::limit();        // Class::static()
    Circle* extra = new Circle(make_point(0, 0), 1.0);  // new Class() + a free-function call
    return a + b + extra->area() * cap;
}

int Scene::limit() {
    return MAX_SIDES;
}

Point make_point(int x, int y) {
    Point p;
    p.x = x;
    p.y = y;
    return p;
}

} // namespace geo
