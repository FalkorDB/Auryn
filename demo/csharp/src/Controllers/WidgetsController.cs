using Acme.Widgets.Models;
using Acme.Widgets.Services;
using Microsoft.AspNetCore.Mvc;

namespace Acme.Widgets.Controllers
{
    [ApiController]
    [Route("api/[controller]")]
    public class WidgetsController : ControllerBase
    {
        private readonly IWidgetService _service;

        public WidgetsController(IWidgetService service)
        {
            _service = service;
        }

        [HttpGet("{id}")]
        public WidgetDto GetById(int id)
        {
            return _service.GetById(id);
        }

        [HttpPost]
        public WidgetDto Create([FromBody] CreateWidgetRequest request)
        {
            return _service.Create(request);
        }

        [HttpPut("{id}")]
        public WidgetDto Update(int id, [FromBody] UpdateWidgetRequest request)
        {
            return _service.Update(id, request);
        }
    }
}
