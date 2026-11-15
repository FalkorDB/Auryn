import { Component } from "./base";
import { Named, Status, Store } from "./models";

export class Widget extends Component implements Named {
  name: string;
  private status: Status;

  constructor(private store: Store, name: string) {
    super();
    this.name = name;
    this.status = Status.Active;
  }

  save(): void {
    this.store.put(this.name);
  }

  describe(): string {
    return this.render();
  }
}
