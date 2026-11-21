package com.example.model;
public class Store {
    private final StringBuilder buf = new StringBuilder();
    public void record(String v) { buf.append(v); }
    public void put(String v) { record(v); }
    public void put(String v, int n) { for (int i = 0; i < n; i++) record(v); }
    public int size() { return buf.length(); }
}
