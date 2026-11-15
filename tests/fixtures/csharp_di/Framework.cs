using System;

// Minimal local stand-ins for the DI container and ASP.NET MVC surfaces the Roslyn
// extractor keys off, so the fixture needs no external packages. Names match the real
// framework (IServiceCollection, AddScoped, ControllerBase, ApiController, HttpGet,
// Route) because the extractor recognizes registrations and controllers by name.
namespace Sample.Framework
{
    public interface IServiceCollection { }

    public static class ServiceCollectionExtensions
    {
        public static IServiceCollection AddScoped<TService, TImplementation>(this IServiceCollection services)
            where TImplementation : TService
            => services;

        public static IServiceCollection AddSingleton<TService, TImplementation>(this IServiceCollection services)
            where TImplementation : TService
            => services;
    }

    public abstract class ControllerBase { }

    [AttributeUsage(AttributeTargets.Class)]
    public sealed class ApiControllerAttribute : Attribute { }

    [AttributeUsage(AttributeTargets.Class | AttributeTargets.Method)]
    public sealed class RouteAttribute : Attribute
    {
        public RouteAttribute(string template) { Template = template; }
        public string Template { get; }
    }

    [AttributeUsage(AttributeTargets.Method)]
    public sealed class HttpGetAttribute : Attribute
    {
        public HttpGetAttribute() { }
        public HttpGetAttribute(string template) { Template = template; }
        public string Template { get; }
    }
}
