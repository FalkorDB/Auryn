using Microsoft.AspNetCore.Mvc;
using Meridian.Gateway.Models;
using Meridian.Gateway.Services;

namespace Meridian.Gateway.Controllers;

[ApiController]
public class RefundsController : ControllerBase
{
    private readonly BillingService _billing;

    public RefundsController(BillingService billing) => _billing = billing;

    [HttpPost("api/refunds")]
    public async Task<RefundResponse> Refund(RefundRequest req)
    {
        return await _billing.Refund(req);
    }
}
