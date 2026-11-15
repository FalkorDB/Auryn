# web-portal (React + TypeScript)

The customer-facing UI. Every action is a component that calls one function in
`src/api/client.ts`, which issues the HTTP request to the api-gateway.

| Component         | client function     | gateway route hit        |
|-------------------|---------------------|--------------------------|
| `CheckoutButton`  | `placeOrder`        | `POST /api/orders`       |
| `PromoField`      | `applyPromo`        | `POST /api/promotions`   |
| `RefundButton`    | `refundOrder`       | `POST /api/refunds`      |
| `OrderDetail`     | `getOrder`          | `GET  /api/orders/{id}`  |
| `ReportsPage`     | `fetchDailyReport`  | `GET  /api/reports/daily`|

These are the entry points of the whole system: a click here is traceable across the
gateway and into platform-core.
