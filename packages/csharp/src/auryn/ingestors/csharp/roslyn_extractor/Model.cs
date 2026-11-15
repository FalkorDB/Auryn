namespace RoslynExtractor;

// The wire format between this tool and the Python ingestion pipeline. Nodes/edges
// map 1:1 to auryn.core.graph_model.GraphNode/GraphEdge. Envelope keys are
// camelCased by the serializer (label/id/relType/...); the free-form `Properties`
// dictionary keys are written verbatim, so they use the graph's snake_case
// convention (name, accessibility, is_static, line_start, ...).

public sealed record Node(string Label, string Id, Dictionary<string, object?> Properties);

public sealed record Edge(
    string RelType,
    string FromLabel,
    string FromId,
    string ToLabel,
    string ToId,
    Dictionary<string, object?> Properties);

public sealed record ProjectInfo(string Name, string? Solution, string? CommitSha, string? TargetFramework);

public sealed class Metrics
{
    public int FilesParsed { get; set; }
    public int FilesTotal { get; set; }
    public int TypesResolved { get; set; }
    public int UnresolvedSymbols { get; set; }
    public int UnresolvedCalls { get; set; }
    public int DiRegistrations { get; set; }
    public int DiResolvedCalls { get; set; }
}

public sealed class Output
{
    public int SchemaVersion { get; set; } = 1;
    public string ParserVersion { get; set; } = "";
    public ProjectInfo Project { get; set; } = null!;
    public List<Node> Nodes { get; set; } = new();
    public List<Edge> Edges { get; set; } = new();
    public Metrics Metrics { get; set; } = new();
}
