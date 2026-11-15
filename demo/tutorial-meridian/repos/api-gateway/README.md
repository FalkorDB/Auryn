# api-gateway (C# / ASP.NET Core BFF)

The backend-for-frontend the web-portal talks to. Exposes REST endpoints and fans them out
to platform-core over HTTP.

| Portal calls (exposed here)        | Controller.Action              | Service call → platform-core |
|------------------------------------|--------------------------------|------------------------------|
| `POST /api/orders`                 | `OrdersController.PlaceOrder`   | `POST /charge`               |
| `GET  /api/orders/{id}`            | `OrdersController.GetOrder`     | `GET  /orders/{id}`          |
| `POST /api/refunds`                | `RefundsController.Refund`      | `POST /refund`               |
| `POST /api/promotions`             | `PromotionsController.ApplyPromo` | `POST /promo`             |
| `GET  /api/reports/daily`          | `ReportsController.DailyReport` | `GET  /reports/daily`        |

Each service holds an `HttpClient` whose `BaseAddress` is platform-core, so it calls
relative routes that heal onto platform-core's FastAPI endpoints in the shared graph.
