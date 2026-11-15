import React from "react";
import { placeOrder, OrderInput } from "../api/client";

// The primary money-moving action in the whole product. A click here ultimately reaches
// platform-core's billing.charge, three repos away.
export function CheckoutButton({ order }: { order: OrderInput }) {
  const onClick = async () => {
    await placeOrder(order);
  };
  return <button onClick={onClick}>Checkout</button>;
}
