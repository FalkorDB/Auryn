"""The full-recompute parse fans every file across a process pool (core.parallel.parse_blobs_ordered),
so it must still return results in INPUT order or node ids would be nondeterministic. Exercise the
*parallel* branch (more than the parallel threshold of files, workers>1) against the real installed
Go parser and assert it is byte-for-byte identical to a forced-serial parse of the same blobs -- if
the pool returned results out of order, or a worker failed to parse, the dataclass equality breaks.
"""

from __future__ import annotations

from auryn.core.parallel import _PARALLEL_MIN_FILES, parse_blobs_ordered
from auryn.ingestors.go.file_parser import parse_source


def _blobs(n: int) -> dict[str, bytes]:
    # Distinct content per file (a unique func name) so an out-of-order result set can't
    # accidentally still compare equal.
    return {str(i): f"package p\n\nfunc F{i}() int {{ return {i} }}\n".encode() for i in range(n)}


def test_parallel_parse_matches_serial_and_preserves_order():
    n = _PARALLEL_MIN_FILES * 3  # comfortably over the threshold so the pool path runs
    blobs = _blobs(n)
    entries = [(f"f{i}.go", str(i)) for i in range(n)]

    def read(sha: str) -> bytes:
        return blobs[sha]

    serial = parse_blobs_ordered(entries, read, parse_source, workers=1)
    parallel = parse_blobs_ordered(
        entries, read, parse_source, preload=["auryn.ingestors.go.file_parser"], workers=4
    )

    # Order preserved and every file actually parsed (one func each).
    assert [f.path for f in parallel] == [f"f{i}.go" for i in range(n)]
    assert all(len(f.functions) == 1 for f in parallel)
    # Structural equality: same objects, same order as the serial baseline.
    assert parallel == serial


def test_parallel_parse_sink_mode_streams_every_result():
    # sink mode (used by TypeScript's disk parse cache): results are streamed to the sink as they
    # complete and never collected, so the whole tree is never held in memory. Prove every file is
    # still handed to the sink exactly once across the pool path.
    n = _PARALLEL_MIN_FILES * 3
    blobs = _blobs(n)
    entries = [(f"f{i}.go", str(i)) for i in range(n)]

    seen: dict[str, str] = {}

    def sink(entry, raw_file):
        # entry is (path, blob_sha); record the parsed func name keyed by blob_sha.
        seen[entry[1]] = raw_file.functions[0].name

    ret = parse_blobs_ordered(
        entries, lambda sha: blobs[sha], parse_source,
        preload=["auryn.ingestors.go.file_parser"], workers=4, sink=sink,
    )
    assert ret is None  # sink mode collects nothing
    assert seen == {str(i): f"F{i}" for i in range(n)}
