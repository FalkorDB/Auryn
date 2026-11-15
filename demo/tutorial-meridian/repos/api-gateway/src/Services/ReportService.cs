using System.Net.Http.Json;
using Meridian.Gateway.Models;

namespace Meridian.Gateway.Services;

public class ReportService
{
    private readonly HttpClient _http;

    public ReportService(HttpClient http) => _http = http;

    public async Task<ReportResponse> DailyReport()
    {
        // -> platform-core GET /reports/daily
        var resp = await _http.GetAsync("/reports/daily");
        return await resp.Content.ReadFromJsonAsync<ReportResponse>() ?? new ReportResponse();
    }
}
