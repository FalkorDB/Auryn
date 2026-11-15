using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp.Syntax;

namespace RoslynExtractor;

// Walks a resolved CSharpCompilation and emits graph nodes/edges. Everything that
// used to be a tree-sitter heuristic (EXTENDS-vs-IMPLEMENTS, call resolution, DTO/
// endpoint classification) is now driven by the semantic model, plus the new
// DI-resolution capability the requirements demand.
internal sealed class Extractor
{
    private sealed class TypeRec
    {
        public required INamedTypeSymbol Symbol;
        public required string TypeId;
        public required string ScopeId;
        public required string? ScopeLabel;   // "Namespace" | "Project" for top-level; null when nested
        public required string? ParentTypeId;  // set when nested
        public required string Namespace;
        public required string FilePath;
        public string FinalLabel = "";         // computed after classification
    }

    private static readonly Dictionary<string, string> HttpVerbAttributes = new()
    {
        ["HttpGet"] = "GET", ["HttpPost"] = "POST", ["HttpPut"] = "PUT", ["HttpDelete"] = "DELETE",
        ["HttpPatch"] = "PATCH", ["HttpHead"] = "HEAD", ["HttpOptions"] = "OPTIONS",
    };
    private static readonly HashSet<string> ControllerBaseNames = new() { "Controller", "ControllerBase" };
    private static readonly HashSet<string> DiRegistrationMethods = new()
    {
        "AddScoped", "AddSingleton", "AddTransient",
        "TryAddScoped", "TryAddSingleton", "TryAddTransient",
    };

    private readonly Compilation _compilation;
    private readonly string _projectId;
    private readonly string? _solutionName;
    private readonly string[] _dtoSuffixes;
    private readonly IReadOnlyList<string> _filePaths;
    private readonly Output _output = new();

    private readonly Dictionary<INamedTypeSymbol, TypeRec> _types = new(SymbolEqualityComparer.Default);
    private readonly Dictionary<INamedTypeSymbol, List<INamedTypeSymbol>> _di = new(SymbolEqualityComparer.Default);
    private readonly HashSet<string> _emittedExterns = new();  // ADR 0004 placeholder ids already emitted (dedup)
    private readonly HashSet<string> _httpEndpoints = new();   // ADR 0006 route-keyed endpoint ids already emitted

    // HttpClient call idioms -> verb. Detected precisely here (by the receiver's type), unlike the
    // tree-sitter engine's name+URL-shape heuristic; both emit the same route-keyed placeholder.
    private static readonly Dictionary<string, string> HttpVerbs = new()
    {
        ["GetAsync"] = "GET", ["GetStringAsync"] = "GET", ["GetByteArrayAsync"] = "GET",
        ["GetStreamAsync"] = "GET", ["GetFromJsonAsync"] = "GET", ["PostAsync"] = "POST",
        ["PostAsJsonAsync"] = "POST", ["PutAsync"] = "PUT", ["PutAsJsonAsync"] = "PUT",
        ["DeleteAsync"] = "DELETE", ["DeleteFromJsonAsync"] = "DELETE", ["PatchAsync"] = "PATCH",
        ["PatchAsJsonAsync"] = "PATCH",
    };

    public Extractor(Compilation compilation, string projectId, string? solutionName, string[] dtoSuffixes, IReadOnlyList<string> filePaths)
    {
        _compilation = compilation;
        _projectId = projectId;
        _solutionName = solutionName;
        _dtoSuffixes = dtoSuffixes;
        _filePaths = filePaths;
    }

    public Output Run()
    {
        CollectTypes(_compilation.GlobalNamespace);
        _output.Metrics.TypesResolved = _types.Count;

        ScanDiRegistrations();
        var controllerLike = ComputeControllerLike();
        var definitiveDtos = DefinitiveDtoTypes(controllerLike);
        foreach (var rec in _types.Values)
            rec.FinalLabel = FinalLabel(rec, definitiveDtos);

        EmitFiles();
        EmitNamespaces();
        foreach (var rec in _types.Values)
            EmitTypeAndMembers(rec);

        foreach (var rec in _types.Values)
        {
            EmitExtendsImplements(rec);
            EmitDependsOn(rec);
            EmitCalls(rec);
            EmitEndpoints(rec, controllerLike, definitiveDtos);
        }
        EmitDiRegistrations();
        return _output;
    }

    // -- Collection ----------------------------------------------------------------

    private void CollectTypes(INamespaceSymbol ns)
    {
        foreach (var type in ns.GetTypeMembers())
            CollectType(type);
        foreach (var child in ns.GetNamespaceMembers())
            CollectTypes(child);
    }

    private void CollectType(INamedTypeSymbol type)
    {
        if (!IsInSource(type))
            return;
        var nsName = type.ContainingNamespace.IsGlobalNamespace ? "" : type.ContainingNamespace.ToDisplayString();
        string scopeId, typeId;
        string? scopeLabel = null, parentTypeId = null;
        if (type.ContainingType is not null && _types.TryGetValue(type.ContainingType, out var parent))
        {
            scopeId = parent.TypeId;
            parentTypeId = parent.TypeId;
            typeId = Ids.TypeIdFromScope(parent.TypeId, type);
        }
        else
        {
            scopeId = Ids.NamespaceId(_projectId, nsName);
            scopeLabel = string.IsNullOrEmpty(nsName) ? "Project" : "Namespace";
            typeId = Ids.TypeIdFromScope(scopeId, type);
        }

        _types[type] = new TypeRec
        {
            Symbol = type,
            TypeId = typeId,
            ScopeId = scopeId,
            ScopeLabel = scopeLabel,
            ParentTypeId = parentTypeId,
            Namespace = nsName,
            FilePath = PrimaryFile(type),
        };
        foreach (var nested in type.GetTypeMembers())
            CollectType(nested);
    }

    // -- DI registration scan ------------------------------------------------------

    private void ScanDiRegistrations()
    {
        foreach (var tree in _compilation.SyntaxTrees)
        {
            var model = _compilation.GetSemanticModel(tree);
            foreach (var invocation in tree.GetRoot().DescendantNodes().OfType<InvocationExpressionSyntax>())
            {
                if (invocation.Expression is not MemberAccessExpressionSyntax member)
                    continue;
                var methodName = member.Name.Identifier.ValueText;
                if (!DiRegistrationMethods.Contains(methodName))
                    continue;
                var (service, impl) = ResolveRegistrationTypes(invocation, member, model);
                if (service is null || impl is null || !IsInSource(impl))
                    continue;
                if (!_di.TryGetValue(service, out var impls))
                    _di[service] = impls = new List<INamedTypeSymbol>();
                if (!impls.Contains(impl, SymbolEqualityComparer.Default))
                    impls.Add(impl);

                var lifetime = methodName.Replace("TryAdd", "Add").Substring("Add".Length); // Scoped/Singleton/Transient
                var (file, line, _) = Span(invocation);
                var regId = $"{_projectId}/di/{service.Name.ToLowerInvariant()}-{impl.Name.ToLowerInvariant()}@{file}:{line}";
                _pendingDi.Add((regId, lifetime, service, impl, file, line));
            }
        }
        _output.Metrics.DiRegistrations = _pendingDi.Count;
    }

    private readonly List<(string RegId, string Lifetime, INamedTypeSymbol Service, INamedTypeSymbol Impl, string File, int Line)> _pendingDi = new();

    private (INamedTypeSymbol?, INamedTypeSymbol?) ResolveRegistrationTypes(
        InvocationExpressionSyntax invocation, MemberAccessExpressionSyntax member, SemanticModel model)
    {
        // Generic form: services.AddScoped<IService, TImpl>()
        if (member.Name is GenericNameSyntax generic && generic.TypeArgumentList.Arguments.Count == 2)
        {
            var service = model.GetTypeInfo(generic.TypeArgumentList.Arguments[0]).Type as INamedTypeSymbol;
            var impl = model.GetTypeInfo(generic.TypeArgumentList.Arguments[1]).Type as INamedTypeSymbol;
            return (service, impl);
        }
        // typeof form: services.AddScoped(typeof(IService), typeof(TImpl))
        var args = invocation.ArgumentList.Arguments;
        if (args.Count == 2
            && args[0].Expression is TypeOfExpressionSyntax t0
            && args[1].Expression is TypeOfExpressionSyntax t1)
        {
            var service = model.GetTypeInfo(t0.Type).Type as INamedTypeSymbol;
            var impl = model.GetTypeInfo(t1.Type).Type as INamedTypeSymbol;
            return (service, impl);
        }
        return (null, null);
    }

    // -- Classification ------------------------------------------------------------

    private HashSet<INamedTypeSymbol> ComputeControllerLike()
    {
        var result = new HashSet<INamedTypeSymbol>(SymbolEqualityComparer.Default);
        foreach (var rec in _types.Values)
        {
            var hasAttr = rec.Symbol.GetAttributes().Any(a => a.AttributeClass?.Name is "ApiControllerAttribute" or "ApiController");
            var hasBase = BaseChain(rec.Symbol).Any(b => ControllerBaseNames.Contains(b.Name));
            if (hasAttr || hasBase)
                result.Add(rec.Symbol);
        }
        return result;
    }

    private HashSet<INamedTypeSymbol> DefinitiveDtoTypes(HashSet<INamedTypeSymbol> controllerLike)
    {
        var dtos = new HashSet<INamedTypeSymbol>(SymbolEqualityComparer.Default);
        foreach (var rec in _types.Values)
        {
            if (!controllerLike.Contains(rec.Symbol))
                continue;
            foreach (var method in Methods(rec.Symbol))
            {
                if (HttpVerb(method) is null)
                    continue;
                foreach (var referenced in ReferencedSourceTypes(method.ReturnType).Concat(method.Parameters.SelectMany(p => ReferencedSourceTypes(p.Type))))
                    dtos.Add(referenced);
            }
        }
        return dtos;
    }

    private string FinalLabel(TypeRec rec, HashSet<INamedTypeSymbol> definitiveDtos)
    {
        var kind = KindLabel(rec.Symbol);
        if (kind is not ("Class" or "Record"))
            return kind;
        if (definitiveDtos.Contains(rec.Symbol))
            return "DTO";
        if (_dtoSuffixes.Any(s => rec.Symbol.Name.EndsWith(s, StringComparison.Ordinal)))
            return "DTO";
        return kind;
    }

    // -- Emission: nodes -----------------------------------------------------------

    private void EmitFiles()
    {
        foreach (var path in _filePaths)
        {
            var fileId = Ids.FileId(_projectId, path);
            _output.Nodes.Add(new Node("File", fileId, new() { ["name"] = System.IO.Path.GetFileName(path), ["path"] = path }));
            _output.Edges.Add(new Edge("HAS_FILE", "Project", _projectId, "File", fileId, new()));
        }
    }

    private void EmitNamespaces()
    {
        var emitted = new HashSet<string>();
        foreach (var ns in _types.Values.Select(r => r.Namespace).Where(n => n.Length > 0).Distinct())
        {
            string prefix = "", parentId = _projectId, parentLabel = "Project";
            foreach (var segment in ns.Split('.'))
            {
                prefix = prefix.Length == 0 ? segment : $"{prefix}.{segment}";
                var nsId = Ids.NamespaceId(_projectId, prefix);
                if (emitted.Add(nsId))
                {
                    _output.Nodes.Add(new Node("Namespace", nsId, new() { ["name"] = segment }));
                    _output.Edges.Add(new Edge("HAS_NAMESPACE", parentLabel, parentId, "Namespace", nsId, new()));
                }
                parentId = nsId;
                parentLabel = "Namespace";
            }
        }
    }

    private void EmitTypeAndMembers(TypeRec rec)
    {
        _output.Nodes.Add(new Node(rec.FinalLabel, rec.TypeId, TypeProperties(rec)));

        if (rec.ParentTypeId is not null)
        {
            var parentLabel = _types[rec.Symbol.ContainingType!].FinalLabel;
            _output.Edges.Add(new Edge("HAS_NESTED_TYPE", parentLabel, rec.ParentTypeId, rec.FinalLabel, rec.TypeId, new()));
        }
        else
        {
            _output.Edges.Add(new Edge("HAS_TYPE", rec.ScopeLabel!, rec.ScopeId, rec.FinalLabel, rec.TypeId, new()));
        }
        var fileId = Ids.FileId(_projectId, rec.FilePath);
        _output.Edges.Add(new Edge("DEFINED_IN", rec.FinalLabel, rec.TypeId, "File", fileId, new()));

        foreach (var method in Methods(rec.Symbol))
            EmitMethod(rec, method);
        foreach (var property in rec.Symbol.GetMembers().OfType<IPropertySymbol>().Where(p => !p.IsImplicitlyDeclared))
            EmitProperty(rec, property);
        foreach (var field in rec.Symbol.GetMembers().OfType<IFieldSymbol>().Where(f => !f.IsImplicitlyDeclared && !f.IsConst))
            EmitField(rec, field);
        foreach (var evt in rec.Symbol.GetMembers().OfType<IEventSymbol>().Where(e => !e.IsImplicitlyDeclared))
            EmitEvent(rec, evt);
        if (rec.Symbol.TypeKind == TypeKind.Enum)
            foreach (var member in rec.Symbol.GetMembers().OfType<IFieldSymbol>().Where(f => f.IsConst))
                EmitEnumMember(rec, member);
    }

    private void EmitMethod(TypeRec rec, IMethodSymbol method)
    {
        var methodId = Ids.MethodId(rec.TypeId, method);
        var (file, start, end) = Span(method);
        var props = WithSpan(new Dictionary<string, object?>
        {
            ["name"] = Ids.MethodName(method),
            ["kind"] = MethodKindLabel(method),
            ["accessibility"] = Accessibility(method),
            ["is_static"] = method.IsStatic,
            ["is_abstract"] = method.IsAbstract,
            ["is_virtual"] = method.IsVirtual,
            ["is_override"] = method.IsOverride,
            ["is_async"] = method.IsAsync,
            ["return_type"] = method.ReturnsVoid ? "void" : Ids.TypeDisplay(method.ReturnType),
            ["source"] = DeclText(method),
        }, file, start, end);
        // ADR 0004: an externally-visible method carries its project-independent linkage key, so a
        // consumer repo's placeholder for the same call heals onto it when they share a graph.
        if (IsExternallyVisible(method))
            props["linkage_key"] = Ids.ExternMethodKey(Ids.MethodQualifiedName(method));
        _output.Nodes.Add(new Node("Method", methodId, props));
        _output.Edges.Add(new Edge("HAS_METHOD", rec.FinalLabel, rec.TypeId, "Method", methodId, new()));
        for (var i = 0; i < method.Parameters.Length; i++)
            EmitParameter(methodId, "Method", method.Parameters[i], i);
    }

    private void EmitParameter(string parentId, string parentLabel, IParameterSymbol param, int position)
    {
        var paramId = Ids.ParameterId(parentId, position, param.Name);
        _output.Nodes.Add(new Node("Parameter", paramId, new()
        {
            ["name"] = param.Name,
            ["type"] = Ids.TypeDisplay(param.Type),
            ["direction"] = ParamDirection(param),
            ["position"] = position + 1,
            ["has_default"] = param.HasExplicitDefaultValue,
        }));
        _output.Edges.Add(new Edge("HAS_PARAMETER", parentLabel, parentId, "Parameter", paramId, new()));
    }

    private void EmitProperty(TypeRec rec, IPropertySymbol property)
    {
        var propId = Ids.PropertyId(rec.TypeId, property.Name);
        var (file, start, end) = Span(property);
        _output.Nodes.Add(new Node("Property", propId, WithSpan(new()
        {
            ["name"] = property.Name,
            ["accessibility"] = Accessibility(property),
            ["type"] = Ids.TypeDisplay(property.Type),
            ["has_getter"] = property.GetMethod is not null,
            ["has_setter"] = property.SetMethod is not null,
            ["is_auto_implemented"] = IsAutoProperty(property),
            ["is_static"] = property.IsStatic,
        }, file, start, end)));
        _output.Edges.Add(new Edge("HAS_PROPERTY", rec.FinalLabel, rec.TypeId, "Property", propId, new()));
    }

    private void EmitField(TypeRec rec, IFieldSymbol field)
    {
        var fieldId = Ids.FieldId(rec.TypeId, field.Name);
        var (file, start, end) = Span(field);
        _output.Nodes.Add(new Node("Field", fieldId, WithSpan(new()
        {
            ["name"] = field.Name,
            ["accessibility"] = Accessibility(field),
            ["type"] = Ids.TypeDisplay(field.Type),
            ["is_readonly"] = field.IsReadOnly,
            ["is_const"] = field.IsConst,
            ["is_static"] = field.IsStatic,
        }, file, start, end)));
        _output.Edges.Add(new Edge("HAS_FIELD", rec.FinalLabel, rec.TypeId, "Field", fieldId, new()));
    }

    private void EmitEvent(TypeRec rec, IEventSymbol evt)
    {
        var eventId = Ids.EventId(rec.TypeId, evt.Name);
        _output.Nodes.Add(new Node("Event", eventId, new()
        {
            ["name"] = evt.Name,
            ["accessibility"] = Accessibility(evt),
            ["type"] = Ids.TypeDisplay(evt.Type),
            ["is_static"] = evt.IsStatic,
        }));
        _output.Edges.Add(new Edge("HAS_EVENT", rec.FinalLabel, rec.TypeId, "Event", eventId, new()));
    }

    private void EmitEnumMember(TypeRec rec, IFieldSymbol member)
    {
        var memberId = Ids.EnumMemberId(rec.TypeId, member.Name);
        _output.Nodes.Add(new Node("EnumMember", memberId, new()
        {
            ["name"] = member.Name,
            ["value"] = member.ConstantValue?.ToString(),
        }));
        _output.Edges.Add(new Edge("HAS_ENUM_MEMBER", "Enum", rec.TypeId, "EnumMember", memberId, new()));
    }

    // -- Emission: cross-reference edges -------------------------------------------

    private void EmitExtendsImplements(TypeRec rec)
    {
        var symbol = rec.Symbol;
        if (symbol.TypeKind == TypeKind.Interface)
        {
            foreach (var iface in symbol.Interfaces.Where(IsInSource))
                Edge2("EXTENDS", rec, iface);
            return;
        }
        if (symbol.BaseType is { } baseType && IsInSource(baseType) && baseType.SpecialType == SpecialType.None)
            Edge2("EXTENDS", rec, baseType);
        foreach (var iface in symbol.Interfaces.Where(IsInSource))
            Edge2("IMPLEMENTS", rec, iface);
    }

    private void Edge2(string relType, TypeRec from, INamedTypeSymbol target)
    {
        if (_types.TryGetValue(target, out var targetRec))
            _output.Edges.Add(new Edge(relType, from.FinalLabel, from.TypeId, targetRec.FinalLabel, targetRec.TypeId, new()));
    }

    private void EmitDependsOn(TypeRec rec)
    {
        foreach (var method in Methods(rec.Symbol))
        {
            var methodId = Ids.MethodId(rec.TypeId, method);
            var targets = ReferencedSourceTypes(method.ReturnType).Concat(method.Parameters.SelectMany(p => ReferencedSourceTypes(p.Type)));
            DependsOnEdges("Method", methodId, rec.TypeId, targets);
        }
        foreach (var property in rec.Symbol.GetMembers().OfType<IPropertySymbol>().Where(p => !p.IsImplicitlyDeclared))
            DependsOnEdges("Property", Ids.PropertyId(rec.TypeId, property.Name), rec.TypeId, ReferencedSourceTypes(property.Type));
        foreach (var field in rec.Symbol.GetMembers().OfType<IFieldSymbol>().Where(f => !f.IsImplicitlyDeclared && !f.IsConst))
            DependsOnEdges("Field", Ids.FieldId(rec.TypeId, field.Name), rec.TypeId, ReferencedSourceTypes(field.Type));
    }

    private void DependsOnEdges(string fromLabel, string fromId, string ownTypeId, IEnumerable<INamedTypeSymbol> targets)
    {
        var seen = new HashSet<string>();
        foreach (var target in targets)
        {
            if (!_types.TryGetValue(target, out var targetRec) || targetRec.TypeId == ownTypeId)
                continue;
            if (seen.Add(targetRec.TypeId))
                _output.Edges.Add(new Edge("DEPENDS_ON", fromLabel, fromId, targetRec.FinalLabel, targetRec.TypeId, new()));
        }
    }

    // Aggregated call metadata for one caller->callee pair, unioned over every call
    // site (the graph keeps a single CALLS edge per pair, so per-site properties would
    // otherwise be lost to dedup). Generic and language-level: no knowledge of any
    // specific API -- consumers (e.g. the routing ingestor) interpret these.
    private sealed class CallAgg
    {
        public readonly SortedSet<string> ArgumentTypes = new(StringComparer.Ordinal);
        public readonly SortedSet<string> TypeArguments = new(StringComparer.Ordinal);

        public Dictionary<string, object?> ToProperties()
        {
            var props = new Dictionary<string, object?>();
            if (ArgumentTypes.Count > 0)
                props["argument_types"] = ArgumentTypes.ToList();
            if (TypeArguments.Count > 0)
                props["type_arguments"] = TypeArguments.ToList();
            return props;
        }
    }

    private void EmitCalls(TypeRec rec)
    {
        foreach (var method in Methods(rec.Symbol))
        {
            var callerId = Ids.MethodId(rec.TypeId, method);
            var direct = new Dictionary<string, CallAgg>();    // in-source targets -> CALLS
            var diResolved = new Dictionary<string, CallAgg>();  // interface->impl -> CALLS (di-resolved)
            var external = new Dictionary<string, CallAgg>();  // BCL / out-of-project -> CALLS (external)

            foreach (var syntaxRef in method.DeclaringSyntaxReferences)
            {
                var node = syntaxRef.GetSyntax();
                var model = _compilation.GetSemanticModel(node.SyntaxTree);
                foreach (var invocation in node.DescendantNodes().OfType<InvocationExpressionSyntax>())
                {
                    if (model.GetSymbolInfo(invocation).Symbol is not IMethodSymbol target)
                    {
                        _output.Metrics.UnresolvedCalls++;
                        continue;
                    }
                    if (TryEmitHttpCall(callerId, target, invocation))
                        continue;  // ADR 0006: an HttpClient call -> the route seam, not a CALLS
                    AccumulateCall(target, invocation, model, direct, diResolved, external);
                }
            }

            foreach (var (toId, agg) in direct)
                _output.Edges.Add(new Edge("CALLS", "Method", callerId, "Method", toId, agg.ToProperties()));
            foreach (var (toId, agg) in diResolved)
            {
                var props = agg.ToProperties();
                props["provenance"] = "di-resolved";
                _output.Edges.Add(new Edge("CALLS", "Method", callerId, "Method", toId, props));
            }
            // ADR 0004: an unresolved-but-nameable callee (BCL / a repo not in this ingest) becomes
            // a placeholder Method reached by an ordinary CALLS edge -- provenance "external", which
            // roslyn.py normalizes to the shared `resolution` key. A later ingest of the defining
            // repo heals it onto the real definition (core/linkage.py).
            foreach (var (toId, agg) in external)
            {
                var props = agg.ToProperties();
                props["provenance"] = "external";
                _output.Edges.Add(new Edge("CALLS", "Method", callerId, "Method", toId, props));
            }
        }
    }

    // ADR 0006: an HttpClient call becomes `caller -CALLS_API-> APIEndpoint{resolved:false}`, a
    // route-keyed placeholder (same key scheme as core.http_seam, so C# and TS callers converge).
    private bool TryEmitHttpCall(string callerId, IMethodSymbol target, InvocationExpressionSyntax invocation)
    {
        if (!HttpVerbs.TryGetValue(target.Name, out var verb))
            return false;
        var receiver = target.IsExtensionMethod ? (target.ReceiverType ?? target.ContainingType) : target.ContainingType;
        if (receiver?.Name != "HttpClient")
            return false;
        var url = FirstStringArgument(invocation);
        if (url is null || !LooksLikeRoute(url))
            return false;
        var route = NormalizeRoute(url);
        if (route.Length == 0)
            return false;
        var endpointId = $"extern://http/{verb}:{route}";
        if (_httpEndpoints.Add(endpointId))
            _output.Nodes.Add(new Node("APIEndpoint", endpointId, new()
            {
                ["name"] = $"{verb} {route}",
                ["route"] = route,
                ["http_method"] = verb,
                ["resolved"] = false,
                ["provenance"] = "http-derived",
            }));
        _output.Edges.Add(new Edge("CALLS_API", "Method", callerId, "APIEndpoint", endpointId, new()
        {
            ["resolution"] = "name",
            ["provenance"] = "http-derived",
        }));
        return true;
    }

    private static string? FirstStringArgument(InvocationExpressionSyntax invocation)
    {
        foreach (var arg in invocation.ArgumentList.Arguments)
        {
            if (arg.Expression is LiteralExpressionSyntax lit && lit.Token.Value is string s)
                return s;
            if (arg.Expression is InterpolatedStringExpressionSyntax interp)
                return InterpolatedRoute(interp);
        }
        return null;
    }

    // `$"api/widgets/{id}"` -> `api/widgets/{param}` so an interpolated URL matches a route template.
    private static string InterpolatedRoute(InterpolatedStringExpressionSyntax interp)
    {
        var route = "";
        foreach (var content in interp.Contents)
        {
            if (content is InterpolatedStringTextSyntax text)
                route += text.TextToken.ValueText;
            else if (content is InterpolationSyntax)
                route += "{param}";
        }
        return route;
    }

    private static bool LooksLikeRoute(string url)
    {
        var u = url.Trim();
        return u.StartsWith("/") || u.StartsWith("http://") || u.StartsWith("https://") || u.Contains("/");
    }

    private static string NormalizeRoute(string url)
    {
        var route = url;
        var cut = route.IndexOfAny(new[] { '?', '#' });
        if (cut >= 0)
            route = route.Substring(0, cut);
        route = route.Trim();
        if (route.Length > 1)
            route = route.TrimEnd('/');
        route = route.ToLowerInvariant();
        // Root a bare path so a relative endpoint route ("api/x") and an absolute call ("/api/x")
        // key identically and merge (mirror of core.http_seam.normalize_route).
        if (route.Length > 0 && !route.StartsWith("/") && !route.StartsWith("http://") && !route.StartsWith("https://"))
            route = "/" + route;
        return route;
    }

    private void AccumulateCall(
        IMethodSymbol target,
        InvocationExpressionSyntax invocation,
        SemanticModel model,
        Dictionary<string, CallAgg> direct,
        Dictionary<string, CallAgg> diResolved,
        Dictionary<string, CallAgg> external)
    {
        // Argument value types (from the argument expressions) and generic type
        // arguments (from the resolved -- possibly inferred -- constructed method).
        var argumentTypes = invocation.ArgumentList.Arguments
            .Select(a => model.GetTypeInfo(a.Expression).Type)
            .Where(t => t is not null)
            .Select(t => Ids.TypeDisplay(t!))
            .ToList();
        var typeArguments = target.TypeArguments.Select(Ids.TypeDisplay).ToList();

        // Normalize to the method as declared: a reduced extension method
        // (services.AddScoped<..>()) drops its `this` parameter, and a constructed
        // generic method carries substituted type args -- either would build a method
        // id that doesn't match the node emitted from the type's own members.
        var canonical = target.ReducedFrom ?? target.OriginalDefinition;
        var targetType = canonical.ContainingType;
        if (targetType is null)
            return;

        if (_types.TryGetValue(targetType, out var targetRec))
        {
            Accumulate(direct, Ids.MethodId(targetRec.TypeId, canonical), argumentTypes, typeArguments);
        }
        else
        {
            // Target type isn't in this ingest (BCL / another repo) -> a placeholder callee keyed
            // by its cross-repo linkage. The semantic model gives a precise fully-qualified name.
            var key = Ids.ExternMethodKey(Ids.MethodQualifiedName(canonical));
            Accumulate(external, key, argumentTypes, typeArguments);
            RecordExternalPlaceholder(key, canonical);
        }

        // DI resolution: a call whose static target is an interface method resolves to
        // the concrete implementation(s) the container is configured to hand out.
        if (targetType.TypeKind == TypeKind.Interface && _di.TryGetValue(targetType, out var impls))
        {
            foreach (var impl in impls)
            {
                if (impl.FindImplementationForInterfaceMember(canonical) is IMethodSymbol implMethod
                    && _types.TryGetValue(implMethod.ContainingType, out var implRec))
                {
                    Accumulate(diResolved, Ids.MethodId(implRec.TypeId, implMethod.OriginalDefinition), argumentTypes, typeArguments);
                    _output.Metrics.DiResolvedCalls++;
                }
            }
        }
    }

    private static void Accumulate(
        Dictionary<string, CallAgg> map, string toId, List<string> argumentTypes, List<string> typeArguments)
    {
        if (!map.TryGetValue(toId, out var agg))
            map[toId] = agg = new CallAgg();
        agg.ArgumentTypes.UnionWith(argumentTypes);
        agg.TypeArguments.UnionWith(typeArguments);
    }

    // ADR 0004: a shallow placeholder Method for an unresolved call, keyed by its cross-repo
    // linkage id and deduped across the whole extraction (many callers MERGE onto one node). Shape
    // mirrors a real Method so the same CALLS queries reach it; `resolved:false` + `origin` mark it.
    private void RecordExternalPlaceholder(string key, IMethodSymbol method)
    {
        if (!_emittedExterns.Add(key))
            return;
        _output.Nodes.Add(new Node("Method", key, new()
        {
            ["name"] = Ids.MethodName(method),
            ["qualified_name"] = Ids.MethodQualifiedName(method),
            ["resolved"] = false,
            ["origin"] = ExternOrigin(method.ContainingType),
            ["linkage_key"] = key,
        }));
    }

    private void EmitEndpoints(TypeRec rec, HashSet<INamedTypeSymbol> controllerLike, HashSet<INamedTypeSymbol> definitiveDtos)
    {
        if (!controllerLike.Contains(rec.Symbol))
            return;
        var classRoute = RouteTemplate(rec.Symbol.GetAttributes());
        foreach (var method in Methods(rec.Symbol))
        {
            var verb = HttpVerb(method);
            if (verb is null)
                continue;
            var methodRoute = RouteTemplate(method.GetAttributes()) ?? HttpVerbTemplate(method);
            var route = EndpointRoute(classRoute, methodRoute, rec.Symbol.Name);
            if (route is null)
                continue;  // convention-routed action -> resolved by the stitch's /Controller/Action path (mirrors Python _emit_endpoints)
            route = NormalizeRoute(route);
            var methodId = Ids.MethodId(rec.TypeId, method);
            // Route-keyed id (same scheme as the caller placeholder / core.http_seam), NOT
            // {method_id}.endpoint: declaration and call converge on ONE node instead of two (ADR 0006).
            var endpointId = $"extern://http/{verb}:{route}";
            _output.Nodes.Add(new Node("APIEndpoint", endpointId, new()
            {
                ["route"] = route, ["http_method"] = verb, ["name"] = method.Name,
            }));
            _output.Edges.Add(new Edge("EXPOSES_ENDPOINT", "Method", methodId, "APIEndpoint", endpointId, new()));

            foreach (var param in method.Parameters)
                foreach (var dto in ReferencedSourceTypes(param.Type).Where(definitiveDtos.Contains))
                    _output.Edges.Add(new Edge("ACCEPTS", "APIEndpoint", endpointId, "DTO", _types[dto].TypeId, new()));
            foreach (var dto in ReferencedSourceTypes(method.ReturnType).Where(definitiveDtos.Contains))
                _output.Edges.Add(new Edge("RETURNS", "APIEndpoint", endpointId, "DTO", _types[dto].TypeId, new()));
        }
    }

    private void EmitDiRegistrations()
    {
        foreach (var (regId, lifetime, service, impl, file, line) in _pendingDi)
        {
            _output.Nodes.Add(new Node("DIRegistration", regId, new()
            {
                ["service_type"] = service.Name,
                ["implementation_type"] = impl.Name,
                ["lifetime"] = lifetime,
                ["provenance"] = "di-registration",
                ["file"] = file,
                ["line_start"] = line,
            }));
            if (_types.TryGetValue(service, out var serviceRec))
            {
                _output.Edges.Add(new Edge("REGISTERS", "DIRegistration", regId, serviceRec.FinalLabel, serviceRec.TypeId, new() { ["role"] = "service" }));
                if (_types.TryGetValue(impl, out var implRec))
                {
                    _output.Edges.Add(new Edge("REGISTERS", "DIRegistration", regId, implRec.FinalLabel, implRec.TypeId, new() { ["role"] = "implementation" }));
                    _output.Edges.Add(new Edge("RESOLVES_TO", serviceRec.FinalLabel, serviceRec.TypeId, implRec.FinalLabel, implRec.TypeId, new() { ["provenance"] = "di-resolved", ["lifetime"] = lifetime }));
                }
            }
        }
    }

    // -- Helpers -------------------------------------------------------------------

    private static bool IsInSource(ISymbol symbol) => symbol.DeclaringSyntaxReferences.Length > 0;

    // Reachable from another assembly/repo (so worth a cross-repo linkage key). internal/private/
    // private-protected are assembly- or type-scoped -- matches resolution.py's _externally_visible.
    private static bool IsExternallyVisible(ISymbol symbol) =>
        symbol.DeclaredAccessibility is Microsoft.CodeAnalysis.Accessibility.Public
            or Microsoft.CodeAnalysis.Accessibility.Protected
            or Microsoft.CodeAnalysis.Accessibility.ProtectedOrInternal;

    // Coarse provenance for an unresolved callee (ADR 0004): stdlib for the BCL (System.*/
    // Microsoft.*), else external -- the candidate a sibling-repo ingest could later heal.
    private static string ExternOrigin(INamedTypeSymbol type)
    {
        var ns = type.ContainingNamespace is { IsGlobalNamespace: false } n ? n.ToDisplayString() : "";
        var root = ns.Split('.')[0];
        return root is "System" or "Microsoft" ? "stdlib" : "external";
    }

    private static IEnumerable<IMethodSymbol> Methods(INamedTypeSymbol type) =>
        type.GetMembers().OfType<IMethodSymbol>().Where(m =>
            !m.IsImplicitlyDeclared &&
            m.MethodKind is MethodKind.Ordinary or MethodKind.Constructor or MethodKind.Destructor
                or MethodKind.UserDefinedOperator or MethodKind.Conversion);

    private static IEnumerable<INamedTypeSymbol> BaseChain(INamedTypeSymbol type)
    {
        for (var b = type.BaseType; b is not null; b = b.BaseType)
            yield return b;
    }

    private IEnumerable<INamedTypeSymbol> ReferencedSourceTypes(ITypeSymbol? type)
    {
        if (type is null)
            yield break;
        if (type is INamedTypeSymbol named)
        {
            if (IsInSource(named))
                yield return named;
            foreach (var arg in named.TypeArguments)
                foreach (var inner in ReferencedSourceTypes(arg))
                    yield return inner;
        }
        else if (type is IArrayTypeSymbol array)
        {
            foreach (var inner in ReferencedSourceTypes(array.ElementType))
                yield return inner;
        }
    }

    private string KindLabel(INamedTypeSymbol type) => type.TypeKind switch
    {
        TypeKind.Interface => "Interface",
        TypeKind.Struct => "Struct",
        TypeKind.Enum => "Enum",
        TypeKind.Delegate => "Delegate",
        _ => type.IsRecord ? "Record" : "Class",
    };

    private static string MethodKindLabel(IMethodSymbol method) => method.MethodKind switch
    {
        MethodKind.Constructor or MethodKind.StaticConstructor => "CONSTRUCTOR",
        MethodKind.Destructor => "DESTRUCTOR",
        MethodKind.UserDefinedOperator or MethodKind.Conversion => "OPERATOR",
        _ => "ORDINARY",
    };

    private static string Accessibility(ISymbol symbol) => symbol.DeclaredAccessibility switch
    {
        Microsoft.CodeAnalysis.Accessibility.Public => "public",
        Microsoft.CodeAnalysis.Accessibility.Private => "private",
        Microsoft.CodeAnalysis.Accessibility.Protected => "protected",
        Microsoft.CodeAnalysis.Accessibility.Internal => "internal",
        Microsoft.CodeAnalysis.Accessibility.ProtectedOrInternal => "protected internal",
        Microsoft.CodeAnalysis.Accessibility.ProtectedAndInternal => "private protected",
        _ => "internal",
    };

    private static string ParamDirection(IParameterSymbol param) => param.RefKind switch
    {
        RefKind.Ref => "REF",
        RefKind.Out => "OUT",
        RefKind.In => "IN",
        _ => param.IsParams ? "PARAMS" : "VALUE",
    };

    private Dictionary<string, object?> TypeProperties(TypeRec rec)
    {
        var symbol = rec.Symbol;
        var (file, start, end) = Span(symbol);
        var props = new Dictionary<string, object?> { ["name"] = symbol.Name, ["accessibility"] = Accessibility(symbol) };
        if (rec.FinalLabel == "DTO")
        {
            props["is_record"] = symbol.IsRecord;
            props["source"] = DeclText(symbol);
        }
        else
        {
            var typeParams = symbol.TypeParameters.Select(tp => tp.Name).ToList();
            props["type_parameters"] = typeParams;
            props["source"] = DeclText(symbol);
            switch (symbol.TypeKind)
            {
                case TypeKind.Class:
                    props["is_abstract"] = symbol.IsAbstract;
                    props["is_sealed"] = symbol.IsSealed;
                    props["is_static"] = symbol.IsStatic;
                    break;
                case TypeKind.Enum:
                    props["underlying_type"] = symbol.EnumUnderlyingType?.ToDisplayString();
                    break;
                case TypeKind.Struct:
                    props["is_readonly"] = symbol.IsReadOnly;
                    break;
            }
        }
        return WithSpan(props, file, start, end);
    }

    private static Dictionary<string, object?> WithSpan(Dictionary<string, object?> props, string file, int start, int end)
    {
        props["file"] = file;
        props["line_start"] = start;
        props["line_end"] = end;
        return props;
    }

    private bool IsAutoProperty(IPropertySymbol property)
    {
        foreach (var reference in property.DeclaringSyntaxReferences)
            if (reference.GetSyntax() is PropertyDeclarationSyntax decl)
                return decl.ExpressionBody is null
                    && decl.AccessorList?.Accessors.All(a => a.Body is null && a.ExpressionBody is null) == true;
        return false;
    }

    private string PrimaryFile(ISymbol symbol) =>
        symbol.DeclaringSyntaxReferences.Length > 0 ? symbol.DeclaringSyntaxReferences[0].SyntaxTree.FilePath : "";

    private (string File, int Start, int End) Span(ISymbol symbol)
    {
        // Prefer the full declaration syntax span (whole method/type, not just the
        // identifier token, which is all symbol.Locations points at) so line_start..
        // line_end covers the declaration -- the provenance the Coding Agent needs.
        var reference = symbol.DeclaringSyntaxReferences.FirstOrDefault();
        if (reference is not null)
            return Span(reference.GetSyntax());
        var location = symbol.Locations.FirstOrDefault(l => l.IsInSource);
        if (location is null)
            return ("", 0, 0);
        var span = location.GetLineSpan();
        return (location.SourceTree?.FilePath ?? "", span.StartLinePosition.Line + 1, span.EndLinePosition.Line + 1);
    }

    private (string File, int Start, int End) Span(SyntaxNode node)
    {
        var span = node.GetLocation().GetLineSpan();
        return (node.SyntaxTree.FilePath, span.StartLinePosition.Line + 1, span.EndLinePosition.Line + 1);
    }

    private static string DeclText(ISymbol symbol)
    {
        var reference = symbol.DeclaringSyntaxReferences.FirstOrDefault();
        return reference?.GetSyntax().ToString() ?? "";
    }

    private static string? RouteTemplate(IEnumerable<AttributeData> attributes)
    {
        foreach (var attr in attributes)
            if (attr.AttributeClass?.Name is "RouteAttribute" or "Route" && attr.ConstructorArguments.Length > 0)
                return attr.ConstructorArguments[0].Value?.ToString();
        return null;
    }

    private static string? HttpVerbTemplate(IMethodSymbol method)
    {
        foreach (var attr in method.GetAttributes())
            if (HttpVerbAttributes.ContainsKey(StripAttribute(attr.AttributeClass?.Name)) && attr.ConstructorArguments.Length > 0)
                return attr.ConstructorArguments[0].Value?.ToString();
        return null;
    }

    private static string? HttpVerb(IMethodSymbol method)
    {
        foreach (var attr in method.GetAttributes())
            if (HttpVerbAttributes.TryGetValue(StripAttribute(attr.AttributeClass?.Name), out var verb))
                return verb;
        return null;
    }

    private static string StripAttribute(string? name)
    {
        name ??= "";
        return name.EndsWith("Attribute", StringComparison.Ordinal) ? name[..^"Attribute".Length] : name;
    }

    private static string? EndpointRoute(string? classRoute, string? methodRoute, string controllerName)
    {
        var parts = new[] { classRoute, methodRoute }.Where(p => !string.IsNullOrEmpty(p)).Select(p => p!.Trim('/')).ToList();
        if (parts.Count == 0)
            return null;
        var token = controllerName.EndsWith("Controller", StringComparison.Ordinal)
            ? controllerName[..^"Controller".Length]
            : controllerName;
        return string.Join("/", parts).Replace("[controller]", token).Replace("[Controller]", token);
    }
}
