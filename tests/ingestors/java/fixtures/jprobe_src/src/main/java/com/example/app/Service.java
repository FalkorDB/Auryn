package com.example.app;
import com.example.model.Store;
public class Service {
    private final Store store = new Store();
    public void save(String v) { store.put(v); }
    public void saveMany(String v, int n) { store.put(v, n); }
    public int count() { return store.size(); }
}
