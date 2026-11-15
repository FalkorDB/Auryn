using Microsoft.AspNetCore.Mvc;
using Meridian.Gateway.Models;
using Meridian.Gateway.Services;

namespace Meridian.Gateway.Controllers;

[ApiController]
public class ReportsController : ControllerBase
{
    private readonly ReportService _reports;

    public ReportsController(ReportService reports) => _reports = reports;

    [HttpGet("api/reports/daily")]
    public async Task<ReportResponse> DailyReport()
    {
        return await _reports.DailyReport();
    }
}
