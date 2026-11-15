using System.Collections.Generic;
using Acme.Widgets.Models;

namespace Acme.Widgets.Services
{
    public interface IWidgetRepository
    {
        Widget FindById(int id);
        void Save(Widget widget);
    }

    public class WidgetRepository : IWidgetRepository
    {
        private readonly Dictionary<int, Widget> _widgets = new Dictionary<int, Widget>();
        private int _nextId = 1;

        public Widget FindById(int id)
        {
            return _widgets[id];
        }

        public void Save(Widget widget)
        {
            if (widget.Id == 0)
            {
                widget.Id = _nextId++;
            }
            _widgets[widget.Id] = widget;
        }
    }

    public interface IWidgetService
    {
        WidgetDto GetById(int id);
        WidgetDto Create(CreateWidgetRequest request);
        WidgetDto Update(int id, UpdateWidgetRequest request);
    }

    public class WidgetService : IWidgetService
    {
        private readonly IWidgetRepository _repository;

        public WidgetService(IWidgetRepository repository)
        {
            _repository = repository;
        }

        public WidgetDto GetById(int id)
        {
            var widget = _repository.FindById(id);
            return ToDto(widget);
        }

        public WidgetDto Create(CreateWidgetRequest request)
        {
            var widget = new Widget { Name = request.Name, Price = request.Price };
            _repository.Save(widget);
            return ToDto(widget);
        }

        public WidgetDto Update(int id, UpdateWidgetRequest request)
        {
            var widget = _repository.FindById(id);
            widget.Name = request.Name;
            _repository.Save(widget);
            return ToDto(widget);
        }

        private WidgetDto ToDto(Widget widget)
        {
            return new WidgetDto { Id = widget.Id, Name = widget.Name, Price = widget.Price };
        }
    }
}
