using Microsoft.AspNetCore.Mvc;
using Meridian.Gateway.Models;
using Meridian.Gateway.Services;

namespace Meridian.Gateway.Controllers;

[ApiController]
public class PromotionsController : ControllerBase
{
    private readonly BillingService _billing;

    public PromotionsController(BillingService billing) => _billing = billing;

    [HttpPost("api/promotions")]
    public async Task<OrderResponse> ApplyPromo(PromoRequest req)
    {
        return await _billing.ApplyPromo(req);
    }
}
