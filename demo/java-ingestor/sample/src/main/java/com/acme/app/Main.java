package com.acme.app;

import com.acme.model.Store;

public class Main {
    public Widget make(Store store) {
        return new Widget(store);
    }

    public void run() {
        Store store = new Store();
        Widget widget = make(store);
        widget.save();
    }
}
