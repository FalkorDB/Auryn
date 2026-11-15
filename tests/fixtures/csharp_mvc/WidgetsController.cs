using Sample.Web.Mvc;

namespace Sample.Web
{
    public class WidgetDto
    {
        public int Id { get; set; }
        public string Name { get; set; }
    }

    // The C# controller the Kendo view's grid/form point at. Its actions are the
    // targets of the UI seam edges (DISPLAYS_DATA / CALLS_API / SUBMITS_TO).
    public class WidgetsController : Controller
    {
        public ActionResult Index()
        {
            return new ActionResult();
        }

        public JsonResult List()
        {
            return new JsonResult();
        }

        public JsonResult Save(WidgetDto dto)
        {
            return new JsonResult();
        }
    }
}
