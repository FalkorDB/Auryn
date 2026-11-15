import React from "react";
import { refundOrder } from "../api/client";

export function RefundButton({ txnId }: { txnId: string }) {
  const onClick = async () => {
    await refundOrder(txnId);
  };
  return <button onClick={onClick}>Refund</button>;
}
