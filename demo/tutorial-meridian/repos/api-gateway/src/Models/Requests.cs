namespace Meridian.Gateway.Models;

public record OrderRequest(int AccountId, double Amount, string Currency = "USD");

public record RefundRequest(string TxnId);

public record PromoRequest(string Code, double Amount);
