#include "shapes.h"

/* internal linkage: a `square` here is distinct from any other file's `square`. */
static int square(int n) {
    return n * n;
}

int shape_area(const Shape *s) {
    return square(s->size);
}

Point point_make(int x, int y) {
    Point p;
    p.x = x;
    p.y = y;
    return p;
}
