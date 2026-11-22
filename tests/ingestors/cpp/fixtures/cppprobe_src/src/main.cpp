#include "geometry.h"
static double compute(const Shape& s) { return s.area(); }   // virtual dispatch -> Shape::area (static)
double run() { Circle c(2.0); return compute(c); }           // compute() call + Circle ctor
