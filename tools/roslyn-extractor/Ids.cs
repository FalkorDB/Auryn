using Microsoft.CodeAnalysis;

namespace RoslynExtractor;

// Reproduces, byte-for-byte, the id scheme in
// src/auryn/ingestors/csharp/{config,resolution}.py so Roslyn-produced nodes drop
// into the same graph / delta-sync as the tree-sitter engine. This is the critical
// correctness contract between the two engines.
internal static class Ids
{
    // Simple type name, special types mapped (System.String -> string), generic args
    // kept -- the closest match to the source text tree-sitter records as a type.
    private static readonly SymbolDisplayFormat TypeNameFormat = new(
        typeQualificationStyle: SymbolDisplayTypeQualificationStyle.NameOnly,
        genericsOptions: SymbolDisplayGenericsOptions.IncludeTypeParameters,
        miscellaneousOptions: SymbolDisplayMiscellaneousOptions.UseSpecialTypes);

    // Fully-qualified dotted type name (namespace + nesting, no `global::`, no generic args) --
    // the shape a consumer writes and the tree-sitter engine builds for linkage keys, so both
    // engines agree on a symbol's linkage across repos (see ids.py's extern_method_key).
    private static readonly SymbolDisplayFormat FullyQualifiedTypeFormat = new(
        typeQualificationStyle: SymbolDisplayTypeQualificationStyle.NameAndContainingTypesAndNamespaces,
        genericsOptions: SymbolDisplayGenericsOptions.None,
        globalNamespaceStyle: SymbolDisplayGlobalNamespaceStyle.Omitted);

    public static string ProjectId(string projectName, string? solutionName) =>
        string.IsNullOrEmpty(solutionName) ? $"csharp://{projectName}" : $"csharp://{solutionName}/{projectName}";

    public static string SolutionId(string solutionName) => $"csharp://{solutionName}";

    public static string NamespaceId(string projectId, string ns) =>
        string.IsNullOrEmpty(ns) ? projectId : $"{projectId}/ns/{ns.ToLowerInvariant()}";

    public static string FileId(string projectId, string relativePath) => $"{projectId}/file/{relativePath}";

    public static string KindSlug(INamedTypeSymbol type) => type.TypeKind switch
    {
        TypeKind.Interface => "interface",
        TypeKind.Struct => "struct",
        TypeKind.Enum => "enum",
        TypeKind.Delegate => "delegate",
        _ => type.IsRecord ? "record" : "class",
    };

    public static string AritySuffix(INamedTypeSymbol type) => type.Arity > 0 ? $"`{type.Arity}" : "";

    public static string TypeIdFromScope(string scopeId, INamedTypeSymbol type) =>
        $"{scopeId}.{KindSlug(type)}.{type.Name.ToLowerInvariant()}{AritySuffix(type)}";

    public static string TypeDisplay(ITypeSymbol type) => type.ToDisplayString(TypeNameFormat);

    public static string MethodName(IMethodSymbol method) =>
        method.MethodKind == MethodKind.Constructor || method.MethodKind == MethodKind.StaticConstructor
            ? method.ContainingType.Name
            : method.Name;

    public static string MethodId(string typeId, IMethodSymbol method)
    {
        var signature = string.Join(",", method.Parameters.Select(p => TypeDisplay(p.Type).ToLowerInvariant()));
        return $"{typeId}.method.{MethodName(method).ToLowerInvariant()}({signature})";
    }

    public static string ParameterId(string parentId, int position, string name) =>
        $"{parentId}.param.{position}.{name.ToLowerInvariant()}";

    public static string PropertyId(string typeId, string name) => $"{typeId}.property.{name.ToLowerInvariant()}";

    public static string FieldId(string typeId, string name) => $"{typeId}.field.{name.ToLowerInvariant()}";

    public static string EventId(string typeId, string name) => $"{typeId}.event.{name.ToLowerInvariant()}";

    public static string EnumMemberId(string typeId, string name) => $"{typeId}.member.{name.ToLowerInvariant()}";

    public static string EndpointId(string methodId) => $"{methodId}.endpoint";

    // -- Cross-repo linkage (ADR 0004) ---------------------------------------------
    // Project-INDEPENDENT key: the fully-qualified consumer name (Namespace.Type.Method),
    // name-based (never parameter types, unlike MethodId's overload disambiguation). Byte-identical
    // to src/auryn/ingestors/csharp/ids.py's extern_method_key, so the two engines interoperate.
    public static string ExternMethodKey(string qualifiedName) => $"extern://csharp/{qualifiedName}";

    public static string TypeFullName(INamedTypeSymbol type) => type.ToDisplayString(FullyQualifiedTypeFormat);

    public static string MethodQualifiedName(IMethodSymbol method) =>
        $"{TypeFullName(method.ContainingType)}.{MethodName(method)}";
}
