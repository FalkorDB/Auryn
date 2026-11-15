#include "shapes.h"
#include <stdio.h>

int main(void) {
    struct Shape s;
    s.kind = SHAPE_SQUARE;
    s.size = 4;

    int a = shape_area(&s);
    Point p = point_make(1, 2);

    printf("area=%d origin.x=%d\n", a, p.x);
    return 0;
}
