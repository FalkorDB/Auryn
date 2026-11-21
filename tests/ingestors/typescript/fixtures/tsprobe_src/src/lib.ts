import { deep } from "./other";

function helper(): number { return 1; }

class Fast {
  run(): number { return helper(); }
}
class Slow {
  run(): number { return 0; }
}

export function caller(): number {
  const f = new Fast();        // inferred type Fast -- deep resolves f.run to Fast#run
  return f.run() + deep();     // name pass: ".run" is ambiguous (Fast#run vs Slow#run) -> dropped
}
