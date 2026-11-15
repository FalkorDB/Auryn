"""A focused Protobuf parser for the gRPC seam -- it extracts only the SERVICE CONTRACT (package +
`service { rpc ... }`), which is all the stitch needs to link a client's RPC call and a server's
handler to the shared `.proto`. It deliberately does NOT model messages/fields/enums/options (not
needed to tie the two code subgraphs together), so a tiny dependency-free regex parser suffices
instead of a full tree-sitter-proto grammar.

Handles proto2/proto3: `package a.b.c;`, `service S { rpc M (Req) returns (stream Resp); }`,
line (`//`) and block (`/* */`) comments, and `stream` on either side."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')  # a double-quoted string literal (handles escapes)
_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
_PACKAGE = re.compile(r"\bpackage\s+([A-Za-z_][\w.]*)\s*;")
_SERVICE = re.compile(r"\bservice\s+([A-Za-z_]\w*)\s*\{")
# rpc Name ( [stream] ReqType ) returns ( [stream] RespType )
_RPC = re.compile(
    r"\brpc\s+([A-Za-z_]\w*)\s*\(\s*(?:stream\s+)?([\w.]+)\s*\)\s*"
    r"returns\s*\(\s*(?:stream\s+)?([\w.]+)\s*\)"
)


@dataclass
class ProtoMethod:
    name: str
    request_type: str
    response_type: str


@dataclass
class ProtoService:
    name: str
    methods: list[ProtoMethod] = field(default_factory=list)


@dataclass
class ProtoFile:
    package: str
    services: list[ProtoService] = field(default_factory=list)


def _block_body(text: str, open_brace_idx: int) -> str:
    """The text between the `{` at `open_brace_idx` and its matching `}` (brace-depth aware, so a
    nested `{}` -- e.g. an rpc body with options -- doesn't end the service early)."""
    depth = 0
    for i in range(open_brace_idx, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace_idx + 1:i]
    return text[open_brace_idx + 1:]  # unterminated (malformed) -- take the rest


def parse_proto(text: str) -> ProtoFile:
    """Parse one `.proto` file's text into its package + service contract. Never raises on malformed
    input -- a best-effort parse returns whatever services/rpcs it could recognize."""
    # Neutralize string-literal CONTENTS first, so `//` inside `"http://..."` isn't read as a comment and a
    # `}` or `service`/`rpc` inside an option string (`option (x) = "}";`) can't break brace matching or
    # inject a phantom service/rpc. Service/rpc parsing needs only identifiers, never string contents.
    text = _STRING.sub('""', text)
    text = _COMMENT.sub(" ", text)
    pkg_m = _PACKAGE.search(text)
    package = pkg_m.group(1) if pkg_m else ""
    services: list[ProtoService] = []
    for m in _SERVICE.finditer(text):
        body = _block_body(text, m.end() - 1)  # m.end()-1 is the `{`
        methods = [ProtoMethod(r.group(1), r.group(2), r.group(3)) for r in _RPC.finditer(body)]
        services.append(ProtoService(m.group(1), methods))
    return ProtoFile(package=package, services=services)
