import React from "react";
import { CheckoutButton } from "./components/CheckoutButton";
import { RefundButton } from "./components/RefundButton";
import { PromoField } from "./components/PromoField";
import { OrderDetail } from "./components/OrderDetail";
import { ReportsPage } from "./components/ReportsPage";

export function App() {
  return (
    <div>
      <CheckoutButton order={{ accountId: 1, amount: 42.0 }} />
      <PromoField amount={42.0} />
      <RefundButton txnId="txn:USD:42.0" />
      <OrderDetail id="ord_1" />
      <ReportsPage />
    </div>
  );
}
