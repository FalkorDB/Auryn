export interface Named {
  name: string;
}

export enum Status {
  Active,
  Inactive,
}

export type Id = string | number;

export class Store {
  private items: string[] = [];

  put(value: string): void {
    this.items.push(value);
  }

  size(): number {
    return this.items.length;
  }
}
