using Meridian.Gateway.Services;

// The BFF host. Every service gets an HttpClient whose BaseAddress points at platform-core,
// so the services call platform-core with relative routes (/charge, /refund, ...).
var builder = WebApplication.CreateBuilder(args);

var platformUrl = builder.Configuration["PLATFORM_CORE_URL"] ?? "http://platform-core:8000";

builder.Services.AddControllers();
builder.Services.AddHttpClient<OrderService>(c => c.BaseAddress = new Uri(platformUrl));
builder.Services.AddHttpClient<BillingService>(c => c.BaseAddress = new Uri(platformUrl));
builder.Services.AddHttpClient<ReportService>(c => c.BaseAddress = new Uri(platformUrl));

var app = builder.Build();
app.MapControllers();
app.Run();
