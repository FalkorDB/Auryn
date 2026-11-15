using Sample.Framework;

namespace Sample.App
{
    // Interface + single implementation, registered in DI below. The extractor must
    // emit IMPLEMENTS(Greeter -> IGreeter) and RESOLVES_TO(IGreeter -> Greeter).
    public interface IGreeter
    {
        string Greet(string name);
    }

    public class Greeter : IGreeter
    {
        public string Greet(string name) => "Hello, " + name;
    }

    // The DI-resolution payoff: GreetingService holds IGreeter (an interface), and
    // calls Greet on it. Static analysis sees a call to IGreeter.Greet; the extractor
    // must additionally synthesize a di-resolved CALLS to Greeter.Greet (the single
    // registered implementation).
    public class GreetingService
    {
        private readonly IGreeter _greeter;

        public GreetingService(IGreeter greeter)
        {
            _greeter = greeter;
        }

        public string Run(string name)
        {
            return _greeter.Greet(name);
        }
    }

    public class GreetingRequest
    {
        public string Name { get; set; }
    }

    public class GreetingResponse
    {
        public string Message { get; set; }
    }

    // Controller-like (ApiController + ControllerBase) with one HTTP endpoint whose
    // parameter and return types must be classified DTO and linked ACCEPTS/RETURNS.
    [ApiController]
    [Route("api/[controller]")]
    public class GreetController : ControllerBase
    {
        private readonly GreetingService _service;

        public GreetController(GreetingService service)
        {
            _service = service;
        }

        [HttpGet]
        public GreetingResponse Get(GreetingRequest request)
        {
            return new GreetingResponse { Message = _service.Run(request.Name) };
        }
    }

    // The composition root: a DI registration the extractor must turn into a
    // DIRegistration node + REGISTERS + RESOLVES_TO edges.
    public static class Startup
    {
        public static void ConfigureServices(IServiceCollection services)
        {
            services.AddScoped<IGreeter, Greeter>();
        }
    }
}
