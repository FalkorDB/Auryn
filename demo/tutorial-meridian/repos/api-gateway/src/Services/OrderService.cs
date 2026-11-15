using System.Net.Http.Json;
using Meridian.Gateway.Models;

namespace Meridian.Gateway.Services;

// Reaches platform-core over HTTP. Each call is an outbound HTTP seam the code graph keys
// by route, so it heals onto platform-core's matching FastAPI endpoint in the same graph.
public class OrderService
{
    private readonly HttpClient _http;

    public OrderService(HttpClient http) => _http = http;

    public async Task<OrderResponse> Checkout(OrderRequest req)
    {
        // -> platform-core POST /charge  (the shared billing funnel)
        var resp = await _http.PostAsJsonAsync("/charge", req);
        return await resp.Content.ReadFromJsonAsync<OrderResponse>() ?? new OrderResponse();
    }

    public async Task<OrderResponse> GetOrder(string id)
    {
        // -> platform-core GET /orders/{id}
        var resp = await _http.GetAsync($"/orders/{id}");
        return await resp.Content.ReadFromJsonAsync<OrderResponse>() ?? new OrderResponse();
    }
}
