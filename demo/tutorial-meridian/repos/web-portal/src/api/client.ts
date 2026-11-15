import axios from "axios";

// The single HTTP client the portal uses to reach the api-gateway (BFF). Every UI action
// funnels through one of these functions, so the code graph can trace a button click across
// the network to the gateway route it hits.
export const api = axios.create({ baseURL: "" });

export interface OrderInput {
  accountId: number;
  amount: number;
  currency?: string;
}

export function placeOrder(order: OrderInput) {
  return api.post("/api/orders", order);
}

export function refundOrder(txnId: string) {
  return api.post("/api/refunds", { txnId });
}

export function applyPromo(code: string, amount: number) {
  return api.post("/api/promotions", { code, amount });
}

export function getOrder(id: string) {
  return api.get(`/api/orders/${id}`);
}

export function fetchDailyReport() {
  return api.get("/api/reports/daily");
}
