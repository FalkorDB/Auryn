using System;

namespace Acme.Widgets.Models
{
    public abstract class BaseEntity
    {
        public int Id { get; set; }
        public DateTime CreatedAt { get; set; }
    }

    public class Widget : BaseEntity
    {
        public string Name { get; set; }
        public decimal Price { get; set; }
        public WidgetStatus Status { get; set; }
    }

    public enum WidgetStatus : byte
    {
        Active = 1,
        Retired = 2,
    }

    public readonly record struct Money(decimal Amount, string Currency);

    public delegate void WidgetChangedHandler(Widget widget, WidgetStatus newStatus);
}
