import React, { useEffect, useState } from "react";
import { getOrder } from "../api/client";

export function OrderDetail({ id }: { id: string }) {
  const [order, setOrder] = useState<any>(null);
  useEffect(() => {
    getOrder(id).then((r) => setOrder(r.data.order));
  }, [id]);
  return <pre>{JSON.stringify(order, null, 2)}</pre>;
}
