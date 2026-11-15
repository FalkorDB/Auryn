"""`.h` C-vs-C++ disambiguation (core.cpp_headers.is_cpp_header).

`.h` is routed to the C ingestor by default, but real C++ overwhelmingly uses `.h` for its headers --
sending a C++ header to the C grammar loses the whole class. `is_cpp_header` sniffs C++-only constructs
to re-route those to the C++ ingestor. The two failure modes it must avoid:
  * false NEGATIVE (a C++ header read as C) -> the class is mangled: data loss.
  * false POSITIVE (a C header read as C++) -> it splits from its `.c` definition (two id schemes).
Comments and string/char literals are stripped first, because on real git a `::` in a doc comment, a
`printf("%s::%s")`, or the word "namespace"/"operator" in prose all otherwise false-positived a C header.
"""

from __future__ import annotations

import pytest

from auryn.core.cpp_headers import is_cpp_header

# Real C headers (several are the exact git files that false-positived before comment/string stripping).
_C_HEADERS = {
    "plain C": "struct Point { int x; int y; };\nint add(int a, int b);\n#define MAX 10\n",
    "scope-res in doc comment": "/* see transport.c::transport_get_remote_refs() */\nint foo(void);\n",
    "scope-res in a string": 'void p(void){ printf("%s::%s\\n", suite, test); }\n',
    "`typename` is a valid C identifier": "struct info { char *typename; int n; };\n",
    "`namespace` in prose comment": "/* the per-workspace ref namespace is implemented here */\nint n;\n",
    "`namespace` in a string literal": '#error "namespace conflict: \'_\' pre-defined?"\n',
    "`operator` in a comment": "/* before a close-group or an alternation operator */\nint x;\n",
    "`virtual` in a comment": "/* the virtual function table for iterators */\nstruct vt { int (*next)(void); };\n",
    "bitfield named private": "struct s { unsigned private : 1; unsigned public : 1; };\n",
    "function pointer, no C++": "typedef int (*cmp)(const void *, const void *);\nvoid qsort2(cmp c);\n",
    "function-pointer struct member": "struct Ops { int (*compute)(int); const char *name; };\n",
    "bitwise-not of a call": "int g(void);\nint h(void){ return ~g(); }\n",
    "var named virtual": "int virtual;\nint f(void){ return virtual; }\n",
    "compare-to-zero of a call": "int foo(int);\nint f(int x){ if (foo(x) == 0) return 1; return 0; }\n",
    "function-pointer initialized to 0": "struct S { int a; };\nstatic void (*callback)(int) = 0;\n",
}

# Real C++ headers -- each uses a construct a C compiler rejects.
_CPP_HEADERS = {
    "namespace + class": "namespace acme {\nclass Widget {\n public:\n  int Compute(int x);\n};\n}\n",
    "scope resolution": "int Foo::bar(int x);\n",
    "template": "template <typename T>\nT max(T a, T b);\n",
    "using namespace": "using namespace std;\nint f();\n",
    "named cast": "inline int to_int(long y) { return static_cast<int>(y); }\n",
    "anonymous namespace": "namespace {\nint helper();\n}\n",
    "class with inheritance": "class Circle : public Shape {\n  int area();\n};\n",
    "operator overload": "struct V { V operator+(const V& o); };\n",
    "std:: qualified type": "#include <vector>\nstd::vector<int> make();\n",
    "struct with a constructor": "struct Widget { Widget(); int Compute(int x); };\n",
    "struct with a virtual method": "struct B { virtual void run(); };\n",
    "pure-virtual method": "struct Base { virtual int f() = 0; };\n",
    "defaulted constructor": "struct P { P() = default; int x; };\n",
    "deleted copy constructor": "struct N { N(const N&) = delete; };\n",
}


@pytest.mark.parametrize("name,text", _C_HEADERS.items(), ids=list(_C_HEADERS))
def test_c_headers_not_misrouted_to_cpp(name, text):
    assert is_cpp_header(text) is False, f"C header false-positived as C++: {name}"


@pytest.mark.parametrize("name,text", _CPP_HEADERS.items(), ids=list(_CPP_HEADERS))
def test_cpp_headers_detected(name, text):
    assert is_cpp_header(text) is True, f"C++ header read as C (would mangle the class): {name}"
