namespace Acme.Widgets.Models
{
    public class WidgetDto
    {
        public int Id { get; set; }
        public string Name { get; set; }
        public decimal Price { get; set; }
    }

    public class CreateWidgetRequest
    {
        public string Name { get; set; }
        public decimal Price { get; set; }
    }

    public class UpdateWidgetRequest
    {
        public string Name { get; set; }
    }
}
