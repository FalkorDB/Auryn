import { Store } from "./models";
import { Widget } from "./widget";
import { greet } from "./util";

export function make(store: Store, name: string): Widget {
  return new Widget(store, name);
}

export const persist = (store: Store, w: Widget): void => {
  store.put(w.name);
};

export function run(): void {
  const store = new Store();
  const w = make(store, greet());
  persist(store, w);
}
