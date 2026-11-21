#include "geometry.h"
double Shape::area() const { return 0.0; }
Circle::Circle(double radius) : r(radius) {}
double Circle::area() const { return 3.14159 * r * r; }
