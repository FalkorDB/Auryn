package com.acme.app;

public record Point(int x, int y) {
    int sum() {
        return x + y;
    }
}
