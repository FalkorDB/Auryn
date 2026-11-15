import React, { useState } from "react";
import { applyPromo } from "../api/client";

// Applies a promo code. The code the user types travels all the way to platform-core's
// apply_promo -- which is exactly why the eval() in that handler is dangerous.
export function PromoField({ amount }: { amount: number }) {
  const [code, setCode] = useState("");
  const onApply = async () => {
    await applyPromo(code, amount);
  };
  return (
    <div>
      <input value={code} onChange={(e) => setCode(e.target.value)} />
      <button onClick={onApply}>Apply promo</button>
    </div>
  );
}
