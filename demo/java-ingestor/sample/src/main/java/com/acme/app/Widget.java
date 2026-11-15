package com.acme.app;

import com.acme.model.Status;
import com.acme.model.Store;

public class Widget extends Component {
    private final Store store;
    private Status status;

    public Widget(Store store) {
        this.store = store;
        this.status = Status.ACTIVE;
    }

    public void save() {
        store.put(render());
    }

    @Override
    public String render() {
        return "widget";
    }
}
