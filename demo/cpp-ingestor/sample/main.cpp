#include "geometry.hpp"
#include <cstdio>

using namespace geo;

int main() {
    Circle c(make_point(1, 2), 3.0);
    Scene scene(c);

    Box<int> box(MAX_SIDES);
    double total = scene.totalArea(&c);

    printf("area=%f sides=%d\n", total, box.get());
    return 0;
}
