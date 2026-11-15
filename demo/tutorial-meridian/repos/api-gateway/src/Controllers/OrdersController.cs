using Microsoft.AspNetCore.Mvc;
using Meridian.Gateway.Models;
using Meridian.Gateway.Services;

namespace Meridian.Gateway.Controllers;

// The BFF's order surface. The web-portal calls these routes; each action delegates to a
// service that reaches platform-core over HTTP.
[ApiController]
public class OrdersController : ControllerBase
{
    private readonly OrderService _orders;

    public OrdersController(OrderService orders) => _orders = orders;

    [HttpPost("api/orders")]
    public async Task<OrderResponse> PlaceOrder(OrderRequest req)
    {
        return await _orders.Checkout(req);
    }

    [HttpGet("api/orders/{id}")]
    public async Task<OrderResponse> GetOrder(string id)
    {
        return await _orders.GetOrder(id);
    }
}
