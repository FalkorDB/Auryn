#ifndef GEOMETRY_HPP
#define GEOMETRY_HPP

#include <string>

#define MAX_SIDES 12

namespace geo {

enum class ShapeKind { CIRCLE, SQUARE };

struct Point {
    int x;
    int y;
    int manhattan() const;   // declared here, defined out-of-line in shapes.cpp
};

// Abstract-ish base. C++ has no interfaces, so `Shape` is just a Class and deriving from it is
// EXTENDS (never IMPLEMENTS), even when it is purely a contract.
class Shape {
public:
    Shape(Point origin, ShapeKind kind);
    virtual ~Shape();
    virtual double area() const;
    ShapeKind kind() const;
    Point origin() const;
protected:
    Point origin_;
    ShapeKind kind_;
};

class Circle : public Shape {
public:
    Circle(Point center, double radius);
    double area() const override;   // defined out-of-line; overrides Shape::area
    double radius() const;
private:
    double radius_;
};

// A template class: `Box` and `Box<T>` must not collide (arity folded into the id).
template <typename T>
class Box {
public:
    Box(T value);
    T get() const;
private:
    T value_;
};

// Aggregates two shapes; its methods exercise cross-type calls (obj./ptr->/Class::/new).
class Scene {
public:
    Scene(Circle shape);
    double totalArea(Circle* other);
    static int limit();
private:
    Circle shape_;
};

Point make_point(int x, int y);   // a free function

} // namespace geo

#endif
