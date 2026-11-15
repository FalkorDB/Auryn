namespace Meridian.Gateway.Models;

public class OrderResponse
{
    public string Result { get; set; } = "";
}

public class RefundResponse
{
    public string Refunded { get; set; } = "";
}

public class ReportResponse
{
    public List<string> Lines { get; set; } = new();
}
