using System.Reflection;
using System.Text.Json;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using RoslynExtractor;

// roslyn-extractor: Roslyn-based semantic extraction of a C# source tree into the
// ingestor graph model, emitted as JSON on stdout (or --out <path>).
//
// Rather than MSBuildWorkspace (which couples us to a specific MSBuild/SDK version),
// this builds a single CSharpCompilation over every .cs file under --source, with the
// running runtime's reference assemblies. That gives a full semantic model -- and
// therefore DI resolution, real EXTENDS/IMPLEMENTS, and resolved call graphs -- for
// every symbol declared in the source, degrading gracefully (counted in metrics) for
// types that live in unreferenced external packages. Full cross-project / NuGet
// reference resolution via MSBuildWorkspace is a documented follow-on.

static string? Arg(string[] args, string name)
{
    var i = Array.IndexOf(args, name);
    return i >= 0 && i + 1 < args.Length ? args[i + 1] : null;
}

var source = Arg(args, "--source");
var projectName = Arg(args, "--project-name");
if (source is null || projectName is null)
{
    Console.Error.WriteLine("usage: roslyn-extractor --source <dir> --project-name <name> [--solution-name <name>] [--commit-sha <sha>] [--target-framework <tf>] [--dto-suffix <s> ...] [--out <path>]");
    return 2;
}
var solutionName = Arg(args, "--solution-name");
var commitSha = Arg(args, "--commit-sha");
var targetFramework = Arg(args, "--target-framework");
var outPath = Arg(args, "--out");

var dtoSuffixes = new List<string>();
for (var i = 0; i < args.Length - 1; i++)
    if (args[i] == "--dto-suffix")
        dtoSuffixes.Add(args[i + 1]);
if (dtoSuffixes.Count == 0)
    dtoSuffixes.AddRange(new[] { "Dto", "Request", "Response", "ViewModel" });

var sourceRoot = Path.GetFullPath(source);
var excludedDirs = new HashSet<string>(StringComparer.Ordinal) { "bin", "obj" };

// AURYN_VERBOSE=1 (exported by `auryn --verbose`) streams a line per source file to
// stderr as it's parsed, so a long C# extraction shows live progress -- and, if a parse hard-
// crashes, the last line names the offending file. roslyn.py forwards these to its logger.
var verbose = (Environment.GetEnvironmentVariable("AURYN_VERBOSE") ?? "").Trim().ToLowerInvariant()
    is not ("" or "0" or "false" or "no" or "off");

var candidates = Directory.EnumerateFiles(sourceRoot, "*.cs", SearchOption.AllDirectories)
    .Select(file => (file, relative: Path.GetRelativePath(sourceRoot, file).Replace('\\', '/')))
    .Where(t => !t.relative.Split('/').Any(excludedDirs.Contains))
    .ToList();

var trees = new List<SyntaxTree>();
var filePaths = new List<string>();
var filesTotal = 0;
for (var idx = 0; idx < candidates.Count; idx++)
{
    var (file, relative) = candidates[idx];
    filesTotal++;
    if (verbose)
        Console.Error.WriteLine($"[{idx + 1}/{candidates.Count}] parsing {relative}");
    string text;
    try { text = File.ReadAllText(file); }
    catch { continue; }
    trees.Add(CSharpSyntaxTree.ParseText(text, path: relative));
    filePaths.Add(relative);
}
if (verbose)
    Console.Error.WriteLine($"parsed {trees.Count} file(s); resolving symbols (semantic analysis)...");
filePaths.Sort(StringComparer.Ordinal);

var references = ((string?)AppContext.GetData("TRUSTED_PLATFORM_ASSEMBLIES") ?? "")
    .Split(Path.PathSeparator)
    .Where(p => p.Length > 0 && File.Exists(p))
    .Select(p => (MetadataReference)MetadataReference.CreateFromFile(p))
    .ToList();

var compilation = CSharpCompilation.Create(
    assemblyName: projectName,
    syntaxTrees: trees,
    references: references,
    options: new CSharpCompilationOptions(OutputKind.DynamicallyLinkedLibrary));

var projectId = Ids.ProjectId(projectName, solutionName);
var extractor = new Extractor(compilation, projectId, solutionName, dtoSuffixes.ToArray(), filePaths);
var output = extractor.Run();

// Root nodes the graph model expects (a Project, optionally wrapped in a Solution).
output.Project = new ProjectInfo(projectName, solutionName, commitSha, targetFramework);
var projectProps = new Dictionary<string, object?> { ["name"] = projectName };
if (targetFramework is not null) projectProps["target_framework"] = targetFramework;
if (commitSha is not null) projectProps["commit_sha"] = commitSha;
output.Nodes.Insert(0, new Node("Project", projectId, projectProps));
if (!string.IsNullOrEmpty(solutionName))
{
    var solutionId = Ids.SolutionId(solutionName);
    output.Nodes.Insert(0, new Node("Solution", solutionId, new() { ["name"] = solutionName }));
    output.Edges.Insert(0, new Edge("HAS_PROJECT", "Solution", solutionId, "Project", projectId, new()));
}

output.Metrics.FilesParsed = trees.Count;
output.Metrics.FilesTotal = filesTotal;
output.Metrics.UnresolvedSymbols = compilation
    .GetDiagnostics()
    .Count(d => d.Severity == DiagnosticSeverity.Error && d.Id is "CS0246" or "CS0234" or "CS0103");

var version = Assembly.GetExecutingAssembly().GetName().Version?.ToString() ?? "0.0.0";
var roslynVersion = typeof(CSharpCompilation).Assembly.GetName().Version?.ToString() ?? "?";
output.ParserVersion = $"roslyn-extractor {version}; Roslyn {roslynVersion}";

var json = JsonSerializer.Serialize(output, new JsonSerializerOptions
{
    PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
    WriteIndented = false,
    DefaultIgnoreCondition = System.Text.Json.Serialization.JsonIgnoreCondition.Never,
});

if (outPath is not null)
    File.WriteAllText(outPath, json);
else
    Console.Out.Write(json);
return 0;
