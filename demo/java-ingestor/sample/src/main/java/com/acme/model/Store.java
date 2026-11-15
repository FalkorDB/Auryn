package com.acme.model;

import java.util.ArrayList;
import java.util.List;

public class Store implements Repository {
    private final List<String> items = new ArrayList<>();

    @Override
    public void put(String value) {
        items.add(value);
    }

    @Override
    public int size() {
        return items.size();
    }
}
