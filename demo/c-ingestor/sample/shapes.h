#ifndef SHAPES_H
#define SHAPES_H

#define MAX_SIDES 12

enum ShapeKind { SHAPE_CIRCLE, SHAPE_SQUARE };

typedef struct Point {
    int x;
    int y;
} Point;

struct Shape {
    enum ShapeKind kind;
    Point origin;
    int size;
};

typedef struct Shape Shape;

int shape_area(const Shape *s);   /* declared here, defined in shapes.c */
Point point_make(int x, int y);

#endif
