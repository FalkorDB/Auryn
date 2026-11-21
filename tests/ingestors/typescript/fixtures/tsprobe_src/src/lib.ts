import { deep } from "./other";

function helper(): number { return 1; }

export class Fast {
  run(): number { return helper(); }
}
export class Slow {       // a decoy: same method name `run`, so the fast name pass can't disambiguate f.run()
  run(): number { return 0; }
}

export function caller(): number {
  const f = new Fast();        // inferred type Fast -- deep resolves f.run to Fast#run
  return f.run() + deep();     // name pass: ".run" is ambiguous (Fast#run vs Slow#run) -> dropped
}

export function fib(n: number): number {
  return n <= 1 ? n : fib(n - 1) + fib(n - 2);   // recursion: the self CALLS edge must survive deep
}

export function getHelper(): () => number {
  return helper;               // `helper` used as a VALUE, not called -- deep must NOT emit a CALLS edge
}
