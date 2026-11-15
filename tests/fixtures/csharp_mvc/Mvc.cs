// Minimal local stand-ins for the ASP.NET MVC surfaces the extractor keys off, so
// the fixture builds offline. A controller is recognized by extending a base named
// `Controller`/`ControllerBase`; actions are just its public methods.
namespace Sample.Web.Mvc
{
    public abstract class Controller { }

    public class ActionResult { }

    public class JsonResult : ActionResult { }
}
