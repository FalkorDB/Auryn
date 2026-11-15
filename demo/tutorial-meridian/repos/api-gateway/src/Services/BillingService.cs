using System.Net.Http.Json;
using Meridian.Gateway.Models;

namespace Meridian.Gateway.Services;

public class BillingService
{
    private readonly HttpClient _http;

    public BillingService(HttpClient http) => _http = http;

    public async Task<RefundResponse> Refund(RefundRequest req)
    {
        // -> platform-core POST /refund
        var resp = await _http.PostAsJsonAsync("/refund", req);
        return await resp.Content.ReadFromJsonAsync<RefundResponse>() ?? new RefundResponse();
    }

    public async Task<OrderResponse> ApplyPromo(PromoRequest req)
    {
        // -> platform-core POST /promo  (which itself funnels into /charge)
        var resp = await _http.PostAsJsonAsync("/promo", req);
        return await resp.Content.ReadFromJsonAsync<OrderResponse>() ?? new OrderResponse();
    }
}
